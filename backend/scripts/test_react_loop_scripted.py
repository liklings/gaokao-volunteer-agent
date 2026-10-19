"""
ReAct 循环的脚本化测试——用一个按预设顺序返回响应的"假模型"，
验证状态机本身的正确性：正常完成路径、重复调用检测、
工具失败后能不能继续、达到最大步数后的降级行为。
全程不发一次真实网络请求，也不需要真实的 Gemini Key。

用法（backend/ 目录下，激活 venv，数据库已经跑通种子数据导入）：
    python scripts/test_react_loop_scripted.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio

from app.db import SessionLocal
from app.schemas.candidate import ProfileIn
from app.services.react_loop import ModelResponse, run_react_loop
from app.services.tools import TOOL_SCHEMAS, ToolContext


def scripted_model(responses: list[ModelResponse]):
    """返回一个 model_caller：每调用一次，按顺序吐出 responses 里的下一个。
    脚本用完还被调用，说明循环没有在预期的地方停下来，直接报错让测试失败。
    """
    call_count = 0

    async def _caller(system_prompt, contents, tool_schemas):
        nonlocal call_count
        if call_count >= len(responses):
            raise AssertionError(
                f"脚本已经用完（预设了{len(responses)}次响应），但循环还在继续调用模型，"
                f"说明状态机没有在预期步骤停下来。"
            )
        r = responses[call_count]
        call_count += 1
        return r

    return _caller


def make_ctx() -> ToolContext:
    db = SessionLocal()
    profile = ProfileIn(
        province="江苏", track="物理类", subjects=["物理", "化学"],
        score=637, rank=5200, major_keyword="计算机",
    )
    return ToolContext(db=db, profile=profile)


async def scenario_normal_completion():
    print("=== 场景1：正常完成路径（查列表 -> 查详情 -> 给出最终回复） ===")
    ctx = make_ctx()
    model = scripted_model([
        ModelResponse(is_final_answer=False, tool_name="search_candidates", tool_args={}, tokens_used=50),
        ModelResponse(is_final_answer=False, tool_name="get_candidate_detail",
                      tool_args={"group_id": "seu-cs-04"}, tokens_used=40),
        ModelResponse(is_final_answer=True, text="根据检索结果，推荐东南大学计算机类专业组。", tokens_used=60),
    ])
    text, trace = await run_react_loop(
        agent_name="retrieval", system_prompt="test", initial_message="帮我查计算机专业",
        ctx=ctx, model_caller=model, tool_schemas=TOOL_SCHEMAS,
    )
    assert trace.status == "completed", f"应该正常完成，实际状态: {trace.status}"
    assert trace.total_steps == 3, f"应该正好3步，实际 {trace.total_steps}"
    assert trace.total_tokens == 150, f"token应该累加到150，实际 {trace.total_tokens}"
    assert "东南大学" in text
    # 校验中间两步确实真实调用了工具、拿到了真实数据（不是空壳）
    assert trace.steps[0].tool_result["data"]["count"] == 10
    assert trace.steps[1].tool_result["data"]["university"] == "东南大学"
    print(f"通过。text={text!r}, total_tokens={trace.total_tokens}, steps={trace.total_steps}")
    ctx.db.close()


async def scenario_duplicate_call_aborts_early():
    print("\n=== 场景2：重复调用同一工具+同样参数，应立即终止（不等到步数用完） ===")
    ctx = make_ctx()
    same_args = {"group_id": "seu-cs-04"}
    model = scripted_model([
        ModelResponse(is_final_answer=False, tool_name="get_candidate_detail", tool_args=same_args, tokens_used=30),
        ModelResponse(is_final_answer=False, tool_name="get_candidate_detail", tool_args=same_args, tokens_used=30),
        # 如果保护没生效，脚本会在这里被要求提供第3个响应，进而触发 AssertionError
    ])
    text, trace = await run_react_loop(
        agent_name="retrieval", system_prompt="test", initial_message="查一下东南大学",
        ctx=ctx, model_caller=model, tool_schemas=TOOL_SCHEMAS,
    )
    assert trace.status == "aborted"
    assert trace.abort_reason == "duplicate_tool_call"
    assert trace.total_steps == 2, f"应该在第2步就终止，实际跑了 {trace.total_steps} 步"
    assert "重复调用" in text
    print(f"通过。在第{trace.total_steps}步正确终止，text={text!r}")
    ctx.db.close()


async def scenario_tool_failure_does_not_abort_loop():
    print("\n=== 场景3：工具调用失败不应该让整个循环崩溃，模型能看到错误并继续 ===")
    ctx = make_ctx()
    model = scripted_model([
        # 第一步：查一个不存在的 group_id，工具会返回失败
        ModelResponse(is_final_answer=False, tool_name="get_candidate_detail",
                      tool_args={"group_id": "not-exist-999"}, tokens_used=30),
        # 第二步：模型"看到"了失败信息后，换成调用 search_candidates 找正确的 group_id
        ModelResponse(is_final_answer=False, tool_name="search_candidates", tool_args={}, tokens_used=40),
        # 第三步：给出最终回复
        ModelResponse(is_final_answer=True, text="抱歉刚才查错了ID，已经找到正确的候选列表。", tokens_used=50),
    ])
    text, trace = await run_react_loop(
        agent_name="retrieval", system_prompt="test", initial_message="查一下某个专业组",
        ctx=ctx, model_caller=model, tool_schemas=TOOL_SCHEMAS,
    )
    assert trace.status == "completed", "工具失败不该导致整个循环被判定为失败"
    assert trace.steps[0].status == "failed", "第一步的工具调用本身应该标记为failed"
    assert trace.steps[1].status == "ok", "第二步换了个工具调用，应该成功"
    assert trace.total_steps == 3
    print(f"通过。第1步失败但循环继续，最终完成，text={text!r}")
    ctx.db.close()


async def scenario_max_steps_exceeded():
    print("\n=== 场景4：模型一直换着不同参数调用、从不给出最终回复，应在达到步数上限时终止 ===")
    ctx = make_ctx()
    # 每次用不同的major_keyword，确保不会触发"重复调用"保护，专门测步数上限这条独立的保护
    responses = [
        ModelResponse(is_final_answer=False, tool_name="search_candidates",
                      tool_args={"major_keyword": f"专业{i}"}, tokens_used=20)
        for i in range(10)  # 故意设置成比 MAX_STEPS 多，确认循环不会真的跑满10次
    ]
    model = scripted_model(responses)
    text, trace = await run_react_loop(
        agent_name="retrieval", system_prompt="test", initial_message="随便查点什么",
        ctx=ctx, model_caller=model, tool_schemas=TOOL_SCHEMAS,
    )
    assert trace.status == "aborted"
    assert trace.abort_reason == "max_steps_exceeded"
    assert trace.total_steps == 6, f"应该在 MAX_STEPS=6 步后终止，实际 {trace.total_steps}"
    assert "步骤比较多" in text
    print(f"通过。在第{trace.total_steps}步正确终止（MAX_STEPS保护生效），text={text!r}")
    ctx.db.close()


async def main():
    await scenario_normal_completion()
    await scenario_duplicate_call_aborts_early()
    await scenario_tool_failure_does_not_abort_loop()
    await scenario_max_steps_exceeded()
    print("\n全部4个场景通过。")


if __name__ == "__main__":
    asyncio.run(main())
