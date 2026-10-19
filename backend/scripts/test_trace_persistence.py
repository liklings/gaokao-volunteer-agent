"""
Trace 落库的真实测试——用脚本化跑出来的真实 RunTrace（不需要 Gemini），
落库之后再从数据库里查回来，确认存进去的每个字段都对得上，
包括 tool_args/tool_result 这种 JSONB 字段有没有正确序列化/反序列化。

用法（backend/ 目录下，激活 venv，数据库已经跑通迁移+种子数据）：
    python scripts/test_trace_persistence.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio

from sqlalchemy import select

from app.db import SessionLocal
from app.models import AgentRun, AgentStep, Profile
from app.schemas.candidate import ProfileIn
from app.services.react_loop import ModelResponse, run_react_loop
from app.services.tools import TOOL_SCHEMAS, ToolContext
from app.services.trace_persistence import persist_trace


def scripted_model(responses):
    call_count = 0

    async def _caller(system_prompt, contents, tool_schemas):
        nonlocal call_count
        r = responses[call_count]
        call_count += 1
        return r

    return _caller


async def main() -> int:
    db = SessionLocal()

    # 需要一条真实的 profiles 行来满足外键约束，直接建一条
    profile_row = Profile(
        province="江苏", year=2026, track="物理类", subjects=["物理", "化学"],
        score=637, rank=5200, major_keyword="计算机",
    )
    db.add(profile_row)
    db.commit()
    db.refresh(profile_row)
    print(f"测试用 profile_id = {profile_row.id}")

    profile_for_ctx = ProfileIn(
        province="江苏", track="物理类", subjects=["物理", "化学"],
        score=637, rank=5200, major_keyword="计算机",
    )
    ctx = ToolContext(db=db, profile=profile_for_ctx)

    model = scripted_model([
        ModelResponse(is_final_answer=False, tool_name="search_candidates", tool_args={}, tokens_used=50),
        ModelResponse(is_final_answer=False, tool_name="get_candidate_detail",
                      tool_args={"group_id": "seu-cs-04"}, tokens_used=40),
        ModelResponse(is_final_answer=True, text="推荐东南大学计算机类专业组。", tokens_used=60),
    ])

    print("\n=== 跑一次真实的 ReAct 循环（脚本化模型，真实工具+真实数据库）===")
    text, trace = await run_react_loop(
        agent_name="retrieval", system_prompt="test", initial_message="帮我查计算机专业",
        ctx=ctx, model_caller=model, tool_schemas=TOOL_SCHEMAS,
    )
    print(f"循环结果: status={trace.status} steps={trace.total_steps} tokens={trace.total_tokens}")

    print("\n=== 落库 ===")
    run = persist_trace(db, trace, profile_id=profile_row.id)
    print(f"已落库，run.id={run.id}")

    print("\n=== 从数据库里查回来，核对每个字段 ===")
    fetched_run = db.get(AgentRun, run.id)
    assert fetched_run is not None, "应该能查到刚存的run"
    assert fetched_run.profile_id == profile_row.id
    assert fetched_run.agent_name == "retrieval"
    assert fetched_run.status == "completed"
    assert fetched_run.total_steps == 3
    assert fetched_run.total_tokens == 150
    assert fetched_run.finished_at is not None
    print(f"AgentRun 核对通过: status={fetched_run.status} steps={fetched_run.total_steps} "
          f"tokens={fetched_run.total_tokens}")

    fetched_steps = list(db.scalars(
        select(AgentStep).where(AgentStep.run_id == run.id).order_by(AgentStep.step_index)
    ).all())
    assert len(fetched_steps) == 3, f"应该存了3个step，实际 {len(fetched_steps)}"

    assert fetched_steps[0].action_type == "tool_call"
    assert fetched_steps[0].tool_name == "search_candidates"
    assert fetched_steps[0].tool_result["data"]["count"] == 10, "JSONB字段应该完整保留真实的数据库查询结果"
    print(f"第0步核对通过: tool={fetched_steps[0].tool_name}, "
          f"真实查到候选数={fetched_steps[0].tool_result['data']['count']}")

    assert fetched_steps[1].tool_name == "get_candidate_detail"
    assert fetched_steps[1].tool_args == {"group_id": "seu-cs-04"}, "tool_args这个JSONB字段应该原样保留"
    assert fetched_steps[1].tool_result["data"]["university"] == "东南大学"
    print(f"第1步核对通过: tool_args={fetched_steps[1].tool_args}, "
          f"查到的学校={fetched_steps[1].tool_result['data']['university']}")

    assert fetched_steps[2].action_type == "final_answer"
    assert fetched_steps[2].tool_name is None
    print(f"第2步核对通过: action_type={fetched_steps[2].action_type}")

    print("\n=== 边界测试：一次被中断的run（重复调用检测触发），落库后abort_reason要对 ===")
    same_args = {"group_id": "seu-cs-04"}
    model2 = scripted_model([
        ModelResponse(is_final_answer=False, tool_name="get_candidate_detail", tool_args=same_args, tokens_used=30),
        ModelResponse(is_final_answer=False, tool_name="get_candidate_detail", tool_args=same_args, tokens_used=30),
    ])
    _text2, trace2 = await run_react_loop(
        agent_name="retrieval", system_prompt="test", initial_message="重复测试",
        ctx=ctx, model_caller=model2, tool_schemas=TOOL_SCHEMAS,
    )
    run2 = persist_trace(db, trace2, profile_id=profile_row.id)
    fetched_run2 = db.get(AgentRun, run2.id)
    assert fetched_run2.status == "aborted"
    assert fetched_run2.abort_reason == "duplicate_tool_call"
    print(f"中断场景核对通过: status={fetched_run2.status} abort_reason={fetched_run2.abort_reason}")

    db.close()
    print("\n全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
