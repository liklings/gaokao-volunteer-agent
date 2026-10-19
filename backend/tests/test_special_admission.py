"""
军事/警务/司法/定向培养等特殊类型招生默认排除的回归测试。

真实撞到的问题：一个只想读普通计算机专业的江苏考生，检索结果前几条全是
"网络空间部队信息工程大学""空军工程大学"这类军事院校——这些学校面向
普通考生的名额少、竞争小，历年最低位次反而比普通高校宽松，纯按参考概率
排序会被顶到最前面，但报考这类专业组通常还需要政治审查/体检/面试等额外
资格，默认不该跟常规专业组混在一起推荐。见 app/services/special_admission.py
和 rules_engine.evaluate_group() 里"招生类型"这条硬性规则。
"""
from app.models import AdmissionGroup, School
from app.schemas.candidate import ProfileIn
from app.services.rules_engine import evaluate_group
from app.services.special_admission import (
    classify_special_admission,
    detect_special_admission_type,
    resolve_admission_gate,
)


def test_detect_matches_military_school_name():
    assert detect_special_admission_type("空军工程大学", "01专业组") == "空军"


def test_detect_matches_directed_training_group_name():
    assert detect_special_admission_type("潍坊科技学院", "05专业组(定向培养军士)") == "定向培养军士"


def test_detect_returns_none_for_ordinary_school():
    assert detect_special_admission_type("南京大学", "04专业组") is None


def test_detect_does_not_false_positive_on_normal_cyber_security_major():
    """"网络空间安全"这类正常计算机相关专业方向不该被"安全"两个字误伤——
    关键词表里刻意没放"安全"这种过于宽泛的词。"""
    assert detect_special_admission_type("东南大学", "网络空间安全类专业组") is None


def test_classify_prefers_authoritative_admission_category_over_keyword():
    """admission_category 有值时以它为准——"提前录取本科院校—军队院校"
    是数据源里现成的官方分类，比关键词匹配可靠，即使学校名/专业组名完全
    没有任何军事相关字样也应该被正确识别。"""
    assert classify_special_admission("国防科技大学", "01专业组", "提前录取本科院校—军队院校") == "提前录取本科院校—军队院校"


def test_classify_treats_regular_categories_as_normal():
    assert classify_special_admission("南京大学", "04专业组", "本科院校") is None
    assert classify_special_admission("某职业学院", "02专业组", "专科院校") is None


def test_classify_treats_special_programs_admission_category_as_special():
    """专项计划（高校专项/地方专项/农村订单定向医学生等）通常要求户籍/
    资格审核，admission_category 命中这些分类名时也该默认排除。"""
    assert classify_special_admission("某大学", "01专业组", "提前录取本科院校—高校专项计划") is not None
    assert classify_special_admission("某大学", "01专业组", "专科院校-定向培养军士") is not None


def test_classify_falls_back_to_keyword_when_category_is_none():
    """admission_category 为 None（数据源没有这一列，比如种子数据）时，
    退回关键词匹配兜底，不能直接放行。"""
    assert classify_special_admission("空军工程大学", "01专业组", None) == "空军"
    assert classify_special_admission("南京大学", "04专业组", None) is None


def _military_group() -> AdmissionGroup:
    school = School(id="mil", name="空军工程大学", city="西安", province="江苏", tags=[])
    group = AdmissionGroup(
        id="mil-01", school_id=school.id, code="01", track="物理类",
        subjects_required=[], major_keywords=[],
        tuition=0, duration=None, why_recommend="", caution="", source_label="",
    )
    group.school = school
    group.records = []
    return group


def test_evaluate_group_hard_excludes_military_school_by_default():
    profile = ProfileIn(province="江苏", track="物理类", subjects=[])
    candidate = evaluate_group(_military_group(), profile)
    assert candidate.all_hard_pass is False
    special_rule = next(r for r in candidate.rules if r.name == "招生类型")
    assert special_rule.passed is False


# ---- "多轮槽位填充表单化"：resolve_admission_gate() 两档开关 ----


def test_resolve_admission_gate_ordinary_school_always_passes():
    passed, reason = resolve_admission_gate("南京大学", "04专业组", "本科院校", None, None)
    assert passed is True
    assert reason is None


def test_resolve_admission_gate_military_always_excluded_even_when_toggles_true():
    """军事/警务/司法/航海类没有开关——不管两个开关传什么都应该排除。"""
    passed, reason = resolve_admission_gate(
        "空军工程大学", "01专业组", "提前录取本科院校—军队院校", True, True,
    )
    assert passed is False
    assert reason == "提前录取本科院校—军队院校"


def test_resolve_admission_gate_directed_training_soldier_is_always_excluded_not_special_program():
    """"定向培养军士"本质是军事性质，即使字面含"定向"，也不该被
    accept_special_program 开关放行——必须命中 _ALWAYS_EXCLUDED_MARKERS。"""
    passed, reason = resolve_admission_gate(
        "潍坊科技学院", "05专业组(定向培养军士)", None, None, True,
    )
    assert passed is False
    assert reason == "定向培养军士"


def test_resolve_admission_gate_sino_foreign_excluded_by_default():
    """classify_special_admission() 命中"中外合作"要靠关键词匹配这条路径
    （admission_category 传现成的"中外合作办学"分类名，不是"本科院校"这种
    正常批次名——正常批次名会让 classify_special_admission 直接判定为
    None、根本走不到这两个开关这里）。"""
    passed, reason = resolve_admission_gate(
        "某大学", "01专业组", "中外合作办学本科", None, None,
    )
    assert passed is False
    assert reason is not None


def test_resolve_admission_gate_sino_foreign_allowed_when_accepted():
    passed, reason = resolve_admission_gate(
        "某大学", "01专业组", "中外合作办学本科", True, None,
    )
    assert passed is True
    assert reason is None


def test_resolve_admission_gate_sino_foreign_stays_excluded_when_explicitly_declined():
    passed, reason = resolve_admission_gate(
        "某大学", "01专业组", "中外合作办学本科", False, None,
    )
    assert passed is False


def test_resolve_admission_gate_special_program_excluded_by_default():
    passed, reason = resolve_admission_gate(
        "某大学", "01专业组", "提前录取本科院校—高校专项计划", None, None,
    )
    assert passed is False
    assert reason is not None


def test_resolve_admission_gate_special_program_allowed_when_accepted():
    passed, reason = resolve_admission_gate(
        "某大学", "01专业组", "提前录取本科院校—高校专项计划", None, True,
    )
    assert passed is True
    assert reason is None


def test_evaluate_group_allows_sino_foreign_when_profile_accepts():
    """admission_category 命中"中外合作"分类名，跟真实数据（"江苏专家版"）里
    这类项目的官方分类方式一致——accept_sino_foreign=True 时应该放行，
    未表态（None）时应该维持默认排除。"""
    from app.models import AdmissionRecord

    school = School(id="sf", name="某大学", city="苏州", province="江苏", tags=[])
    group = AdmissionGroup(
        id="sf-01", school_id=school.id, code="01", track="物理类",
        subjects_required=[], major_keywords=[],
        tuition=60000, duration=None, why_recommend="", caution="", source_label="",
    )
    group.school = school
    group.admission_category = "中外合作办学本科"
    group.records = [AdmissionRecord(group_id=group.id, year=2025, group_name="01专业组")]

    profile_accept = ProfileIn(province="江苏", track="物理类", subjects=[], accept_sino_foreign=True)
    profile_undecided = ProfileIn(province="江苏", track="物理类", subjects=[], accept_sino_foreign=None)

    accepted = evaluate_group(group, profile_accept)
    undecided = evaluate_group(group, profile_undecided)
    accepted_rule = next(r for r in accepted.rules if r.name == "招生类型")
    undecided_rule = next(r for r in undecided.rules if r.name == "招生类型")
    assert accepted_rule.passed is True
    assert undecided_rule.passed is False


# ---- 真实撞到的问题：admission_category 判定为"正常批次"，但真正的
# 中外合作/境外分校信息只出现在专业组名称的括注里 ----


def test_classify_catches_sino_foreign_hidden_in_group_name_under_normal_category():
    """真实数据里"计划类别"经常不会单独标"中外合作办学"，还是标成
    "本科院校"，中外合作/境外分校的信息只出现在专业组名称的括注里
    （比如"05专业组(中外合作办学)"）——之前的逻辑只要 admission_category
    有值就直接采信、不再看关键词，这类候选完全漏判成了"正常批次"。"""
    assert classify_special_admission(
        "东北大学秦皇岛分校", "05专业组(中外合作办学)", "本科院校",
    ) is not None
    assert classify_special_admission(
        "厦门大学", "09专业组(马来西亚分校)", "本科院校",
    ) is not None


def test_resolve_admission_gate_catches_sino_foreign_hidden_in_group_name_under_normal_category():
    passed, reason = resolve_admission_gate(
        "东北大学秦皇岛分校", "05专业组(中外合作办学)", "本科院校", None, None,
    )
    assert passed is False
    assert reason is not None

    # 明确表示愿意考虑中外合作/境外分校后应该放行。
    passed, reason = resolve_admission_gate(
        "东北大学秦皇岛分校", "05专业组(中外合作办学)", "本科院校", True, None,
    )
    assert passed is True


def test_resolve_admission_gate_catches_overseas_branch_campus_hidden_in_group_name():
    """境外分校（比如"马来西亚分校"）归进跟中外合作办学同一个开关——
    都是学费更高、就读环境跟常规校本部不同的类型，默认排除，
    accept_sino_foreign=True 才放行。"""
    passed, reason = resolve_admission_gate(
        "厦门大学", "09专业组(马来西亚分校)", "本科院校", None, None,
    )
    assert passed is False

    passed, reason = resolve_admission_gate(
        "厦门大学", "09专业组(马来西亚分校)", "本科院校", True, None,
    )
    assert passed is True


def test_classify_still_catches_military_keyword_even_under_normal_admission_category():
    """同样的漏判风险对军事类关键词也成立：如果 admission_category 恰好
    标成了"本科院校"（数据源标注不一致的情况），专业组名称里的军事关键词
    也不该因为 admission_category 有值就被跳过——这类始终没有开关，
    漏判的后果比中外合作/专项计划更严重。"""
    assert classify_special_admission("某大学", "05专业组(定向培养军士)", "本科院校") is not None
    passed, _ = resolve_admission_gate(
        "某大学", "05专业组(定向培养军士)", "本科院校", True, True,
    )
    assert passed is False  # 军事类不提供开关，两个开关传 True 也不放行
