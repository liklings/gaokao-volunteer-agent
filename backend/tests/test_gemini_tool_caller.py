"""
Mock 测试：用假的、但格式完全仿照真实 Gemini 响应结构的数据（那个结构是这个
项目之前真实调用成功时观察到的，字段名不是编的），验证 call_gemini_with_tools()
的请求构造和响应解析逻辑本身是对的，不依赖真实网络、不需要真实 Key。

能验证：请求体格式对不对、functionCall 解析对不对、functionResponse 回填对不对、
    token 累加对不对、跟 react_loop 衔接对不对——这些都是我们自己代码的逻辑。
不能验证：Gemini 真实收到这个请求后，行为是否真的和我们预期的一样——这部分要靠
    标记为 integration 的真实网络测试来确认。

从 scripts/test_gemini_tool_caller_mocked.py 迁移，断言逻辑原样保留。
"""
import json
from unittest.mock import MagicMock, patch

import app.services.gemini_tool_caller as gtc
from app.services.react_loop import run_react_loop
from app.services.tools import TOOL_SCHEMAS, ToolContext

from .conftest import make_profile_in


def fake_httpx_response(status_code: int, json_body: dict):
    m = MagicMock()
    m.status_code = status_code
    m.text = json.dumps(json_body, ensure_ascii=False)
    m.json.return_value = json_body
    return m


# 这两份响应结构，字段名（candidates/content/parts/usageMetadata/promptTokenCount等）
# 都是之前真实调用 Gemini 成功时观察到的原样结构，不是随便编的。
FUNCTION_CALL_RESPONSE = {
    "candidates": [{
        "content": {
            "parts": [{
                "functionCall": {"name": "search_candidates", "args": {"major_keyword": "计算机"}},
                "thoughtSignature": "fake-signature-abc123",
            }],
            "role": "model",
        },
        "finishReason": "STOP",
    }],
    "usageMetadata": {"promptTokenCount": 60, "candidatesTokenCount": 20, "totalTokenCount": 80},
}

FINAL_TEXT_RESPONSE = {
    "candidates": [{
        "content": {"parts": [{"text": "根据检索结果，推荐东南大学计算机类专业组。"}], "role": "model"},
        "finishReason": "STOP",
    }],
    "usageMetadata": {"promptTokenCount": 90, "candidatesTokenCount": 30, "totalTokenCount": 120},
}


async def test_tool_call_then_final_text_round_trip(db):
    call_count = 0
    captured_second_request_body = {}

    async def fake_post(self, url, headers=None, json=None, timeout=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return fake_httpx_response(200, FUNCTION_CALL_RESPONSE)
        captured_second_request_body.update(json or {})
        return fake_httpx_response(200, FINAL_TEXT_RESPONSE)

    ctx = ToolContext(db=db, profile=make_profile_in())

    with patch("httpx.AsyncClient.post", new=fake_post):
        text, trace = await run_react_loop(
            agent_name="retrieval", system_prompt="test",
            initial_message="查一下计算机相关专业",
            ctx=ctx, model_caller=gtc.call_gemini_with_tools, tool_schemas=TOOL_SCHEMAS,
        )

    assert trace.status == "completed", f"应该正常完成，实际: {trace.status}"
    assert trace.total_steps == 2, f"应该2步，实际 {trace.total_steps}"
    assert trace.total_tokens == 80 + 120, f"token应该是200，实际 {trace.total_tokens}"
    assert "东南大学" in text, f"最终回复里应该有东南大学，实际: {text}"
    # 关键校验：第一步的工具调用是真实打到数据库的，不是也被mock掉了
    assert trace.steps[0].tool_result["data"]["count"] == 10, "第一步应该真实查到10个候选（来自真实数据库）"

    # 关键校验：第二轮请求体里，回填的 functionCall part 必须原样带上 thoughtSignature，
    # 这是真实撞到 400 "missing thought_signature" 之后补上的修复，没有这条断言，
    # 代码改错了也测不出来。
    second_contents = captured_second_request_body.get("contents", [])
    model_turn = next((c for c in second_contents if c.get("role") == "model"), None)
    assert model_turn is not None, "第二轮请求体里应该有一条 role=model 的回填记录"
    fc_part = model_turn["parts"][0]
    assert fc_part.get("thoughtSignature") == "fake-signature-abc123", (
        f"thoughtSignature 没有被正确回填，实际 part: {fc_part}"
    )
