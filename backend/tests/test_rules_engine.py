"""
rules_engine.search_candidates() 排序逻辑的测试。

真实撞到过的可用性/信息量问题：接入真实数据（一份专家版大表能聚合出上千个
专业组）之后，单次查询能匹配到几百上千条候选，但 search_candidates() 之前
完全没有排序——results 靠 city_pref 才有一点排序（没设置就是数据库返回的
任意顺序），audit（含未通过硬性规则的候选，用于"资格核验"面板）干脆完全不
排序、把全部候选原样堆给前端。候选一多，用户在前端根本翻不到自己关心的
那个学校，"资格核验"面板里少数几条通过的候选被淹没在大量未通过的候选里。

这里只测排序逻辑本身（用手工构造的、不落库的 ORM 对象——search_candidates
只读 group.school/group.records/group.track 等属性，不需要真的存进数据库），
不测数据库集成，数据库层面的行为在 test_admission_master_loader.py 等文件
里已经覆盖过。
"""
import pandas as pd

from app.models import AdmissionGroup, AdmissionRecord, School
from app.schemas.candidate import ProfileIn
from app.services.rules_engine import build_rank_scale_context, evaluate_group, search_candidates
from app.services.score_rank_loader import load_score_rank_dataframe

PROVINCE = "测试省"


def _make_group(
    group_id: str,
    *,
    city: str = "测试市",
    track: str = "物理类",
    subjects_required: list[str] | None = None,
    tuition: int | None = None,
    min_rank: int | None = None,
) -> AdmissionGroup:
    """构造一个不落库的 AdmissionGroup（school/records 关系直接用 Python
    属性赋值，不经过数据库）——evaluate_group() 只读这些属性，不需要真实持久化。
    """
    school = School(id=f"{group_id}-school", name=f"{group_id}大学", city=city, province=PROVINCE, tags=[])
    group = AdmissionGroup(
        id=group_id, school_id=school.id, code=None, track=track,
        subjects_required=subjects_required or [], major_keywords=[],
        tuition=tuition, duration=None, why_recommend="", caution="", source_label="",
    )
    group.school = school
    group.records = []
    if min_rank is not None:
        group.records = [AdmissionRecord(
            group_id=group_id, year=2025, group_name=group_id, plan_count=10,
            min_score=600, min_rank=min_rank, source_url=None, fetched_at=None,
        )]
    return group


def _profile(**overrides) -> ProfileIn:
    base = dict(province=PROVINCE, track="物理类", subjects=["物理", "化学"], score=630, rank=5000)
    base.update(overrides)
    return ProfileIn(**base)


def test_results_sorted_by_rank_diff_descending():
    # min_rank 越小（比考生位次 5000 更靠前）→ diff 越大 → 越接近"保底"。
    # 分别构造 diff 明显不同的三个候选，验证 results 按 diff 从高到低排。
    groups = [
        _make_group("mid", min_rank=5000),    # diff=0，"稳妥"档附近
        _make_group("high", min_rank=8000),   # diff=3000，正差更大
        _make_group("low", min_rank=2000),    # diff=-3000，负差
    ]
    results, _audit = search_candidates(groups, _profile())

    diffs = [c.diff for c in results]
    assert diffs == sorted(diffs, reverse=True), f"results 应该按位次差从高到低排序，实际 diffs={diffs}"
    assert [c.group_id for c in results] == ["high", "mid", "low"]


def test_results_without_rank_diff_is_none_and_sinks_to_bottom():
    # 考生没填位次时，全部候选的 diff 都是 None——排序不应该报错，
    # 顺序退化成原有的 city_pref/稳定顺序即可。
    groups = [_make_group("a", min_rank=5000), _make_group("b", min_rank=1000)]
    results, _audit = search_candidates(groups, _profile(rank=None))
    assert all(c.diff is None for c in results)
    assert len(results) == 2


def test_major_matched_candidate_ranks_before_higher_diff_mismatch():
    """真实撞到的可用性问题：major_keyword 是软匹配、不参与硬性排除，但
    完全不参与排序的话，一个查"计算机"的考生，排在最前面的候选完全可能
    是护理学这类毫不相关的专业组，只是因为它位次差恰好更大。专业匹配
    应该是比位次差更优先的排序维度：即使位次差明显更小，专业对口的候选
    也该排在专业不对口、位次差更大的候选前面。
    """
    mismatched = _make_group("nursing", min_rank=8000)  # 位次差更大（更"保底"）
    mismatched.major_keywords = ["护理学"]
    matched = _make_group("cs", min_rank=5000)  # 位次差更小，但专业对口
    matched.major_keywords = ["计算机科学与技术"]

    results, _audit = search_candidates([mismatched, matched], _profile(major_keyword="计算机"))
    assert [c.group_id for c in results] == ["cs", "nursing"], (
        "专业对口的候选应该排在专业不对口的候选前面，不管位次差大小"
    )


def test_candidates_with_diff_rank_above_those_without():
    # 混合场景：部分候选算得出 diff，部分算不出（比如该专业组没有历年录取
    # 数据）——有 diff 的应该排在没有 diff 的前面，不能混在一起按原始顺序排。
    groups = [
        _make_group("no_history", min_rank=None),  # 没有历年记录 → diff=None
        _make_group("has_history", min_rank=5000),  # diff 算得出来
    ]
    results, _audit = search_candidates(groups, _profile())
    assert [c.group_id for c in results] == ["has_history", "no_history"]


def test_city_pref_is_tiebreak_within_same_diff():
    # 两个候选 min_rank 相同（diff 完全一样），只有 city 不同——city_pref
    # 匹配的那个应该排在前面，验证 city_pref 仍然生效（只是从"唯一排序依据"
    # 降级成了"同位次差档位内的次级排序依据"）。
    groups = [
        _make_group("other_city", city="其它市", min_rank=5000),
        _make_group("pref_city", city="偏好市", min_rank=5000),
    ]
    results, _audit = search_candidates(groups, _profile(city_pref="偏好市"))
    assert results[0].diff == results[1].diff, "两个候选的 diff 应该完全相同，才能验证 city_pref 是次级排序"
    assert [c.group_id for c in results] == ["pref_city", "other_city"]


# ---- 同位次跨年映射（RankScaleContext / build_rank_scale_context） ----
#
# 真实撞到的问题：diff = 历年 min_rank - 考生今年的 rank，隐含假设了
# "每年全省报考总人数差不多"，但实际每年都会自然波动、扩招缩招，直接
# 拿今年的位次去跟往年的位次相减不够准确。这里用一分一段表里每年的
# 总人数（近似取 cumulative_count 最大值）算一个缩放比例，把"考生今年
# 的位次"折算成"放到历史那一年的报考规模下大概相当于第几名"，再拿
# 折算后的位次去跟当年 min_rank 比，才是同一个"尺度"下的比较。
#
# _make_group() 构造的候选历年记录固定是 2025 年（见上面 _make_group 的
# 实现），这里复用它，只是额外往数据库里灌一分一段表数据、构造出
# build_rank_scale_context() 需要查询的真实场景。
#
# db 这个 fixture 用的是真实数据库、不做事务回滚（见 conftest.py），
# 所以这里刻意每个用例用自己独有的省份名（而不是复用模块级的 PROVINCE），
# 并且用幂等的 load_score_rank_dataframe()（跟真实导入脚本同一份逻辑）
# 而不是裸的 db.add(ScoreRankRow(...))——避免重复跑测试时撞
# (province, year, track, score) 唯一约束，也避免不同用例的一分一段表
# 数据互相污染彼此的 build_rank_scale_context() 结果。

def _seed_score_rank(db, province: str, year: int, rows: list[tuple[int, int]]) -> None:
    df = pd.DataFrame(
        [[score, 10, cum] for score, cum in rows],
        columns=["分数", "本段人数", "累计人数"],
    )
    load_score_rank_dataframe(db, df, province, year, "物理类")
    db.commit()


def test_evaluate_group_uses_rank_scale_to_adjust_cross_year_diff(db):
    """2025 年（历史年）总人数比 2026 年（考生自己这年，也是一分一段表
    里最新的一年）少，说明这两年报考规模不一样——不做换算时 diff 是
    "min_rank - 原始rank"，做了换算之后应该不一样（数值上更小，因为
    折算后的等效位次比原始位次更靠前/数字更小）。"""
    province = "测试省RK1"
    _seed_score_rank(db, province, 2025, [(600, 90000), (601, 80000)])
    _seed_score_rank(db, province, 2026, [(600, 100000), (601, 90000)])

    group = _make_group("target", min_rank=1000)  # 唯一一条历年记录是 2025 年
    group.school.province = province
    profile = _profile(province=province, rank=500)

    without_scale = evaluate_group(group, profile)  # 不传 rank_scale，退化成原有行为
    assert without_scale.diff == 1000 - 500
    assert without_scale.rank_equivalent_note is None

    rank_scale = build_rank_scale_context(db, province, "物理类")
    assert rank_scale.current_year == 2026  # 一分一段表里最新的一年
    with_scale = evaluate_group(group, profile, rank_scale)
    # 2025年总人数90000 < 2026年总人数100000，等效位次 = 500 * 90000/100000 = 450
    assert with_scale.diff == 1000 - 450, f"应该用折算后的位次算diff，实际 diff={with_scale.diff}"
    assert with_scale.diff != without_scale.diff, "换算前后的diff应该不一样，否则说明换算没生效"
    assert with_scale.rank_equivalent_note is not None
    assert "2025" in with_scale.rank_equivalent_note


def test_build_rank_scale_context_no_data_degrades_to_no_adjustment(db):
    """一分一段表完全没导入过这个省份/科类的数据时，应该优雅退化成
    "不做换算"，而不是报错，也不应该编一个误导性的换算说明。"""
    province = "测试省RK2-从没导入过一分一段表"
    rank_scale = build_rank_scale_context(db, province, "物理类")
    assert rank_scale.current_year is None

    group = _make_group("target", min_rank=1000)
    group.school.province = province
    result = evaluate_group(group, _profile(province=province, rank=500), rank_scale)
    assert result.diff == 1000 - 500, "查不到一分一段表数据时应该直接用原始位次，不报错"
    assert result.rank_equivalent_note is None, "没有可换算的数据时不应该生成换算说明"


def test_build_rank_scale_context_partial_data_notes_missing_but_falls_back(db):
    """只导入了考生这一年（2026）的一分一段表、没导入历史年份（2025）
    的，应该如实告知"这条候选没能做跨年换算"，但 diff 还是要能算出来
    （退化用原始位次），不能因为换算数据不全就干脆放弃整条候选的概率计算。"""
    province = "测试省RK3"
    _seed_score_rank(db, province, 2026, [(600, 100000), (601, 90000)])

    rank_scale = build_rank_scale_context(db, province, "物理类")
    group = _make_group("target", min_rank=1000)  # 历年记录是 2025 年，但一分一段表没有2025年数据
    group.school.province = province
    result = evaluate_group(group, _profile(province=province, rank=500), rank_scale)

    assert result.diff == 1000 - 500, "换算数据不全时应该退化用原始位次，diff照常算出来"
    assert result.rank_equivalent_note is not None
    assert "2025" in result.rank_equivalent_note and "未找到" in result.rank_equivalent_note


def test_audit_passed_candidates_sort_before_failed_ones():
    # audit 包含硬性规则未通过的候选（比如选科要求不满足）——这些不该跟
    # 通过的候选混在原始顺序里，真实数据下未通过的候选数量远大于通过的，
    # 混排会把"资格核验"面板里真正有用的通过候选淹没掉。
    groups = [
        _make_group("fail_subject", subjects_required=["生物"]),  # 考生没选生物，选科不满足
        _make_group("pass", min_rank=5000),
    ]
    _results, audit = search_candidates(groups, _profile())
    assert [c.group_id for c in audit] == ["pass", "fail_subject"]
    assert audit[0].all_hard_pass is True
    assert audit[1].all_hard_pass is False


def test_audit_within_passed_group_also_sorted_by_probability():
    groups = [
        _make_group("mid", min_rank=5000),
        _make_group("high", min_rank=8000),
        _make_group("fail_subject", subjects_required=["生物"]),
    ]
    _results, audit = search_candidates(groups, _profile())
    passed = [c for c in audit if c.all_hard_pass]
    assert [c.group_id for c in passed] == ["high", "mid"], "audit 里通过的候选也应该按位次差排序"


def test_latest_year_plan_only_row_falls_back_to_earlier_year_for_diff():
    # 真实撞到的 bug：某专业组最新年份（比如 2026）只是招生计划，还没有
    # 实际录取结果（min_rank=None），但更早的年份（2025/2024）有真实数据。
    # evaluate_group() 之前直接拿"最新年份"那一条算概率，min_rank 是 None
    # 就直接判定"算不出概率"，白白浪费掉更早年份明明可用的真实数据。
    school = School(id="g-school", name="g大学", city="测试市", province=PROVINCE, tags=[])
    group = AdmissionGroup(
        id="g", school_id=school.id, code=None, track="物理类",
        subjects_required=[], major_keywords=[], tuition=None, duration=None,
        why_recommend="", caution="", source_label="",
    )
    group.school = school
    group.records = [
        AdmissionRecord(group_id="g", year=2026, group_name="g", plan_count=6,
                         min_score=None, min_rank=None, source_url=None, fetched_at=None),
        AdmissionRecord(group_id="g", year=2025, group_name="g", plan_count=7,
                         min_score=429, min_rank=35877, source_url=None, fetched_at=None),
        AdmissionRecord(group_id="g", year=2024, group_name="g", plan_count=7,
                         min_score=427, min_rank=34110, source_url=None, fetched_at=None),
    ]
    results, _audit = search_candidates([group], _profile())
    assert len(results) == 1
    assert results[0].diff is not None, "2026 是纯计划行，应该退回用 2025 年的真实数据算位次差，而不是显示'待计算'"
    assert results[0].diff == 35877 - 5000


def _make_real_group(group_id: str, *, city: str, province_label: str, min_rank: int | None = 5000) -> AdmissionGroup:
    """跟 _make_group 类似，但用真实的中国城市名（geo.py 反查表里有的），
    province_label 是 School.province（招生目标省份，可能跟学校实际所在地不同）。
    """
    school = School(id=f"{group_id}-school", name=f"{group_id}大学", city=city, province=province_label, tags=[])
    group = AdmissionGroup(
        id=group_id, school_id=school.id, code=None, track="物理类",
        subjects_required=[], major_keywords=[], tuition=None, duration=None,
        why_recommend="", caution="", source_label="",
    )
    group.school = school
    group.records = [AdmissionRecord(
        group_id=group_id, year=2025, group_name=group_id, plan_count=10,
        min_score=600, min_rank=min_rank, source_url=None, fetched_at=None,
    )] if min_rank is not None else []
    return group


def test_home_province_only_excludes_school_physically_outside_target_province():
    # 真实撞到的情况：潍坊科技学院面向江苏招生（School.province="江苏"），
    # prov_match 硬规则通过，但它物理上在山东潍坊——用户勾选"只看省内院校"
    # 后应该被过滤掉。南京大学物理和招生目标都在江苏，应该保留。
    groups = [
        _make_real_group("weifang", city="潍坊", province_label="江苏"),
        _make_real_group("nanjing", city="南京", province_label="江苏"),
    ]
    profile = _profile(province="江苏", home_province_only=True)
    results, audit = search_candidates(groups, profile)
    assert [c.group_id for c in results] == ["nanjing"]
    weifang_audit = next(c for c in audit if c.group_id == "weifang")
    assert weifang_audit.all_hard_pass is False
    home_rule = next(r for r in weifang_audit.rules if r.name == "实际所在地（省内院校）")
    assert home_rule.passed is False


def test_home_province_only_defaults_off_and_unknown_city_is_lenient():
    # 默认不开启这个过滤——不传 home_province_only 时潍坊科技学院照常出现，
    # 不能因为加了新规则就意外改变了默认行为。
    groups = [_make_real_group("weifang", city="潍坊", province_label="江苏")]
    results, _audit = search_candidates(groups, _profile(province="江苏"))
    assert [c.group_id for c in results] == ["weifang"]

    # 反查表里没有的城市名（数据表覆盖不全）：开启过滤时也不应该被硬性排除，
    # 因为查不到属于"未知"，不能当成"不匹配"处理。
    groups2 = [_make_real_group("unknown_city", city="某个反查表里没有的县级市", province_label="江苏")]
    results2, _audit2 = search_candidates(groups2, _profile(province="江苏", home_province_only=True))
    assert [c.group_id for c in results2] == ["unknown_city"]
