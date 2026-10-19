"""
逐年漂移分析：同一个专业组，T-1 年的最低位次折算到 T 年规模后，跟 T 年真实最低位次差多少？

回测回答「分档规则合不合格」，这里回答「为什么」：
- ratio = T 年真实最低位次 ÷ （T-1 年最低位次 × 总人数比例折算到 T 年规模）。理想世界里 ≈ 1；
  <1 表示该组 T 年比预期更难考（更靠前）——这正是保底档失败的来源；
- 按「折算后历史位次」分段看：若所有分段中位数都明显偏离 1，说明跨年折算有系统性偏差；
  若中位数≈1 但下尾（p10）很低，说明主要是个别专业组的随机大幅波动，调阈值解决不了，要靠多年取严、单年封顶等规则；
- 同时给出 ratio<0.8、<0.7 的占比：保底阈值 0.20 对应「历史位次比考生位次宽松 20%」，
  占比越高，需要的安全边际越大。

    python -m tests.eval.backtest.drift
"""
from __future__ import annotations

import argparse
import os
import statistics

os.environ.setdefault("JWT_SECRET", "eval-only")

BANDS = [(0, 2000, "<2千"), (2000, 10000, "2千-1万"), (10000, 30000, "1万-3万"), (30000, 10**9, ">3万")]


def drift_rows(groups: list, year: int, track: str, totals_by_year: dict[int, int]) -> list[dict]:
    """每个同时有 T-1 年和 T 年最低位次的专业组一行。"""
    out = []
    tot_t, tot_prev = totals_by_year.get(year), totals_by_year.get(year - 1)
    scale = (tot_t / tot_prev) if tot_t and tot_prev else 1.0
    for g in groups:
        if g.track != track:
            continue
        recs = {r.year: r.min_rank for r in g.records if r.min_rank is not None}
        if year in recs and (year - 1) in recs:
            prev_scaled = recs[year - 1] * scale
            out.append({"group_id": g.id, "school": g.school.name, "prev_scaled": prev_scaled,
                        "truth": recs[year], "ratio": recs[year] / prev_scaled,
                        "years_of_history": sum(1 for y in recs if y < year)})
    return out


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def summarize_drift(rows: list[dict]) -> list[dict]:
    result = []
    for lo, hi, label in BANDS + [(0, 10**9, "全部")]:
        sel = [r["ratio"] for r in rows if lo <= r["prev_scaled"] < hi]
        if len(sel) < 5:
            continue
        result.append({
            "band": label, "n": len(sel), "median": round(statistics.median(sel), 3), "p10": round(_pct(sel, 0.10), 3),
            "p05": round(_pct(sel, 0.05), 3),
            "share_lt_0.8": round(sum(x < 0.8 for x in sel) / len(sel), 3),
            "share_lt_0.7": round(sum(x < 0.7 for x in sel) / len(sel), 3),
        })
    return result


def main() -> None:
    from .run_backtest import TRACKS, load_groups

    ap = argparse.ArgumentParser()
    ap.add_argument("--province", default="江苏")
    args = ap.parse_args()
    groups, totals = load_groups(args.province)
    print(f"一分一段表（本科上线人数）：{totals}")
    print("ratio = T年真实最低位次 ÷ (T-1年最低位次×规模折算)；≈1 为理想，<1 表示该组变难（保底会失败）\n")
    for track in TRACKS:
        for year in (2025, 2024, 2023):
            rows = drift_rows(groups, year, track, totals[track])
            if not rows:
                continue
            print(f"--- {year} 年 vs {year - 1} 年 · {track}（{len(rows)} 个专业组）---")
            print(f"{'历史位次段':<8}{'组数':>6}{'中位数':>8}{'p10':>7}{'p05':>7}{'<0.8占比':>9}{'<0.7占比':>9}")
            for r in summarize_drift(rows):
                print(f"{r['band']:<8}{r['n']:>6}{r['median']:>8}{r['p10']:>7}{r['p05']:>7}{r['share_lt_0.8']:>9.1%}{r['share_lt_0.7']:>9.1%}")
            print()


if __name__ == "__main__":
    main()
