"""
历史回测入口（连你本机数据库，只读）。在 backend/ 目录、激活 venv、Postgres 在运行时：

    python -m tests.eval.backtest.run_backtest                       # 2025/2024/2023 各做一遍
    python -m tests.eval.backtest.run_backtest --years 2025 --n-ranks 60
    python -m tests.eval.backtest.run_backtest --no-rank-scale       # 对照：不做跨年位次折算
    python -m tests.eval.backtest.run_backtest --history-strategy min3 --baowen-ratio 0.3   # 实验：更保守的历史口径/阈值
    python -m tests.eval.backtest.run_backtest --sweep               # 扫 历史口径 × 保底阈值，打印对比表

结果打印成表，同时写 tests/eval/backtest/reports/backtest_<tag>.json。
库里缺某年的一分一段表时，该年的跨年折算自动退化（和线上一致），报告里会列出每年可用的总人数。
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

os.environ.setdefault("JWT_SECRET", "eval-only")

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import selectinload  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import AdmissionGroup  # noqa: E402
from app.services.rank_lookup import get_total_candidates_by_year  # noqa: E402

from .core import HISTORY_STRATEGIES, has_data, rank_grid, run_year_track, snapshot_groups, summarize  # noqa: E402

REPORT_DIR = Path(__file__).parent / "reports"
TRACKS = ["物理类", "历史类"]


def load_groups(province: str):
    with SessionLocal() as db:
        rows = db.scalars(
            select(AdmissionGroup).options(selectinload(AdmissionGroup.records), selectinload(AdmissionGroup.school))
        ).all()
        groups = snapshot_groups(rows)
        totals = {t: get_total_candidates_by_year(db, province, t) for t in TRACKS}
    return [g for g in groups if g.school.province == province], totals


def print_report(rep: dict) -> None:
    o = rep["overall"]
    print("\n=== 历史回测（汇总：所有验证年 × 科类）===")
    names = {"baowen": "保底", "wen": "稳妥", "chong": "冲刺"}
    for k in ("baowen", "wen", "chong"):
        v = o["tier_admit_rate"].get(k)
        if v:
            print(f"  {names[k]}档：推荐项 {v['picks']:>7}，T年实际录取率 {v['rate']:.2%}")
    print(f"  整份方案不滑档率：{o['plan_not_slid_rate']:.2%}   三档覆盖率：{o['three_tier_coverage']:.2%}")
    print(f"  Pass Line（>{o['pass_line']:.1%}）：保底档录取率 {'通过' if o['baowen_pick_pass'] else '未通过'}；"
          f"不滑档率 {'通过' if o['plan_not_slid_pass'] else '未通过'}")
    print("\n--- 分年份/科类 ---")
    for r in rep["by_year_track"]:
        t = r["tier_admit_rate"]
        rates = "  ".join(f"{names.get(k, k)} {v['rate']:.1%}({v['picks']})" for k, v in t.items() if v["rate"] is not None)
        print(f"{r['year']} {r['track']}  考生{r['students']:>3}  {rates}  不滑档 "
              f"{(r['plan_not_slid_rate'] or 0):.1%}")
        bands = "  ".join(f"{b}:{v['rate']:.1%}" for b, v in r["baowen_by_rank_band"].items() if v["rate"] is not None)
        if bands:
            print(f"      保底档按位次段：{bands}")


def run_config(groups, totals, years_in_data, args, ranks, *, history_strategy, baowen_ratio, use_rank_scale, verbose=True,
               single_year_cap=False):
    results = []
    for year in args.years:
        if not any(y < year for y in years_in_data) or year not in years_in_data:
            if verbose:
                print(f"跳过 {year}：库里没有该年真实录取位次，或没有更早年份的历史数据可用")
            continue
        for track in TRACKS:
            if not has_data(groups, year, track):
                if verbose:
                    print(f"  跳过 {year} {track}：该科类没有可回测的数据")
                continue
            results.append(run_year_track(
                groups, year, track, ranks, totals[track], use_rank_scale=use_rank_scale, province=args.province,
                history_strategy=history_strategy, ratios=(baowen_ratio, None, None), single_year_cap=single_year_cap,
            ))
            if verbose:
                print(f"  完成 {year} {track}")
    return summarize(results)


def sweep(groups, totals, years_in_data, args, ranks) -> None:
    print("\n=== 扫描：历史口径 × 单年数据封顶 × 保底阈值（BAOWEN_RATIO）===")
    print("口径：latest=线上现状（最近一年）；min2=最近2年取最严（库里最多2年历史，min3 与 min2 相同故不列）")
    print("封顶：只有1年最低位次的候选最高只能算「稳妥」，不允许当保底")
    print(f"{'口径':<7}{'封顶':>4}{'保底阈值':>8}  {'保底录取率':>10}{'(推荐项)':>8} {'稳妥':>7} {'冲刺':>7} {'不滑档':>8} {'三档覆盖':>8}  达标")
    rows = []
    for strat in ("latest", "min2"):
        for cap in (False, True):
            for br in (0.20, 0.30, 0.40):
                rep = run_config(groups, totals, years_in_data, args, ranks, history_strategy=strat, baowen_ratio=br,
                                 use_rank_scale=not args.no_rank_scale, verbose=False, single_year_cap=cap)
                o = rep["overall"]; t = o["tier_admit_rate"]
                f = lambda k: f"{t[k]['rate']:.1%}" if k in t and t[k]["rate"] is not None else "—"
                n = t.get("baowen", {}).get("picks", 0)
                ok = "是" if o["baowen_pick_pass"] and o["plan_not_slid_pass"] else "否"
                print(f"{strat:<7}{'是' if cap else '否':>4}{br:>8.2f}  {f('baowen'):>10}{n:>8} {f('wen'):>7} {f('chong'):>7} "
                      f"{(o['plan_not_slid_rate'] or 0):>8.2%} {(o['three_tier_coverage'] or 0):>8.1%}  {ok}")
                rows.append({"history_strategy": strat, "single_year_cap": cap, "baowen_ratio": br, "overall": o})
    REPORT_DIR.mkdir(exist_ok=True)
    (REPORT_DIR / "backtest_sweep.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, nargs="+", default=[2025, 2024, 2023])
    ap.add_argument("--n-ranks", type=int, default=40, help="每个(年,科类)虚拟考生数，位次对数等距")
    ap.add_argument("--rank-lo", type=int, default=500)
    ap.add_argument("--rank-hi", type=int, default=100000)
    ap.add_argument("--no-rank-scale", action="store_true")
    ap.add_argument("--history-strategy", choices=HISTORY_STRATEGIES, default="latest")
    ap.add_argument("--baowen-ratio", type=float, default=None, help="覆盖保底阈值（默认用线上常量）")
    ap.add_argument("--single-year-cap", action="store_true", help="实验：只有1年历史的候选不得标为保底")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--province", default="江苏")
    ap.add_argument("--tag", default=None)
    args = ap.parse_args()

    t0 = time.time()
    groups, totals = load_groups(args.province)
    years_in_data = sorted({r.year for g in groups for r in g.records if r.min_rank is not None})
    print(f"载入 {len(groups)} 个专业组；有真实最低位次的年份：{years_in_data}；一分一段表总人数：{totals}")
    ranks = rank_grid(args.rank_lo, args.rank_hi, args.n_ranks)

    if args.sweep:
        sweep(groups, totals, years_in_data, args, ranks)
        print(f"\n用时 {time.time() - t0:.0f}s")
        return

    rep = run_config(groups, totals, years_in_data, args, ranks, history_strategy=args.history_strategy,
                     baowen_ratio=args.baowen_ratio, use_rank_scale=not args.no_rank_scale,
                     single_year_cap=args.single_year_cap)
    rep["config"] = {"years": args.years, "n_ranks": args.n_ranks, "rank_range": [args.rank_lo, args.rank_hi],
                     "rank_scale": not args.no_rank_scale, "history_strategy": args.history_strategy, "single_year_cap": args.single_year_cap,
                     "baowen_ratio": args.baowen_ratio, "totals_by_year": totals,
                     "groups": len(groups), "years_in_data": years_in_data}
    print_report(rep)
    REPORT_DIR.mkdir(exist_ok=True)
    tag = args.tag or "_".join(filter(None, [
        "noscale" if args.no_rank_scale else "scale", args.history_strategy if args.history_strategy != "latest" else None, "cap" if args.single_year_cap else None,
        f"b{args.baowen_ratio}" if args.baowen_ratio else None]))
    (REPORT_DIR / f"backtest_{tag}.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
