"""Slot F1 评测入口。在 backend/ 目录、激活 venv、backend/.env 里配好 GEMINI_API_KEY 后运行：

    python -m tests.eval.slots.run_slot_eval            # 真实调用 Gemini（有缓存，重跑不重复调用）
    python -m tests.eval.slots.run_slot_eval --no-cache # 忽略旧缓存、全部重新调用，并刷新缓存
    python -m tests.eval.slots.run_slot_eval --per-minute 8   # 配额更紧时调低

结果打印成表，同时写入 tests/eval/slots/reports/slot_f1.json。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

os.environ.setdefault("JWT_SECRET", "eval-only")

from .cases import CASES  # noqa: E402
from .metrics import SCORED_SLOTS, summarize  # noqa: E402
from .runner import rate_limited, run_all, with_cache  # noqa: E402

REPORT_DIR = Path(__file__).parent / "reports"
PASS_LINE_F1 = 0.98  # 需求文档：Slot F1-Score > 98%


def _fmt(x) -> str:
    return "  -  " if x is None else f"{x:.3f}"


def print_report(rep) -> dict:
    o = rep.overall
    print(f"\n=== Slot F1（{rep.n_cases} 条用例，micro）===")
    print(f"P={_fmt(o.precision())}  R={_fmt(o.recall())}  F1={_fmt(o.f1())}  "
          f"整条全对 {rep.n_exact}/{rep.n_cases}  追问遵从 {rep.followup_ok}/{rep.followup_total}  "
          f"线 >{PASS_LINE_F1}：{'达标' if (o.f1() or 0) > PASS_LINE_F1 else '未达标'}")
    print(f"\n{'槽位':<22}{'TP':>4}{'FP':>4}{'FN':>4}  {'P':>6} {'R':>6} {'F1':>6}")
    for s in SCORED_SLOTS:
        c = rep.by_slot[s]
        print(f"{s:<22}{c.tp:>4}{c.fp:>4}{c.fn:>4}  {_fmt(c.precision()):>6} {_fmt(c.recall()):>6} {_fmt(c.f1()):>6}")
    print(f"\n{'类型':<12}{'TP':>4}{'FP':>4}{'FN':>4}  {'F1':>6}")
    for t, c in rep.by_tag.items():
        print(f"{t:<12}{c.tp:>4}{c.fp:>4}{c.fn:>4}  {_fmt(c.f1()):>6}")
    bad = [r for r in rep.rows if not r["exact"]]
    if bad:
        print(f"\n未全对的用例（{len(bad)}）：")
        for r in bad:
            print(f"- {r['id']}" + (f"  调用失败：{r['error']}" if r["error"] else ""))
            for e in r["errors"]:
                print(f"    {e['slot']}: 期望 {e['expected']!r}，实际 {e['got']!r}")
    missed = [(r["id"], f) for r in rep.rows for f in r["followup_failed"]]
    if missed:
        print(f"\n必填项不齐却没追问（{len(missed)}）：")
        for cid, f in missed:
            print(f"- {cid} 第 {f['turn'] + 1} 轮回复：{f['reply']!r}")
    return {
        "n_cases": rep.n_cases, "n_exact": rep.n_exact, "pass_line": PASS_LINE_F1,
        "micro": {"precision": o.precision(), "recall": o.recall(), "f1": o.f1(), "tp": o.tp, "fp": o.fp, "fn": o.fn},
        "followup": {"ok": rep.followup_ok, "total": rep.followup_total},
        "by_slot": {s: {"tp": c.tp, "fp": c.fp, "fn": c.fn, "f1": c.f1()} for s, c in rep.by_slot.items()},
        "by_tag": {t: {"tp": c.tp, "fp": c.fp, "fn": c.fn, "f1": c.f1()} for t, c in rep.by_tag.items()},
        "failures": bad,
        "followup_failed": [{"id": c, **f} for c, f in missed],
    }


async def main_async(args) -> None:
    from app.services.agent_client import call_agent  # 延迟导入：没 Key 时也能 --help

    limited = rate_limited(call_agent, per_minute=args.per_minute)
    fn = with_cache(limited, refresh=args.no_cache)  # --no-cache：忽略旧缓存、重新调用并刷新缓存文件
    results = await run_all(CASES, fn, concurrency=args.concurrency)
    out = print_report(summarize(results))
    REPORT_DIR.mkdir(exist_ok=True)
    (REPORT_DIR / "slot_f1.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"\n已写入 {REPORT_DIR / 'slot_f1.json'}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--per-minute", type=int, default=12, help="每分钟最多发起多少次 Gemini 请求（免费档上限 15）")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
