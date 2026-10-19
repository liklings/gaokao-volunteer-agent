"""把被评对象整理成 JudgeItem。

- profile_items：重放 Slot F1 用例，逐轮取 Profile Agent 的回复（命中 slots 的响应缓存时不花 Gemini 额度）。
  背景事实 = 本轮之后的已收集条件 + 仍缺的必填项（由确定性代码算出，评审员据此判断追问是否得当）。
- advisor_items：真实跑 Orchestrator（需要本机数据库里的江苏数据 + Gemini），取检索回复和 search_candidates
  的工具结果；工具结果既作为评审员的背景事实，也用于 checks.py 的确定性检查。
  运行会在本机库里临时创建评测专用 Profile，结束后连同对话/trace 记录一起删除。
"""
from __future__ import annotations

import asyncio
import json
import random

from ..slots.cases import CASES
from ..slots.metrics import is_empty, merge_update, missing_required
from ..slots.runner import _EMPTY_PROFILE
from .checks import check_answer, tier_candidates
from .judge import JudgeItem

_MISSING_LABEL = {"province": "省份", "track": "类别（物理/历史）", "subjects": "选科组合",
                  "major_keyword": "专业关键词", "score_or_rank": "分数或位次"}

# 评测场景：覆盖不同位次段与类别。位次范围参考库里 2025 年的分布（物理类约 20 万考生、历史类约 5.6 万）。
ADVISOR_SCENARIOS = [
    {"name": "phys-top", "profile": dict(province="江苏", track="物理类", subjects=["物理", "化学", "生物"], rank=1500, major_keyword="计算机")},
    {"name": "phys-mid", "profile": dict(province="江苏", track="物理类", subjects=["物理", "化学", "生物"], rank=15000, major_keyword="电子信息")},
    {"name": "phys-low", "profile": dict(province="江苏", track="物理类", subjects=["物理", "化学", "地理"], rank=60000, major_keyword="机械")},
    {"name": "phys-med", "profile": dict(province="江苏", track="物理类", subjects=["物理", "化学", "生物"], rank=5000, major_keyword="临床医学")},
    {"name": "hist-top", "profile": dict(province="江苏", track="历史类", subjects=["历史", "政治", "地理"], rank=1000, major_keyword="法学")},
    {"name": "hist-mid", "profile": dict(province="江苏", track="历史类", subjects=["历史", "政治", "地理"], rank=8000, major_keyword="汉语言文学")},
    {"name": "hist-low", "profile": dict(province="江苏", track="历史类", subjects=["历史", "政治", "生物"], rank=25000, major_keyword="会计")},
    {"name": "phys-budget", "profile": dict(province="江苏", track="物理类", subjects=["物理", "化学", "生物"], rank=20000, major_keyword="计算机", budget_max=8000)},
]
ADVISOR_MESSAGE = "帮我推荐一份志愿，冲稳保三档都给我看看"


async def profile_items(agent_fn, limit: int = 80, seed: int = 11) -> tuple[list[JudgeItem], int]:
    all_items: list[JudgeItem] = []
    for case in CASES:
        state: dict = {}
        history: list[dict] = []
        for i, msg in enumerate(case.turns):
            res = await agent_fn(json.dumps({**_EMPTY_PROFILE, **state}, ensure_ascii=False), list(history), msg)
            merge_update(state, res.get("profile") or {})
            reply = res.get("reply", "")
            ctx = {
                "对话历史": [{"role": m["role"], "content": m["content"]} for m in history[-6:]],
                "本轮用户输入": msg,
                "已收集的条件": {k: v for k, v in state.items() if not is_empty(v) and k != "year"},
                "仍缺的必填项": [_MISSING_LABEL[m] for m in missing_required(state)],
            }
            all_items.append(JudgeItem(f"{case.id}#t{i}", "profile_reply", ctx, reply, extra={"tag": case.tag}))
            history += [{"role": "user", "content": msg}, {"role": "assistant", "content": reply}]
    n_total = len(all_items)
    if limit and limit < n_total:
        all_items = sorted(random.Random(seed).sample(all_items, limit), key=lambda x: x.id)
    return all_items, n_total


def _summarize_tool_data(data: dict) -> dict:
    """压缩 search_candidates 结果给评审员看：保留判断需要的字段，截断每档数量。"""
    def slim(c):
        keep = ("university", "group_name", "city", "rank_diff", "school_tier", "tags", "tuition", "rank_equivalent_note")
        return {k: c.get(k) for k in keep if c.get(k) not in (None, [], "")}

    return {
        "tiers": {k: [slim(c) for c in tier_candidates(v)[:6]] for k, v in (data.get("tiers") or {}).items()},
        "各档候选总数": {k: v.get("total_in_category") for k, v in (data.get("tiers") or {}).items() if isinstance(v, dict)},
        "discoveries": [{k: d.get(k) for k in ("university", "city", "school_tier", "rank_diff", "category_label") if d.get(k) is not None}
                        for d in (data.get("discoveries") or [])],
        "note": data.get("note"),
    }


def advisor_item_from_run(name: str, profile: dict, reply: str, tool_data: dict | None) -> JudgeItem:
    ctx = {"用户请求": ADVISOR_MESSAGE, "考生条件": {k: v for k, v in profile.items()},
           "工具结果": _summarize_tool_data(tool_data or {})}
    return JudgeItem(f"adv-{name}", "advisor_answer", ctx, reply,
                     extra={"checks": check_answer(reply, tool_data or {}), "has_tool_data": bool(tool_data)})


def cleanup_profile(db, pid: int) -> None:
    """删除评测专用 Profile 及其对话/trace 记录。全部用批量删除并按外键顺序执行：
    会话 autoflush=False，若混用 db.delete(对象) 和批量删除，对象删除会被延后到批量删除之后，
    触发 agent_runs_profile_id_fkey 外键冲突（首次运行就撞到了这个）。"""
    from sqlalchemy import select

    from app.models import ChatMessage, Profile
    from app.models.agent_run import AgentRun
    from app.models.agent_step import AgentStep

    run_ids = select(AgentRun.id).where(AgentRun.profile_id == pid)
    db.query(AgentStep).filter(AgentStep.run_id.in_(run_ids)).delete(synchronize_session=False)
    db.query(AgentRun).filter(AgentRun.profile_id == pid).delete(synchronize_session=False)
    db.query(ChatMessage).filter(ChatMessage.profile_id == pid).delete(synchronize_session=False)
    db.query(Profile).filter(Profile.id == pid).delete(synchronize_session=False)
    db.commit()


def cleanup_cli(pid: int) -> None:
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        cleanup_profile(db, pid)
        print(f"已清理 Profile {pid}")
    finally:
        db.close()


async def run_advisor_scenarios(gap_seconds: float = 20.0, only: list[str] | None = None) -> list[dict]:
    """真实运行 Orchestrator。返回 [{name, profile, reply, tool_data, error?}]。"""
    from app.db import SessionLocal
    from app.models import Profile
    from app.services.orchestrator import run_orchestrator

    out = []
    for sc in ADVISOR_SCENARIOS:
        if only and sc["name"] not in only:
            continue
        db = SessionLocal()
        pid = None
        try:
            prof = Profile(**sc["profile"], accept_sino_foreign=False, accept_special_program=False)
            db.add(prof)
            db.commit()
            db.refresh(prof)
            pid = prof.id
            res = await run_orchestrator(db, prof, ADVISOR_MESSAGE)
            tool_data = None
            if res.retrieval_trace:
                for st in res.retrieval_trace.steps:
                    if st.tool_name == "search_candidates" and st.status == "ok" and st.tool_result:
                        tool_data = st.tool_result.get("data", st.tool_result)
            out.append({"name": sc["name"], "profile": sc["profile"], "reply": res.reply, "tool_data": tool_data,
                        "agents": [s.agent for s in res.plan_steps]})
        except Exception as exc:
            out.append({"name": sc["name"], "profile": sc["profile"], "reply": "", "tool_data": None,
                        "error": f"{type(exc).__name__}: {exc}"})
        finally:
            db.rollback()
            if pid is not None:
                try:
                    cleanup_profile(db, pid)
                except Exception as exc:  # 清理失败不能让整批评测丢结果；提示手工清理
                    db.rollback()
                    print(f"警告：评测专用 Profile {pid} 清理失败（{type(exc).__name__}），可稍后手工清理：\n"
                          f"  python -c \"from tests.eval.judge.targets import cleanup_cli; cleanup_cli({pid})\"")
            db.close()
        await asyncio.sleep(gap_seconds)  # 编排器内部会连续调用 Gemini，场景间留间隔避免撞免费档限额
    return out
