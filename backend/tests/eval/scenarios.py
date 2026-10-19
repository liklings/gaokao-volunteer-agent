"""
第2层（硬约束零容忍）场景库——第一批种子 case，对应需求里"边界硬约束
Case"这一类：选科冲突、地域极端、特殊排除（中外合作/专项计划/军警）。

这是"代码自动化断言"这一步的核心产出：每条 case 都有唯一、可程序化判定
的期望结果（某个候选必须/绝不能出现在通过硬性规则的结果里），不存在
"这个回答算不算合理"的模糊地带，所以放在三段流水线（代码断言 → 历史
回测 → LLM-as-Judge）里最先做、跑得最快、也最该做到 0 违规。

后续按需求扩充到 300~500 条时，直接往 LAYER2_SCENARIOS 这个列表里追加
新的 dict 即可，不需要改测试代码本身（tests/eval/test_layer2_hard_constraints.py
是通用 runner，靠 pytest.mark.parametrize 遍历这个列表）。

字段说明：
- id：场景编号，建议前缀标注类别（subject-conflict / geo-extreme /
  exclude-sino-foreign 等），方便以后按类别统计"硬约束违规率"。
- description：对应需求文档里"边界硬约束 Case"具体是哪一条、真实场景是
  什么，方便看失败报告时立刻理解这条 case 在防什么问题。
- profile：考生条件（app.schemas.candidate.ProfileIn）。
- groups：候选池（不落库的 AdmissionGroup 列表，见 fixtures.make_group）。
- must_pass：这些 group_id 必须出现在 all_hard_pass 的结果里（结果集合的
  下界——系统不能漏判，把明明合规的候选也误杀了）。
- must_fail：这些 group_id 绝不能出现在通过硬性规则的结果里（这才是真正
  的"零容忍"红线——选科不符、地域穿透、未经同意的中外合作/专项计划，
  只要漏判一个就算这条 case 失败）。
"""
from __future__ import annotations

from .fixtures import make_group, make_profile, make_school

LAYER2_SCENARIOS: list[dict] = [
    # ---- 选科冲突 ----
    {
        "id": "subject-conflict-01-wuhuadi-vs-wuhuasheng",
        "description": (
            "考生选考物理+化学+地理（物化地组合），临床医学等专业组通常"
            "硬性要求物理+化学+生物（物化生）——地理不能替代生物，这类"
            "专业组必须被硬性排除，不能因为「都选了物理化学」就放行。"
        ),
        "profile": make_profile(subjects=["物理", "化学", "地理"]),
        "groups": [
            make_group(
                "clinical-med", make_school("s-med", city="南京"),
                subjects_required=["物理", "化学", "生物"], min_rank=6000,
            ),
            make_group(
                "cs-normal", make_school("s-cs", city="南京"),
                subjects_required=["物理", "化学"], min_rank=6000,
            ),
        ],
        "must_pass": {"cs-normal"},
        "must_fail": {"clinical-med"},
    },
    {
        "id": "subject-conflict-02-no-requirement-always-passes",
        "description": "专业组没有选科限制（subjects_required 为空）时，任何选科组合都应该放行，不能误伤。",
        "profile": make_profile(subjects=["历史", "政治", "地理"], track="历史类"),
        "groups": [
            make_group("no-req", make_school("s-noreq", city="南京"), track="历史类", subjects_required=[], min_rank=6000),
        ],
        "must_pass": {"no-req"},
        "must_fail": set(),
    },
    # ---- 地域极端：只考虑省内，且要考虑"招生目标省份"≠"学校实际所在省份" ----
    {
        "id": "geo-extreme-01-home-province-only-blocks-out-of-province-school",
        "description": (
            "考生要求「只考虑省内高校」——重点是要能识别出「学校面向本省"
            "招生」和「学校实际在本省」是两回事：一所物理上在河北秦皇岛的"
            "学校，即使 School.province 字段填的是江苏（意味着它有一个"
            "面向江苏招生的计划），只要用户勾选了「只看省内院校」，就该"
            "按学校实际所在地（秦皇岛→河北）排除，不能因为招生目标省份"
            "写的是江苏就放行——这正是种子数据里真实出现过的「潍坊科技"
            "学院」模式。"
        ),
        "profile": make_profile(home_province_only=True),
        "groups": [
            make_group(
                "in-nanjing", make_school("s-nj", city="南京", province="江苏"),
                min_rank=6000,
            ),
            make_group(
                "in-suzhou", make_school("s-sz", city="苏州", province="江苏"),
                min_rank=6000,
            ),
            # School.province 写的是"江苏"（面向江苏招生），但实际物理位置在
            # 河北秦皇岛——只看省内院校时必须按实际所在地排除。
            make_group(
                "actually-hebei", make_school("s-hb", city="秦皇岛", province="江苏"),
                min_rank=6000,
            ),
        ],
        "must_pass": {"in-nanjing", "in-suzhou"},
        "must_fail": {"actually-hebei"},
    },
    {
        "id": "geo-extreme-02-plain-out-of-province-target-excluded",
        "description": "没有勾选「只看省内」时，最基本的招生目标省份不符（School.province != 考生省份）也必须硬性排除，这是最基础的红线，不依赖 home_province_only 开关。",
        "profile": make_profile(home_province_only=False),
        "groups": [
            make_group("target-jiangsu", make_school("s-target", city="南京", province="江苏"), min_rank=6000),
            make_group("target-other", make_school("s-other", city="武汉", province="湖北"), min_rank=6000),
        ],
        "must_pass": {"target-jiangsu"},
        "must_fail": {"target-other"},
    },
    # ---- 特殊排除：中外合作办学 / 专项计划，默认排除、明确同意才放行 ----
    {
        "id": "exclude-sino-foreign-01-default-excluded",
        "description": "考生没有明确表示接受中外合作办学（accept_sino_foreign 为默认值 None）时，中外合作项目必须被排除——即使学费在预算内、选科完全匹配。",
        "profile": make_profile(accept_sino_foreign=None),
        "groups": [
            make_group(
                "sino-foreign", make_school("s-sf", city="南京"),
                admission_category="中外合作办学", min_rank=6000,
            ),
        ],
        "must_pass": set(),
        "must_fail": {"sino-foreign"},
    },
    {
        "id": "exclude-sino-foreign-02-explicit-accept-allows",
        "description": "考生在对话里明确表示「愿意考虑中外合作办学」（accept_sino_foreign=True）后，同样的候选应该放行——不是一刀切禁止，用户可以自己决定。",
        "profile": make_profile(accept_sino_foreign=True),
        "groups": [
            make_group(
                "sino-foreign", make_school("s-sf", city="南京"),
                admission_category="中外合作办学", min_rank=6000,
            ),
        ],
        "must_pass": {"sino-foreign"},
        "must_fail": set(),
    },
    {
        "id": "exclude-special-program-01-default-excluded-explicit-accept-allows",
        "description": "专项计划/定向培养默认排除、明确同意后放行，逻辑跟中外合作对称，用同一条 case 覆盖「默认排除」和「同意后放行」两个分支。",
        "profile": make_profile(accept_special_program=None),
        "groups": [
            make_group(
                "special-program", make_school("s-sp", city="南京"),
                admission_category="国家专项计划", min_rank=6000,
            ),
        ],
        "must_pass": set(),
        "must_fail": {"special-program"},
    },
    {
        "id": "exclude-military-01-no-opt-in-switch-exists",
        "description": (
            "军事/警务/司法/航海类招生始终硬性排除、不提供任何开关——即使"
            "考生把 accept_sino_foreign 和 accept_special_program 都设成"
            "True，也不能被误判成「已经同意」而放行，这是跟中外合作/专项"
            "计划唯一不对称的地方，必须单独测，防止未来重构时不小心把"
            "军警类也接进了同一个开关判断里。"
        ),
        "profile": make_profile(accept_sino_foreign=True, accept_special_program=True),
        "groups": [
            make_group(
                "military", make_school("s-mil", city="南京"),
                admission_category="提前录取本科院校—军队院校", min_rank=6000,
            ),
        ],
        "must_pass": set(),
        "must_fail": {"military"},
    },
    # ---- 预算/科类等其余基础硬约束，防止重构时连带弄坏 ----
    {
        "id": "budget-01-over-budget-excluded",
        "description": "学费超出预算上限时必须排除。",
        "profile": make_profile(budget_max=10000),
        "groups": [
            make_group("cheap", make_school("s-cheap", city="南京"), tuition=6000, min_rank=6000),
            make_group("expensive", make_school("s-exp", city="南京"), tuition=45000, min_rank=6000),
        ],
        "must_pass": {"cheap"},
        "must_fail": {"expensive"},
    },
    {
        "id": "budget-02-missing-tuition-data-not-excluded",
        "description": "学费数据缺失（tuition=None）时不能当成「超预算」误杀——数据缺失应该如实告知、不作为硬性排除依据，这条防止未来有人把「缺失」和「超标」搞混。",
        "profile": make_profile(budget_max=10000),
        "groups": [
            make_group("unknown-tuition", make_school("s-unk", city="南京"), tuition=None, min_rank=6000),
        ],
        "must_pass": {"unknown-tuition"},
        "must_fail": set(),
    },
    {
        "id": "track-01-wuli-vs-lishi-mismatch-excluded",
        "description": "科类（物理类/历史类）不匹配必须硬性排除——这是最基础的红线，物理类考生不该看到历史类专业组。",
        "profile": make_profile(track="物理类"),
        "groups": [
            make_group("physics-track", make_school("s-phy", city="南京"), track="物理类", min_rank=6000),
            make_group("history-track", make_school("s-his", city="南京"), track="历史类", min_rank=6000),
        ],
        "must_pass": {"physics-track"},
        "must_fail": {"history-track"},
    },
]
