"""
调用 Gemini Embedding API，把文本转成向量。跟 gemini_tool_caller.py（负责对话/
function calling）是两个独立的客户端，故意不合并——这两件事用的是完全不同的
Gemini 接口（:embedContent / :batchEmbedContents vs :generateContent），而且
以后如果对话模型换掉（比如换成 Qwen），这个文件完全不用动：只要 policy_chunks
表里存的向量还是用这里的 embedding 模型生成的，检索就还能正常工作，"对话模型"
和"embedding 模型"是两条独立的、可以分别替换的依赖。

taskType 区分"给文档建索引"（RETRIEVAL_DOCUMENT）还是"给用户的问题做检索"
（RETRIEVAL_QUERY）——这是 Gemini 官方文档里对检索场景的推荐用法，同一段文本
用不同 taskType 编出来的向量不完全一样，检索质量会更好，不是可有可无的参数。

复用 gemini_errors.py / gemini_retry.py 里已经验证过的异常类型和重试逻辑，
跟 gemini_tool_caller.py 是同一套容错策略：没配 Key 不重试直接报错，网络/
非200 这类"这次运气不好"的问题自动重试。
"""
from __future__ import annotations

import httpx

from app.config import get_settings
from app.services.gemini_errors import GeminiCallError, GeminiConfigError
from app.services.gemini_retry import call_with_retry

_EMBED_ONE_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent"
_EMBED_BATCH_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:batchEmbedContents"

# 单次 batchEmbedContents 请求最多放多少段文本——避免一次性把几百个 chunk 全塞进
# 一个请求体导致超时或触发 API 侧的单请求大小限制，具体数字没有官方硬性规定，
# 100 是一个留了余量的保守值。
MAX_BATCH_SIZE = 100

TaskType = str  # "RETRIEVAL_DOCUMENT" | "RETRIEVAL_QUERY"


def _require_api_key() -> tuple[str, str, int]:
    settings = get_settings()
    if not settings.gemini_api_key:
        raise GeminiConfigError("未配置 GEMINI_API_KEY，无法调用 Gemini Embedding API。")
    return settings.gemini_api_key, settings.policy_embedding_model, settings.policy_embedding_dimensions


async def _embed_text_once(text: str, task_type: TaskType) -> list[float]:
    api_key, model, dimensions = _require_api_key()
    body = {
        "content": {"parts": [{"text": text}]},
        "taskType": task_type,
        "outputDimensionality": dimensions,
    }
    url = _EMBED_ONE_ENDPOINT.format(model=model)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url, headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                json=body, timeout=30.0,
            )
    except httpx.HTTPError as exc:
        raise GeminiCallError(f"请求 Gemini Embedding API 失败（网络层，{type(exc).__name__}）：{exc}") from exc

    if resp.status_code != 200:
        raise GeminiCallError(f"Gemini Embedding API 返回 {resp.status_code}：{resp.text[:2000]}")

    try:
        return resp.json()["embedding"]["values"]
    except (KeyError, TypeError) as exc:
        raise GeminiCallError(f"Embedding 响应里没有找到 embedding/values：{resp.text[:2000]}") from exc


async def embed_text(text: str, task_type: TaskType = "RETRIEVAL_QUERY") -> list[float]:
    """给单段文本生成向量——检索时用（把用户问题转成向量），量小，不值得走批量接口。"""
    return await call_with_retry(lambda: _embed_text_once(text, task_type))


async def _embed_batch_once(texts: list[str], task_type: TaskType) -> list[list[float]]:
    api_key, model, dimensions = _require_api_key()
    body = {
        "requests": [
            {
                "model": f"models/{model}",
                "content": {"parts": [{"text": t}]},
                "taskType": task_type,
                "outputDimensionality": dimensions,
            }
            for t in texts
        ]
    }
    url = _EMBED_BATCH_ENDPOINT.format(model=model)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url, headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                json=body, timeout=60.0,
            )
    except httpx.HTTPError as exc:
        raise GeminiCallError(f"请求 Gemini Embedding API（批量）失败（网络层，{type(exc).__name__}）：{exc}") from exc

    if resp.status_code != 200:
        raise GeminiCallError(f"Gemini Embedding API（批量）返回 {resp.status_code}：{resp.text[:2000]}")

    try:
        embeddings = resp.json()["embeddings"]
        return [e["values"] for e in embeddings]
    except (KeyError, TypeError) as exc:
        raise GeminiCallError(f"批量 Embedding 响应格式不对：{resp.text[:2000]}") from exc


async def embed_texts(texts: list[str], task_type: TaskType = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
    """给一批文本生成向量——导入政策文档时用（给每个 chunk 建索引），按 MAX_BATCH_SIZE
    分批调用 batchEmbedContents，返回顺序跟输入顺序严格一致（调用方按下标对应）。
    """
    if not texts:
        return []
    results: list[list[float]] = []
    for i in range(0, len(texts), MAX_BATCH_SIZE):
        batch = texts[i : i + MAX_BATCH_SIZE]
        batch_result = await call_with_retry(lambda b=batch: _embed_batch_once(b, task_type))
        results.extend(batch_result)
    return results
