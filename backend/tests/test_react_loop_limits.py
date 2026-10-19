"""
ReAct 循环里三条成本控制硬限制（app/services/limits.py）：单轮 token 预算、
单个工具调用超时、整轮总耗时上限。不发真实网络请求，不需要真实 Gemini Key，
用假的 model_caller 和（必要时）临时注册的慢工具来触发这几条限制。

从 scripts/test_react_loop_limits_scripted.py 迁移，断言逻辑原样保留；
临时修改全局配置/注册表的部分改用 pytest 的 monkeypatch fixture 自动复原，
比原脚本手写的 try/finally 更不容易漏掉复原步骤。
"""
import time

import app.services.react_loop as react_loop_module
from app.services.react_loop import ModelResponse, run_react_loop
from app.services.tools import TOOL_REGISTRY, ToolContext, ToolResult

from .conftest import make_profile_in


def make_ctx(db) -> ToolContext:
    return ToolContext(db=db, profile=make_profile_in())


def scripted_model(responses: list[ModelResponse]):
    call_count = 0

    async def _caller(system_prompt, contents, tool_schemas):
        nonlocal call_count
        if call_count >= len(responses):
            raise AssertionError(
                f"脚本已经用完（预设了{len(responses)}次响应），但循环还在继续调用模型。"
            )
        r = responses[call_count]
        call_count += 1
        return r

    return _caller


async def test_token_budget_exceeded(db, monkeypatch):
    """单轮 token 预算超限，应在预算耗尽后强制中断，不再多调用一次模型。"""
    monkeypatch.setattr(react_loop_module, "MAX_TOKENS_PER_RUN", 8000)  # 跟生产配置保持一致
    ctx = make_ctx(db)
    # 两次工具调用各消耗5000 token，累计10000 > 8000，第三次模型调用前应该被拦下——
    # 脚本只预设了2次响应，如果循环真的还发起第3次调用，scripted_model会直接报错，
    # 这本身就是"没有在预算超限时提前停下"的证据。
    model = scripted_model([
        ModelResponse(is_final_answer=False, tool_name="search_candidates", tool_args={}, tokens_used=5000),
        ModelResponse(is_final_answer=False, tool_name="get_candidate_detail",
                      tool_args={"group_id": "seu-cs-04"}, tokens_used=5000),
    ])
    text, trace = await run_react_loop(
        agent_name="retrieval", system_prompt="test", initial_message="帮我查计算机专业",
        ctx=ctx, model_caller=model, tool_schemas=[],
    )
    assert trace.status == "aborted", f"应该被中断，实际状态: {trace.status}"
    assert trace.abort_reason == "token_budget_exceeded", f"中断原因不对: {trace.abort_reason}"
    assert trace.total_tokens == 10000, f"两步token应该都算上，实际: {trace.total_tokens}"
    assert trace.total_steps == 2, f"应该恰好执行了2步（第3步前被拦下），实际: {trace.total_steps}"


async def test_tool_call_timeout(db, monkeypatch):
    """单个工具调用超时，应该记成失败观察结果，不让整个循环崩溃/卡死。"""
    monkeypatch.setattr(react_loop_module, "TOOL_CALL_TIMEOUT_SECONDS", 0.3)  # 不用真等15秒

    def _slow_tool(args: dict, ctx: ToolContext) -> ToolResult:
        time.sleep(1)  # 模拟一个卡住的工具（比如以后接入的真实联网搜索）
        return ToolResult(ok=True, data={"should_not_reach_here": True})

    monkeypatch.setitem(TOOL_REGISTRY, "_slow_test_tool", _slow_tool)
    ctx = make_ctx(db)
    model = scripted_model([
        ModelResponse(is_final_answer=False, tool_name="_slow_test_tool", tool_args={}, tokens_used=10),
        ModelResponse(is_final_answer=True, text="工具超时了，但我还是基于已知信息回答了。", tokens_used=20),
    ])
    text, trace = await run_react_loop(
        agent_name="retrieval", system_prompt="test", initial_message="随便问点什么",
        ctx=ctx, model_caller=model, tool_schemas=[],
    )
    assert trace.status == "completed", f"工具超时不该让整轮失败，实际状态: {trace.status}"
    assert trace.steps[0].status == "failed", "超时的这一步应该记成失败"
    assert "超时" in trace.steps[0].tool_result["error"], f"错误信息里应该说明是超时: {trace.steps[0].tool_result}"
    assert trace.steps[1].action_type == "final_answer"


async def test_run_total_timeout(db, monkeypatch):
    """整轮耗时超过总上限，应该强制中断，不管步数还剩多少。"""
    monkeypatch.setattr(react_loop_module, "RUN_TOTAL_TIMEOUT_SECONDS", 0.3)  # 不用真等60秒
    ctx = make_ctx(db)

    async def _slow_caller(system_prompt, contents, tool_schemas):
        import asyncio
        await asyncio.sleep(1)  # 模拟模型调用本身很慢（比如网络重试撞在了一起）
        return ModelResponse(is_final_answer=True, text="不应该跑到这里。", tokens_used=1)

    text, trace = await run_react_loop(
        agent_name="retrieval", system_prompt="test", initial_message="随便问点什么",
        ctx=ctx, model_caller=_slow_caller, tool_schemas=[],
    )
    assert trace.status == "aborted", f"应该被整轮超时中断，实际状态: {trace.status}"
    assert trace.abort_reason == "run_timeout_exceeded", f"中断原因不对: {trace.abort_reason}"
