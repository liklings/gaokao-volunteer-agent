"""
Mock 测试：验证 embeddings.py 的请求构造（taskType/outputDimensionality 有没有
带对）、单条/批量响应解析、批量顺序保留、错误处理——不依赖真实网络、不需要
真实 Gemini Key。跟 test_gemini_tool_caller.py 用同一套 fake_httpx_response 手法。

不测的：真实 Gemini 收到这个请求后是否真的返回符合预期的向量——那要靠
标记为 integration 的真实网络测试确认。
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from app.services import embeddings
from app.services.gemini_errors import GeminiCallError, GeminiConfigError


def fake_httpx_response(status_code: int, json_body: dict):
    m = MagicMock()
    m.status_code = status_code
    m.text = json.dumps(json_body, ensure_ascii=False)
    m.json.return_value = json_body
    return m


async def test_embed_text_sends_task_type_and_dimensions_and_parses_response():
    captured = {}

    async def fake_post(self, url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return fake_httpx_response(200, {"embedding": {"values": [0.1, 0.2, 0.3]}})

    with patch("httpx.AsyncClient.post", new=fake_post):
        result = await embeddings.embed_text("江苏省高考报名条件", task_type="RETRIEVAL_QUERY")

    assert result == [0.1, 0.2, 0.3]
    assert captured["json"]["taskType"] == "RETRIEVAL_QUERY"
    assert captured["json"]["outputDimensionality"] == 768
    assert captured["json"]["content"]["parts"][0]["text"] == "江苏省高考报名条件"
    assert ":embedContent" in captured["url"]
    assert captured["headers"]["x-goog-api-key"]


async def test_embed_text_default_task_type_is_retrieval_query():
    async def fake_post(self, url, headers=None, json=None, timeout=None):
        assert json["taskType"] == "RETRIEVAL_QUERY"
        return fake_httpx_response(200, {"embedding": {"values": [0.5]}})

    with patch("httpx.AsyncClient.post", new=fake_post):
        await embeddings.embed_text("随便什么问题")


async def test_embed_texts_uses_retrieval_document_task_type_and_batch_endpoint():
    captured = {}

    async def fake_post(self, url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return fake_httpx_response(
            200,
            {"embeddings": [{"values": [1.0, 0.0]}, {"values": [0.0, 1.0]}]},
        )

    with patch("httpx.AsyncClient.post", new=fake_post):
        result = await embeddings.embed_texts(["第一段政策原文", "第二段政策原文"])

    assert result == [[1.0, 0.0], [0.0, 1.0]]
    assert ":batchEmbedContents" in captured["url"]
    reqs = captured["json"]["requests"]
    assert len(reqs) == 2
    assert all(r["taskType"] == "RETRIEVAL_DOCUMENT" for r in reqs)
    assert reqs[0]["content"]["parts"][0]["text"] == "第一段政策原文"
    assert reqs[1]["content"]["parts"][0]["text"] == "第二段政策原文"


async def test_embed_texts_preserves_order_across_batches():
    # MAX_BATCH_SIZE=100，构造 120 段文本，验证分两批调用后拼回来的顺序仍然
    # 跟输入顺序一致——调用方（policy_ingest.py）是按下标把向量对应回 chunk 的，
    # 顺序错了会导致每个 chunk 存进错误的向量，且不会报任何错误，必须专门测。
    texts = [f"chunk-{i}" for i in range(120)]
    call_count = 0

    async def fake_post(self, url, headers=None, json=None, timeout=None):
        nonlocal call_count
        call_count += 1
        reqs = json["requests"]
        # 用请求里文本内容里的下标编号构造出对应的向量，这样最后能验证顺序没被打乱
        values = [[float(int(r["content"]["parts"][0]["text"].split("-")[1]))] for r in reqs]
        return fake_httpx_response(200, {"embeddings": [{"values": v} for v in values]})

    with patch("httpx.AsyncClient.post", new=fake_post):
        result = await embeddings.embed_texts(texts)

    assert call_count == 2, f"120条应该分两批调用（MAX_BATCH_SIZE=100），实际调用了{call_count}次"
    assert [int(v[0]) for v in result] == list(range(120)), "批量拼接后顺序跟输入不一致"


async def test_embed_texts_empty_list_short_circuits_without_network_call():
    async def fake_post(self, url, headers=None, json=None, timeout=None):
        raise AssertionError("空列表不应该发起任何网络请求")

    with patch("httpx.AsyncClient.post", new=fake_post):
        result = await embeddings.embed_texts([])

    assert result == []


async def test_non_200_response_raises_gemini_call_error():
    async def fake_post(self, url, headers=None, json=None, timeout=None):
        return fake_httpx_response(500, {"error": {"message": "internal error"}})

    with patch("httpx.AsyncClient.post", new=fake_post):
        with pytest.raises(GeminiCallError):
            await embeddings.embed_text("测试文本")


async def test_malformed_single_response_raises_gemini_call_error():
    async def fake_post(self, url, headers=None, json=None, timeout=None):
        return fake_httpx_response(200, {"unexpected": "shape"})

    with patch("httpx.AsyncClient.post", new=fake_post):
        with pytest.raises(GeminiCallError):
            await embeddings.embed_text("测试文本")


async def test_malformed_batch_response_raises_gemini_call_error():
    async def fake_post(self, url, headers=None, json=None, timeout=None):
        return fake_httpx_response(200, {"unexpected": "shape"})

    with patch("httpx.AsyncClient.post", new=fake_post):
        with pytest.raises(GeminiCallError):
            await embeddings.embed_texts(["a", "b"])


async def test_network_error_raises_gemini_call_error():
    import httpx as real_httpx

    async def fake_post(self, url, headers=None, json=None, timeout=None):
        raise real_httpx.ConnectTimeout("timed out")

    with patch("httpx.AsyncClient.post", new=fake_post):
        with pytest.raises(GeminiCallError):
            await embeddings.embed_text("测试文本")


async def test_missing_api_key_raises_config_error_without_network_call(monkeypatch):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "gemini_api_key", None)
    get_settings.cache_clear()
    monkeypatch.setattr(embeddings, "get_settings", lambda: settings)

    async def fake_post(self, url, headers=None, json=None, timeout=None):
        raise AssertionError("没配 Key 时不应该发起网络请求")

    with patch("httpx.AsyncClient.post", new=fake_post):
        with pytest.raises(GeminiConfigError):
            await embeddings.embed_text("测试文本")
