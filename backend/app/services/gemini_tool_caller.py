"""
真实的 Gemini function calling 客户端，实现 react_loop.py 里定义的 ModelCaller 接口。

已经用 scripts/test_gemini_function_calling_minimal.py 和
scripts/test_gemini_function_calling_roundtrip.py 两个独立脚本，
拿真实 Gemini API 验证过：模型能正确识别该调用哪个工具、
把工具结果通过 functionResponse（role 用 "user"）喂回去后，
模型能正确读懂并在最终回复里引用——这个实现里的请求/响应格式是对的。

【从真实测试中得到的一条重要经验，已经体现在 agent_prompts.py 里】
不给系统提示词明确约束"什么时候必须调用工具"时，模型碰到能用自己
知识库回答的问题，会倾向于直接给文字回复、不触发工具调用——哪怕工具
描述写得再清楚也不够，必须在 systemInstruction 里显式写清楚
"这类问题必须调用工具、不许凭自己的知识编造"。
"""
from __future__ import annotations

import httpx

from app.config import get_settings
from app.services.gemini_errors import GeminiCallError, GeminiConfigError
from app.services.gemini_retry import call_with_retry
from app.services.react_loop import ModelResponse

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _extract_response(data: dict) -> ModelResponse:
    """
    解析 Gemini 的原始响应。thoughtSignature 是 Gemini 3 系列模型新加的字段
    （见 https://ai.google.dev/gemini-api/docs/thought-signatures），出现在
    functionCall 所在的 part 里，必须原样提取出来，下一轮通过 react_loop.py
    喂回去，不然会报400 "missing thought_signature"（这是真实测试撞到过的坑，
    不是提前臆测的）。
    """
    usage = data.get("usageMetadata", {})
    tokens_used = usage.get("promptTokenCount", 0) + usage.get("candidatesTokenCount", 0)

    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError) as exc:
        raise GeminiCallError(f"响应里没有找到 candidates/content/parts：{data}") from exc

    for part in parts:
        if "functionCall" in part:
            fc = part["functionCall"]
            return ModelResponse(
                is_final_answer=False,
                tool_name=fc.get("name"),
                tool_args=fc.get("args") or {},
                tokens_used=tokens_used,
                thought_signature=part.get("thoughtSignature"),
            )

    # 没有任何一个 part 带 functionCall，说明模型给出的是最终文字回复。
    # 文本回复的最后一个 part 有时也会带 thoughtSignature（官方文档：不带不会
    # 报错，纯粹是为了保持推理连贯性的性能建议），这里顺手一并提取。
    text = "".join(p.get("text", "") for p in parts)
    trailing_signature = parts[-1].get("thoughtSignature") if parts else None
    return ModelResponse(
        is_final_answer=True, text=text, tokens_used=tokens_used,
        thought_signature=trailing_signature,
    )


async def _call_gemini_with_tools_once(system_prompt: str, contents: list[dict], tool_schemas: list[dict]) -> ModelResponse:
    """真正发一次请求。call_gemini_with_tools()（下面）在这外面包了一层自动重试。"""
    settings = get_settings()
    if not settings.gemini_api_key:
        raise GeminiConfigError("未配置 GEMINI_API_KEY，无法调用真实 Gemini function calling。")

    body = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
        "tools": [{"functionDeclarations": tool_schemas}],
    }

    url = GEMINI_ENDPOINT.format(model=settings.gemini_model)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                headers={"x-goog-api-key": settings.gemini_api_key, "Content-Type": "application/json"},
                json=body,
                timeout=60.0,
            )
    except httpx.HTTPError as exc:
        raise GeminiCallError(
            f"请求 Gemini function calling 失败（网络层，{type(exc).__name__}）：{exc}"
        ) from exc

    if resp.status_code != 200:
        raise GeminiCallError(f"Gemini function calling 返回 {resp.status_code}：{resp.text[:2000]}")

    return _extract_response(resp.json())


async def call_gemini_with_tools(system_prompt: str, contents: list[dict], tool_schemas: list[dict]) -> ModelResponse:
    """对外入口，带自动重试，规则跟 agent_client.call_agent 一致。"""
    return await call_with_retry(lambda: _call_gemini_with_tools_once(system_prompt, contents, tool_schemas))
