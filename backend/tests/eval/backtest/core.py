"""
Layer 5：历史回测的核心逻辑（纯函数，不依赖数据库，可单测）。

思路：「假装回到 T 年」——
1. 只保留每个专业组 year < T 的历史记录（防止"偷看答案"），用生产里同一套规则引擎
   （evaluate_group → 冲稳保分档 → curate 精选 10 所）给一批虚拟考生生成推荐方案；
2. 用 T 年的真实最低位次判卷：考生位次 ≤ 该组 T 年最低位次，即视为会被录取；
3. 统计各档录取率（校准表）、保底档录取率（红线 >99.5%）、整份方案不滑档率。

判卷只看「位次 ≤ 当年最低位次」，不模拟平行志愿的先后顺序、当年扩招缩招、专业组合并改名，
所以它衡量的是「分档规则（0.20/0.05/-0.10 阈值 + 跨年位次折算）对不对」，不是真实录取概率。
没有 T 年录取记录的推荐项（新专业组、停招）不参与判卷，单独计数，不算成功也不算失败。
"""
from __future__ import annotations

import math
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace

from app.schemas.candidate import ProfileIn
from app.services.candidate_curation import curate
from app.services import rules_engine
from app.services.rules_engine import RankScaleContext, search_candidates

# 给虚拟考生一份"什么选科都满足"的选科，目的是只测分档规则，不让选科硬规则把候选筛掉
ALL_SUBJECTS = ["物理", "历史", "化学", "生物", "思想政治", "地理"]
PASS_LINE_BAOWEN = 0.95  # 产品决定：原需求 >99.5%，数据仅 3 年、单组年际波动大，定为 95%（见 README）
RANK_BANDS = [(0, 2000, "<2千"), (2000, 10000, "2千-1万"), (10000, 30000, "1万-3万"), (30000, 10**9, ">3万")]


def _copy_columns(obj, **extra) -> SimpleNamespace:
    ns = SimpleNamespace(**{c.key: getattr(obj, c.key) for c in obj.__table__.columns})
    for k, v in extra.items():
        setattr(ns, k, v)
    return ns


def snapshot_groups(orm_groups) -> list[SimpleNamespace]:
    """把 ORM 的 AdmissionGroup（含 school、records）拷成纯数据对象，之后回测随便改记录也不会碰到
    数据库 session。evaluate_group 只读属性，鸭子类型即可。"""
    out = []
    for g in orm_groups:
        school = _copy_columns(g.school)
        records = [_copy_columns(r) for r in g.records]
        out.append(_copy_columns(g, school=school, records=records))
    return out


def truncate_before(groups: list, year: int) -> list[SimpleNamespace]:
    """只保留 year < T 的历史记录，其余字段共享。"""
    out = []
    for g in groups:
        ns = SimpleNamespace(**vars(g))
        ns.records = [r for r in g.records if r.year < year]
        out.append(ns)
    return out


def truth_ranks(groups: list, year: int) -> dict[str, int]:
    """T 年每个专业组真实录取的最低位次（只含有该年 min_rank 的组）。"""
    out = {}
    for g in groups:
        for r in g.records:
            if r.year == year and r.min_rank is not None:
                out[g.id] = r.min_rank
    return out


HISTORY_STRATEGIES = ("latest", "min2", "min3")


def apply_history_strategy(groups: list, strategy: str, totals_by_year: dict[int, int]) -> list:
    """【回测实验，不是线上行为】线上现在只看「最近一个有最低位次的年份」（latest）。
    min2/min3：取最近 2/3 个有数据年份里**最靠前（最严）**的最低位次，先折算到最近那一年的报考规模，
    再写进最近那一年的记录——只有这条候选的最近一年被「改写」，其余字段不动。
    用来回答：单年数据偶然偏松（某一年冷门）是不是保底档失败的主因。"""
    if strategy == "latest":
        return groups
    k = int(strategy[3:])
    out = []
    for g in groups:
        ns = SimpleNamespace(**vars(g))
        ranked = sorted((r for r in g.records if r.min_rank is not None), key=lambda r: r.year, reverse=True)[:k]
        if len(ranked) >= 2:
            newest = ranked[0]
            tot_new = totals_by_year.get(newest.year)

            def in_newest_scale(r):
                tot = totals_by_year.get(r.year)
                return r.min_rank * tot_new / tot if tot and tot_new else r.min_rank

            strict = round(min(in_newest_scale(r) for r in ranked))
            ns.records = [SimpleNamespace(**{**vars(r), "min_rank": strict}) if r is newest else r for r in g.records]
        out.append(ns)
    return out


def cap_single_year_history(c, year: int):
    """【回测实验】只有一个年份最低位次的候选，最高只能算「稳妥」，不允许标成「保底」——
    单年数据可能是偶然偏松的那一年，见回测里吉林大学 36407→9941 这类案例。"""
    if c.category == "baowen" and sum(1 for h in c.history if h.year < year and h.min_rank is not None) < 2:
        return c.model_copy(update={"category": "wen", "category_label": "稳妥"})
    return c


@contextmanager
def patched_ratios(baowen: float | None = None, wen: float | None = None, chong: float | None = None):
    """临时改 rules_engine 的三个分档阈值（只在 with 块内生效），用来扫阈值，不动线上常量。"""
    old = (rules_engine.BAOWEN_RATIO, rules_engine.WEN_RATIO, rules_engine.CHONG_RATIO)
    try:
        if baowen is not None: rules_engine.BAOWEN_RATIO = baowen
        if wen is not None: rules_engine.WEN_RATIO = wen
        if chong is not None: rules_engine.CHONG_RATIO = chong
        yield
    finally:
        rules_engine.BAOWEN_RATIO, rules_engine.WEN_RATIO, rules_engine.CHONG_RATIO = old


def rank_grid(lo: int, hi: int, n: int) -> list[int]:
    """在 [lo, hi] 上对数等距取 n 个位次（高分段密、低分段疏，贴近"位次越靠前越敏感"）。"""
    if n <= 1:
        return [lo]
    step = math.log(hi / lo) / (n - 1)
    return sorted({round(lo * math.exp(step * i)) for i in range(n)})


@dataclass
class Tally:
    picks: int = 0       # 参与判卷的推荐项数
    admitted: int = 0    # 其中会被录取的
    no_truth: int = 0    # T 年没有录取记录、无法判卷的推荐项

    def rate(self) -> float | None:
        return self.admitted / self.picks if self.picks else None


@dataclass
class YearTrackResult:
    year: int
    track: str
    students: int = 0
    students_judged: int = 0          # 方案里至少有一项可判卷的考生数
    plan_not_slid: int = 0            # 方案里至少一项会被录取的考生数
    baowen_students_with_pick: int = 0
    baowen_students_all_admitted: int = 0
    full_three_tiers: int = 0         # 三档都有推荐的考生数（覆盖率）
    tier: dict[str, Tally] = field(default_factory=lambda: defaultdict(Tally))
    band: dict[str, dict[str, Tally]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(Tally)))
    baowen_failures: list[dict] = field(default_factory=list)


def _band_of(rank: int) -> str:
    return next(label for lo, hi, label in RANK_BANDS if lo <= rank < hi)


def run_year_track(
    groups_full: list, year: int, track: str, ranks: list[int], totals_by_year: dict[int, int],
    *, use_rank_scale: bool = True, province: str = "江苏", max_failure_examples: int = 15,
    history_strategy: str = "latest", ratios: tuple[float | None, float | None, float | None] = (None, None, None),
    single_year_cap: bool = False,
) -> YearTrackResult:
    visible = apply_history_strategy(
        [g for g in truncate_before(groups_full, year) if g.track == track], history_strategy, totals_by_year,
    )
    truth = truth_ranks(groups_full, year)
    scale = RankScaleContext(current_year=year, totals_by_year=totals_by_year) if use_rank_scale else None
    res = YearTrackResult(year=year, track=track)

    for r in ranks:
        profile = ProfileIn(province=province, track=track, subjects=ALL_SUBJECTS, rank=r)
        with patched_ratios(*ratios):
            results, _audit = search_candidates(visible, profile, rank_scale=scale)
        if single_year_cap:
            results = [cap_single_year_history(c, year) for c in results]
        curated = curate(results)
        res.students += 1
        band = _band_of(r)
        picks_judged = admitted_any = False
        tiers_present = 0
        bao_picks = bao_ok = 0
        for g_out in curated.groups:
            if g_out.candidates:
                tiers_present += 1
            for c in g_out.candidates:
                t = truth.get(c.group_id)
                if t is None:
                    res.tier[g_out.category].no_truth += 1
                    continue
                ok = r <= t
                for tally in (res.tier[g_out.category], res.band[band][g_out.category]):
                    tally.picks += 1
                    tally.admitted += ok
                picks_judged = True
                admitted_any = admitted_any or ok
                if g_out.category == "baowen":
                    bao_picks += 1
                    bao_ok += ok
                    if not ok and len(res.baowen_failures) < max_failure_examples:
                        res.baowen_failures.append({
                            "student_rank": r, "group_id": c.group_id, "school": c.university,
                            "truth_min_rank": t, "history_min_ranks": {h.year: h.min_rank for h in c.history if h.year < year},
                            "diff_used": c.diff,
                        })
        if tiers_present == 3:
            res.full_three_tiers += 1
        if picks_judged:
            res.students_judged += 1
            res.plan_not_slid += admitted_any
        if bao_picks:
            res.baowen_students_with_pick += 1
            res.baowen_students_all_admitted += (bao_ok == bao_picks)
    return res


def summarize(results: list[YearTrackResult]) -> dict:
    """把多个 (年, 科类) 结果汇总成报告字典，并判断是否达到 Pass Line。"""
    def pct(x):
        return None if x is None else round(x, 4)

    per = []
    agg_tier: dict[str, Tally] = defaultdict(Tally)
    tot_judged = tot_slid_ok = tot_students = tot_full = 0
    for r in results:
        per.append({
            "year": r.year, "track": r.track, "students": r.students, "judged": r.students_judged,
            "plan_not_slid_rate": pct(r.plan_not_slid / r.students_judged) if r.students_judged else None,
            "three_tier_coverage": pct(r.full_three_tiers / r.students) if r.students else None,
            "tier_admit_rate": {k: {"picks": v.picks, "rate": pct(v.rate()), "no_truth": v.no_truth} for k, v in sorted(r.tier.items())},
            "baowen_by_rank_band": {b: {"picks": d["baowen"].picks, "rate": pct(d["baowen"].rate())}
                                   for b, d in sorted(r.band.items()) if "baowen" in d},
            "baowen_failure_examples": r.baowen_failures,
        })
        for k, v in r.tier.items():
            a = agg_tier[k]
            a.picks += v.picks; a.admitted += v.admitted; a.no_truth += v.no_truth
        tot_judged += r.students_judged; tot_slid_ok += r.plan_not_slid
        tot_students += r.students; tot_full += r.full_three_tiers
    bao = agg_tier.get("baowen", Tally())
    not_slid = tot_slid_ok / tot_judged if tot_judged else None
    return {
        "overall": {
            "tier_admit_rate": {k: {"picks": v.picks, "rate": pct(v.rate())} for k, v in sorted(agg_tier.items())},
            "plan_not_slid_rate": pct(not_slid),
            "three_tier_coverage": pct(tot_full / tot_students) if tot_students else None,
            "pass_line": PASS_LINE_BAOWEN,
            "baowen_pick_pass": bao.rate() is not None and bao.rate() > PASS_LINE_BAOWEN,
            "plan_not_slid_pass": not_slid is not None and not_slid > PASS_LINE_BAOWEN,
        },
        "by_year_track": per,
    }


def has_data(groups: list, year: int, track: str) -> bool:
    """这个 (年, 科类) 能不能回测：该科类既有 T 年的真实录取位次、又有 T 之前的历史记录。"""
    truth = truth_ranks([g for g in groups if g.track == track], year)
    if not truth:
        return False
    return any(r.year < year and r.min_rank is not None for g in groups if g.track == track and g.id in truth for r in g.records)
