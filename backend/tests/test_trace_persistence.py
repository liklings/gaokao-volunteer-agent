"""
Trace 落库：用脚本化跑出来的真实 RunTrace（不需要 Gemini），落库之后
再从数据库里查回来，确认存进去的每个字段都对得上，包括 tool_args/
tool_result 这种 JSONB 字段有没有正确序列化/反序列化。

从 scripts/test_trace_persistence.py 迁移，断言逻辑原样保留。
"""
from sqlalchemy import select

from app.models import AgentRun, AgentStep, Profile
from app.services.react_loop import ModelResponse, run_react_loop
from app.services.tools import TOOL_SCHEMAS, ToolContext
from app.services.trace_persistence import persist_trace

from .conftest import make_profile_in


def scripted_model(responses):
    call_count = 0

    async def _caller(system_prompt, contents, tool_schemas):
        nonlocal call_count
        r = responses[call_count]
        call_count += 1
        return r

    return _caller


def _make_profile_row(db) -> Profile:
    # 需要一条真实的 profiles 行来满足外键约束
    profile_row = Profile(
        province="江苏", year=2026, track="物理类", subjects=["物理", "化学"],
        score=637, rank=5200, major_keyword="计算机",
    )
    db.add(profile_row)
    db.commit()
    db.refresh(profile_row)
    return profile_row


async def test_completed_run_persists_all_fields(db):
    profile_row = _make_profile_row(db)
    ctx = ToolContext(db=db, profile=make_profile_in())

    model = scripted_model([
        ModelResponse(is_final_answer=False, tool_name="search_candidates", tool_args={}, tokens_used=50),
        ModelResponse(is_final_answer=False, tool_name="get_candidate_detail",
                      tool_args={"group_id": "seu-cs-04"}, tokens_used=40),
        ModelResponse(is_final_answer=True, text="推荐东南大学计算机类专业组。", tokens_used=60),
    ])
    text, trace = await run_react_loop(
        agent_name="retrieval", system_prompt="test", initial_message="帮我查计算机专业",
        ctx=ctx, model_caller=model, tool_schemas=TOOL_SCHEMAS,
    )

    run = persist_trace(db, trace, profile_id=profile_row.id)

    fetched_run = db.get(AgentRun, run.id)
    assert fetched_run is not None, "应该能查到刚存的run"
    assert fetched_run.profile_id == profile_row.id
    assert fetched_run.agent_name == "retrieval"
    assert fetched_run.status == "completed"
    assert fetched_run.total_steps == 3
    assert fetched_run.total_tokens == 150
    assert fetched_run.finished_at is not None

    fetched_steps = list(db.scalars(
        select(AgentStep).where(AgentStep.run_id == run.id).order_by(AgentStep.step_index)
    ).all())
    assert len(fetched_steps) == 3, f"应该存了3个step，实际 {len(fetched_steps)}"

    assert fetched_steps[0].action_type == "tool_call"
    assert fetched_steps[0].tool_name == "search_candidates"
    assert fetched_steps[0].tool_result["data"]["count"] == 10, "JSONB字段应该完整保留真实的数据库查询结果"

    assert fetched_steps[1].tool_name == "get_candidate_detail"
    assert fetched_steps[1].tool_args == {"group_id": "seu-cs-04"}, "tool_args这个JSONB字段应该原样保留"
    assert fetched_steps[1].tool_result["data"]["university"] == "东南大学"

    assert fetched_steps[2].action_type == "final_answer"
    assert fetched_steps[2].tool_name is None


async def test_aborted_run_persists_abort_reason(db):
    """边界测试：一次被中断的run（重复调用检测触发），落库后abort_reason要对。"""
    profile_row = _make_profile_row(db)
    ctx = ToolContext(db=db, profile=make_profile_in())

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
