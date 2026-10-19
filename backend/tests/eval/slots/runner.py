"""Slot F1 运行器：逐轮调用 Profile Agent、按生产规则合并槽位、交给 metrics 打分。

agent_fn 默认是生产的 app.services.agent_client.call_agent（真实调 Gemini）；
测试里可以注入脚本化的假 Agent，所以评分/合并逻辑不需要 Key 就能验证。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Awaitable, Callable

from ..ratelimit import rate_limited  # noqa: F401  （re-export：保持 from .runner import rate_limited 可用）
from .cases import Case
from .metrics import has_question, merge_update, missing_required

AgentFn = Callable[[str, list, str], Awaitable[dict]]
CACHE_PATH = Path(__file__).parent / ".cache" / "agent_responses.json"

_EMPTY_PROFILE = {
    "province": None, "track": None, "subjects": [], "score": None, "rank": None,
    "major_keyword": None, "city_pref": None, "budget_max": None, "home_province_only": False,
    "accept_sino_foreign": None, "accept_special_program": None,
}


async def run_case(case: Case, agent_fn: AgentFn) -> dict:
    state: dict = {}
    history: list[dict] = []
    followup: list[tuple[int, bool]] = []
    replies: list[str] = []
    for i, msg in enumerate(case.turns):
        shown = {**_EMPTY_PROFILE, **state}  # 生产里给 Agent 看的是完整 ProfileIn，这里保持同形
        res = await agent_fn(json.dumps(shown, ensure_ascii=False), list(history), msg)
        merge_update(state, res.get("profile") or {})
        reply = res.get("reply", "")
        replies.append(reply)
        history += [{"role": "user", "content": msg}, {"role": "assistant", "content": reply}]
        if i in case.followup_turns:
            followup.append((i, bool(missing_required(state)) and has_question(reply)))
    return {"id": case.id, "tag": case.tag, "expected": case.expected, "state": state,
            "followup": followup, "replies": replies}


def with_cache(agent_fn: AgentFn, path: Path = CACHE_PATH, refresh: bool = False) -> AgentFn:
    """按 (profile, history, message) 缓存 Agent 响应：重跑评分不重复花 Gemini 调用。
    改了 prompt/模型之后要删掉 .cache 目录，否则读到的是旧响应。"""
    # refresh=True：忽略旧缓存、真实重新调用，并把新响应写回（覆盖旧文件）。改了提示词/模型后必须这样做，
    # 否则 Slot F1 和 LLM-as-Judge 的 profile 评测（共用这份缓存）读到的还是旧提示词时的回复。
    cache: dict = {} if refresh or not path.exists() else json.loads(path.read_text())

    async def wrapped(profile_json: str, history: list, message: str) -> dict:
        key = hashlib.sha256(json.dumps([profile_json, history, message], ensure_ascii=False).encode()).hexdigest()
        if key not in cache:
            cache[key] = await agent_fn(profile_json, history, message)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cache, ensure_ascii=False))
        return cache[key]

    return wrapped


async def run_all(cases: list[Case], agent_fn: AgentFn, concurrency: int = 3) -> list[dict]:
    sem = asyncio.Semaphore(concurrency)

    async def one(c: Case) -> dict:
        async with sem:
            try:
                return await run_case(c, agent_fn)
            except Exception as exc:  # 调用失败要计入结果，不能让整批评测中断或悄悄少一条
                return {"id": c.id, "tag": c.tag, "expected": c.expected, "state": {},
                        "followup": [], "replies": [], "error": f"{type(exc).__name__}: {exc}"}

    return await asyncio.gather(*(one(c) for c in cases))
