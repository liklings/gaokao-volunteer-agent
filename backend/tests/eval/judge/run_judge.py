"""LLM-as-Judge 评测入口。在 backend/ 目录、激活 venv、backend/.env 里配好 GEMINI_API_KEY 后运行：

    python -m tests.eval.judge.run_judge --target calibration   # 先校准评审员（约 17 条，必须先过这一关）
    python -m tests.eval.judge.run_judge --target profile       # 评 Profile Agent 的追问回复（默认抽 80 轮）
    python -m tests.eval.judge.run_judge --target advisor       # 真实跑 8 个推荐场景并评分（需要本机库里的江苏数据）
    python -m tests.eval.judge.run_judge --target all

    --limit N          profile 目标抽样条数（0=全部）
    --per-minute N     每分钟最多多少次 Gemini 请求（默认 12，免费档上限 15）
    --refresh-advisor  重新真实运行推荐场景（默认复用 .cache 里上次的运行结果）
    --gap SECONDS      推荐场景之间的间隔（默认 20）
    环境变量 JUDGE_MODEL 可以指定评审模型（建议与被评模型不同，避免自我偏好）。

评审响应缓存在 judge/.cache；改了评分维度/prompt 后要删掉缓存，否则读到的是旧评分。
报告写入 judge/reports/judge_<target>.json。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

os.environ.setdefault("JWT_SECRET", "eval-only")

from ..ratelimit import json_cached, rate_limited  # noqa: E402
from .calibration import CALIBRATION_MIN_AGREEMENT, GOLD, agreement  # noqa: E402
from .judge import aggregate, call_gemini_judge, judge_all, judge_model_name  # noqa: E402
from .rubrics import PASS_SCORE  # noqa: E402

HERE = Path(__file__).parent
REPORT_DIR = HERE / "reports"
CACHE_DIR = HERE / ".cache"
PROFILE_MIN_PASS = 0.90    # 建议线（产品可调）：Profile 回复每个维度合格率
ADVISOR_MIN_PASS = 0.80    # 建议线（产品可调）：推荐回复每个维度合格率（样本只有 8 条，仅作参考）


def _fmt(x) -> str:
    return "  -  " if x is None else f"{x:.2f}"


def print_aggregate(title: str, agg: dict, min_pass: float | None) -> None:
    print(f"\n=== {title}（{agg['n_items']} 条，评审出错 {agg['n_error']}；合格 = 分数≥{PASS_SCORE}）===")
    print(f"{'维度':<18}{'n':>4}{'均分':>7}{'合格率':>8}")
    for k, s in agg["by_criterion"].items():
        flag = "" if min_pass is None or s["pass_rate"] is None else ("  ✓" if s["pass_rate"] >= min_pass else "  ✗ 低于建议线")
        print(f"{k:<18}{s['n']:>4}{_fmt(s['mean']):>7}{_fmt(s['pass_rate']):>8}{flag}")


def print_worst(verdicts, items, n: int = 8) -> list[dict]:
    by_id = {i.id: i for i in items}
    rows = []
    for v in verdicts:
        if v.error:
            rows.append({"id": v.item_id, "score": 0, "key": "ERROR", "reason": v.error, "reply": by_id[v.item_id].reply})
            continue
        for k, s in v.scores.items():
            if s["applicable"] and s["score"] < PASS_SCORE:
                rows.append({"id": v.item_id, "score": s["score"], "key": k, "reason": s["reason"], "reply": by_id[v.item_id].reply})
    rows.sort(key=lambda r: r["score"])
    if rows:
        print(f"\n不合格项（共 {len(rows)} 处，显示最低的 {min(n, len(rows))} 处）：")
        for r in rows[:n]:
            print(f"- {r['id']} [{r['key']}={r['score']}] {r['reason']}\n    回复：{r['reply'][:120]!r}")
    return rows


async def run_calibration(judge_fn) -> dict:
    items = [g[0] for g in GOLD]
    verdicts = await judge_all(items, judge_fn)
    rep = agreement(verdicts)
    print(f"\n=== 评审员校准（人工标注 {rep['labels']} 个判断；评审模型 {judge_model_name()}）===")
    print(f"一致率 {rep['agreement']}（要求 ≥{CALIBRATION_MIN_AGREEMENT}）  误放行 {rep['false_pass']}（要求 0）  误杀 {rep['false_fail']}  出错 {rep['errors']}")
    print("评审员可信：" + ("是" if rep["reliable"] else "否——下面的评分只能当参考，先调评分维度/换评审模型"))
    for d in rep["disagreements"]:
        print(f"- {d['item']} [{d['key']}] 人工={'合格' if d['human'] else '不合格'} 评审={d['judge']}：{d.get('reason')}")
    return rep


def stale_cache_warning(cache_path: Path) -> str | None:
    """Profile 回复来自 Slot F1 的 Agent 响应缓存。若缓存比提示词文件旧，说明评的是改提示词之前的回复。"""
    prompt_file = Path(__file__).resolve().parents[3] / "app" / "services" / "agent_client.py"
    if cache_path.exists() and prompt_file.exists() and cache_path.stat().st_mtime < prompt_file.stat().st_mtime:
        return ("⚠ 警告：Agent 响应缓存（slots/.cache/agent_responses.json）比 Profile 提示词（agent_client.py）旧——\n"
                "  下面评的是改提示词之前的回复，不能用来判断新提示词。请先运行：\n"
                "  python -m tests.eval.slots.run_slot_eval --no-cache")
    return None


async def run_profile(judge_fn, args) -> dict:
    from app.services.agent_client import call_agent

    from ..slots.runner import CACHE_PATH, with_cache
    from .targets import profile_items

    agent_fn = with_cache(rate_limited(call_agent, per_minute=args.per_minute), CACHE_PATH)  # 与 Slot F1 共用缓存
    warn = stale_cache_warning(CACHE_PATH)
    if warn:
        print("\n" + warn)
    items, n_total = await profile_items(agent_fn, limit=args.limit)
    print(f"\nProfile 回复：共 {n_total} 轮，本次评审 {len(items)} 轮")
    verdicts = await judge_all(items, judge_fn)
    agg = aggregate(verdicts)
    print_aggregate("Profile Agent 回复质量", agg, PROFILE_MIN_PASS)
    worst = print_worst(verdicts, items)
    return {"n_total_turns": n_total, "aggregate": agg, "min_pass_suggested": PROFILE_MIN_PASS, "worst": worst}


async def run_advisor(judge_fn, args) -> dict:
    from .targets import advisor_item_from_run, run_advisor_scenarios

    cache = CACHE_DIR / "advisor_runs.json"
    if cache.exists() and not args.refresh_advisor:
        runs = json.loads(cache.read_text())
        print(f"\n复用上次的推荐场景运行结果（{len(runs)} 个；--refresh-advisor 可重新运行）")
    else:
        runs = await run_advisor_scenarios(gap_seconds=args.gap)
        CACHE_DIR.mkdir(exist_ok=True)
        cache.write_text(json.dumps(runs, ensure_ascii=False))
    ok_runs = [r for r in runs if not r.get("error") and r["reply"]]
    for r in runs:
        if r.get("error"):
            print(f"- 场景 {r['name']} 运行失败：{r['error'][:200]}")
    items = [advisor_item_from_run(r["name"], r["profile"], r["reply"], r["tool_data"]) for r in ok_runs]
    # 确定性检查（不经过 LLM）
    print("\n=== 推荐回复的确定性检查 ===")
    det = {}
    for it in items:
        c = it.extra["checks"]
        det[it.id] = c
        bad = [k for k in ("no_probability", "discoveries_mentioned", "grounded_schools") if not c[k]]
        print(f"{it.id:<22}{'通过' if not bad else '未通过：' + '、'.join(bad)}" +
              (f"  概率措辞={c['probability_hits']}" if not c["no_probability"] else "") +
              (f"  漏提信息差={c['discoveries_missing']}" if not c["discoveries_mentioned"] else "") +
              (f"  工具结果里没有的学校={c['ungrounded']}" if not c["grounded_schools"] else "") +
              ("" if it.extra["has_tool_data"] else "  （没取到工具结果，检查不可靠）"))
    verdicts = await judge_all(items, judge_fn)
    agg = aggregate(verdicts)
    print_aggregate("推荐回复质量（LLM 评审）", agg, ADVISOR_MIN_PASS)
    worst = print_worst(verdicts, items)
    return {"scenarios": [{"name": r["name"], "error": r.get("error"), "agents": r.get("agents")} for r in runs],
            "deterministic": det, "aggregate": agg, "min_pass_suggested": ADVISOR_MIN_PASS, "worst": worst,
            "verdicts": [{"id": v.item_id, "error": v.error, "scores": v.scores} for v in verdicts]}


async def main_async(args) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    fn = rate_limited(call_gemini_judge, per_minute=args.per_minute)
    judge_fn = fn if args.no_cache else json_cached(fn, CACHE_DIR / f"judge_{judge_model_name()}.json")
    targets = ["calibration", "profile", "advisor"] if args.target == "all" else [args.target]
    REPORT_DIR.mkdir(exist_ok=True)
    for t in targets:
        rep = {"calibration": lambda: run_calibration(judge_fn),
               "profile": lambda: run_profile(judge_fn, args),
               "advisor": lambda: run_advisor(judge_fn, args)}[t]
        out = await rep()
        out["judge_model"] = judge_model_name()
        (REPORT_DIR / f"judge_{t}.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
        print(f"已写入 {REPORT_DIR / f'judge_{t}.json'}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["calibration", "profile", "advisor", "all"], required=True)
    ap.add_argument("--limit", type=int, default=80)
    ap.add_argument("--per-minute", type=int, default=12)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--refresh-advisor", action="store_true")
    ap.add_argument("--gap", type=float, default=20.0)
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
