"""
回测核心逻辑的自检（合成数据，不需要数据库）。回测本身产出的是一份报告，不是 pass/fail；
这里测的是「回测这把尺子本身是准的」：
1. 不偷看答案：把 T 年的记录改得面目全非，推荐结果不能有任何变化；
2. 稳定世界里保底档录取率 = 100%（规则没毛病时，回测不能冤枉它）；
3. 世界突然变难（T 年所有最低位次整体前移）时，回测必须能测出保底档录取率下降（尺子能报警）；
4. 没有 T 年记录的推荐项不参与判卷，只计 no_truth。
"""
import random

from app.models import AdmissionGroup, AdmissionRecord, School

from ..fixtures import make_group, make_record, make_school
from .core import apply_history_strategy, has_data, patched_ratios, rank_grid, run_year_track, snapshot_groups, summarize, truncate_before, truth_ranks


def _world(n=120, drift_2025=1.0, seed=7, years=(2023, 2024, 2025)):
    """n 个专业组，历年最低位次稳定在 base 附近（±2% 噪声），2025 整体乘 drift_2025。"""
    rnd = random.Random(seed)
    groups = []
    for i in range(n):
        base = int(800 * (1.07 ** i))  # 800 ~ 数十万，位次跨度大
        recs = []
        for y in years:
            v = base * rnd.uniform(0.98, 1.02) * (drift_2025 if y == 2025 else 1.0)
            recs.append(make_record(f"g{i}", y, min_rank=int(v)))
        groups.append(make_group(f"g{i}", make_school(f"s{i}", city="南京"), records=recs))
    return snapshot_groups(groups)


def test_truncate_hides_truth_year_and_later():
    g = _world(5)
    view = truncate_before(g, 2025)
    assert all(r.year < 2025 for x in view for r in x.records)
    assert truth_ranks(g, 2025), "完整数据里应该有 2025 的真实最低位次"


def test_no_leakage_poisoning_truth_year_does_not_change_picks():
    ranks = rank_grid(800, 60000, 12)
    clean = _world(120)
    poisoned = _world(120)
    for g in poisoned:
        for r in g.records:
            if r.year == 2025:
                r.min_rank = 1  # 把答案改成荒谬值；推荐不允许因此改变
    a = run_year_track(clean, 2025, "物理类", ranks, {})
    b = run_year_track(poisoned, 2025, "物理类", ranks, {})
    assert {k: v.picks for k, v in a.tier.items()} == {k: v.picks for k, v in b.tier.items()}, (
        "T 年记录被改动后推荐项数量变了——回测偷看了 T 年的数据"
    )


def test_stable_world_baowen_is_always_admitted():
    res = run_year_track(_world(150), 2025, "物理类", rank_grid(800, 80000, 30), {})
    rep = summarize([res])
    bao = rep["overall"]["tier_admit_rate"]["baowen"]
    assert bao["picks"] > 0
    assert bao["rate"] == 1.0, f"稳定世界里保底档应 100% 录取，实际 {bao}"
    assert rep["overall"]["plan_not_slid_rate"] == 1.0
    assert rep["overall"]["baowen_pick_pass"] and rep["overall"]["plan_not_slid_pass"]


def test_tiers_are_ordered_in_calibration_table():
    """校准表应单调：保底录取率 ≥ 稳妥 ≥ 冲刺。这是「分档有意义」的最低要求。"""
    rep = summarize([run_year_track(_world(150), 2025, "物理类", rank_grid(800, 80000, 40), {})])
    t = rep["overall"]["tier_admit_rate"]
    assert t["baowen"]["rate"] >= t["wen"]["rate"] >= t["chong"]["rate"]


def _near_threshold_world(truth_2025: int, n=12):
    """考生位次 5000；每个专业组 2023/2024 的最低位次都是 6200（diff/rank=0.24 → 保底），
    2025 的真实最低位次由 truth_2025 决定。"""
    groups = []
    for i in range(n):
        recs = [make_record(f"g{i}", 2023, min_rank=6200), make_record(f"g{i}", 2024, min_rank=6200),
                make_record(f"g{i}", 2025, min_rank=truth_2025)]
        groups.append(make_group(f"g{i}", make_school(f"s{i}", city="南京"), records=recs))
    return snapshot_groups(groups)


def test_backtest_passes_when_baowen_really_is_safe():
    rep = summarize([run_year_track(_near_threshold_world(6200), 2025, "物理类", [5000], {})])
    assert rep["overall"]["tier_admit_rate"]["baowen"]["rate"] == 1.0
    assert rep["overall"]["baowen_pick_pass"]


def test_backtest_detects_world_getting_harder():
    """2025 年竞争突然加剧：这些「保底」组的真实最低位次前移到 4000，考生位次 5000 其实录不上——
    回测必须测出保底档录取率 0、Pass Line 不通过，并给出失败样例。"""
    res = run_year_track(_near_threshold_world(4000), 2025, "物理类", [5000], {})
    rep = summarize([res])
    assert rep["overall"]["tier_admit_rate"]["baowen"]["rate"] == 0.0
    assert not rep["overall"]["baowen_pick_pass"] and not rep["overall"]["plan_not_slid_pass"]
    ex = rep["by_year_track"][0]["baowen_failure_examples"][0]
    assert ex["student_rank"] == 5000 and ex["truth_min_rank"] == 4000 and ex["history_min_ranks"] == {2024: 6200, 2023: 6200}


def test_group_without_truth_year_record_is_not_judged_but_counted():
    groups = _world(60)
    for g in groups[:30]:
        g.records = [r for r in g.records if r.year != 2025]  # 这些组 2025 年没招生记录
    res = run_year_track(groups, 2025, "物理类", rank_grid(800, 80000, 30), {})
    assert sum(t.no_truth for t in res.tier.values()) > 0


def test_has_data_requires_truth_year_and_earlier_history():
    g = _world(10)
    assert has_data(g, 2025, "物理类")
    assert not has_data(g, 2025, "历史类")          # 该科类没有任何组
    assert not has_data(g, 2023, "物理类")          # 2023 之前没有历史可用


def test_conservative_history_strategy_uses_strictest_recent_year():
    """复刻真实失败案例：2024 年偶然偏松（5946），2023 是 3399——只看最近一年（latest）会把它当保底，
    min2 取更严的那年（3399）就不会。"""
    recs = [make_record("g", 2023, min_rank=3399), make_record("g", 2024, min_rank=5946)]
    groups = snapshot_groups([make_group("g", make_school("s"), records=recs)])
    latest = apply_history_strategy(groups, "latest", {})[0]
    min2 = apply_history_strategy(groups, "min2", {})[0]
    assert {r.year: r.min_rank for r in latest.records} == {2023: 3399, 2024: 5946}
    assert {r.year: r.min_rank for r in min2.records} == {2023: 3399, 2024: 3399}
    # 原数据不被改写
    assert {r.year: r.min_rank for r in groups[0].records}[2024] == 5946


def test_min_strategy_converts_older_years_to_newest_scale():
    recs = [make_record("g", 2023, min_rank=1000), make_record("g", 2024, min_rank=1100)]
    groups = snapshot_groups([make_group("g", make_school("s"), records=recs)])
    out = apply_history_strategy(groups, "min2", {2023: 200000, 2024: 240000})[0]
    # 2023 的 1000 名折算到 2024 规模 = 1000*240000/200000 = 1200，比 1100 松，取更严的 1100
    assert {r.year: r.min_rank for r in out.records}[2024] == 1100


def test_patched_ratios_restore_and_change_tiering():
    from app.services import rules_engine
    before = rules_engine.BAOWEN_RATIO
    with patched_ratios(baowen=0.5):
        assert rules_engine.prob_category(diff=1200, rank=5000)[0] == "wen"   # 0.24 < 0.5 不再是保底
    assert rules_engine.BAOWEN_RATIO == before
    assert rules_engine.prob_category(diff=1200, rank=5000)[0] == "baowen"


def test_raising_baowen_ratio_makes_marginal_baowen_pick_disappear():
    groups = _near_threshold_world(4000)  # 保底阈值 0.20 时这些组被当保底并在 T 年失败
    with_default = run_year_track(groups, 2025, "物理类", [5000], {})
    stricter = run_year_track(groups, 2025, "物理类", [5000], {}, ratios=(0.5, None, None))
    assert with_default.tier["baowen"].picks > 0
    assert stricter.tier["baowen"].picks == 0


def test_single_year_cap_demotes_baowen_with_one_year_history_only():
    one_year = snapshot_groups([make_group(f"g{i}", make_school(f"s{i}", city="南京"),
                                           records=[make_record(f"g{i}", 2024, min_rank=6200), make_record(f"g{i}", 2025, min_rank=4000)])
                                for i in range(10)])
    plain = run_year_track(one_year, 2025, "物理类", [5000], {})
    capped = run_year_track(one_year, 2025, "物理类", [5000], {}, single_year_cap=True)
    assert plain.tier["baowen"].picks > 0
    assert capped.tier["baowen"].picks == 0, "只有1年历史的候选不该再被标成保底"


def test_single_year_cap_keeps_baowen_with_two_years_of_history():
    two_year = snapshot_groups([make_group(f"g{i}", make_school(f"s{i}", city="南京"),
                                           records=[make_record(f"g{i}", 2023, min_rank=6200), make_record(f"g{i}", 2024, min_rank=6200),
                                                    make_record(f"g{i}", 2025, min_rank=6000)]) for i in range(10)])
    capped = run_year_track(two_year, 2025, "物理类", [5000], {}, single_year_cap=True)
    assert capped.tier["baowen"].picks > 0


def test_drift_rows_and_summary():
    from .drift import drift_rows, summarize_drift
    groups = _world(60)
    rows = drift_rows(groups, 2025, "物理类", {2024: 200000, 2025: 220000})
    assert rows and all(0.5 < r["ratio"] < 1.5 for r in rows)  # 稳定世界里 ratio 应接近 1/1.1 的折算量级
    s = summarize_drift(rows)
    assert s[-1]["band"] == "全部" and s[-1]["n"] == len(rows)
