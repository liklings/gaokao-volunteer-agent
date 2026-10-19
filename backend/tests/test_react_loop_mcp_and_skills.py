"""
验证 web_search（走 MCP 客户端）和 load_skill 这两个新工具，在真实的
ReAct 循环状态机里（不是直接调用 execute_tool()）也能正常被调度、
真实拿到数据、正确写回对话历史——跟 test_react_loop.py 里其他工具的
集成测试是同一种验证方式（假模型 + 真实工具执行），只是这里换成新工具。

web_search 这一步会真实通过子进程连接本仓库自带的离线参考 MCP server
（app/mcp_servers/policy_search_server.py），不是脚本模拟——这是故意的：
只测"假模型决定调用 web_search 工具"这半句没有意义，真正要验证的是
"ReAct 循环 -> execute_tool -> mcp_client -> 真实子进程 -> 真实返回"
这条完整链路在没有 Gemini 参与的情况下也是通的，Gemini 那一半（模型
自己决定要不要调用）没法在这个沙箱里对真实 API 验证，留给 scripts/
目录下需要真实 GEMINI_API_KEY 的手动脚本。
"""
from app.services.react_loop import ModelResponse, run_react_loop
from app.services.tools import TOOL_SCHEMAS, ToolContext

from .conftest import make_profile_in


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


def make_ctx(db) -> ToolContext:
    return ToolContext(db=db, profile=make_profile_in())


async def test_load_skill_then_final_answer(db):
    ctx = make_ctx(db)
    model = scripted_model([
        ModelResponse(is_final_answer=False, tool_name="load_skill",
                      tool_args={"skill_id": "adjustment"}, tokens_used=30),
        ModelResponse(is_final_answer=True, text="服从调剂可以避免退档，但可能被分到不喜欢的专业。", tokens_used=40),
    ])
    text, trace = await run_react_loop(
        agent_name="retrieval", system_prompt="test", initial_message="服从调剂是什么意思",
        ctx=ctx, model_caller=model, tool_schemas=TOOL_SCHEMAS,
    )
    assert trace.status == "completed"
    assert trace.steps[0].tool_result["data"]["skill_id"] == "adjustment"
    assert "退档" in trace.steps[0].tool_result["data"]["content"]
    assert "服从调剂" in text


async def test_web_search_via_mcp_then_final_answer(db):
    ctx = make_ctx(db)
    model = scripted_model([
        ModelResponse(is_final_answer=False, tool_name="web_search",
                      tool_args={"query": "招生计划", "recency": "any"}, tokens_used=30),
        ModelResponse(is_final_answer=True, text="招生计划每年会有变化。", tokens_used=40),
    ])
    text, trace = await run_react_loop(
        agent_name="retrieval", system_prompt="test", initial_message="招生计划会变吗",
        ctx=ctx, model_caller=model, tool_schemas=TOOL_SCHEMAS,
    )
    assert trace.status == "completed", f"应该正常完成，实际 {trace.status}/{trace.abort_reason}"
    assert trace.steps[0].status == "ok", f"web_search 这一步应该成功: {trace.steps[0].tool_result}"
    assert trace.steps[0].tool_result["data"]["results"], "应该真实拿到匹配结果"
    assert "招生计划" in text
