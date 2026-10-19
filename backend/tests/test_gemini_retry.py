"""
自动重试逻辑：不需要 Gemini API，用假的会失败N次再成功的函数直接测
call_with_retry()，再用 mock httpx 测一下真正接进 call_agent() 之后的效果。

从 scripts/test_gemini_retry.py 迁移，断言逻辑原样保留。
"""
import json
from unittest.mock import MagicMock, patch

from app.services.gemini_errors import GeminiCallError, GeminiConfigError
from app.services.gemini_retry import call_with_retry


async def test_succeeds_after_two_failures():
    call_count = 0

    async def flaky():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise GeminiCallError(f"第{call_count}次故意失败")
        return "成功了"

    result = await call_with_retry(flaky, max_retries=2, backoff_seconds=0.01)
    assert result == "成功了"
    assert call_count == 3, f"应该总共调用3次，实际 {call_count}"


async def test_exhausts_retries_and_raises_last_exception():
    call_count = 0

    async def always_fails():
        nonlocal call_count
        call_count += 1
        raise GeminiCallError(f"第{call_count}次失败")

    try:
        await call_with_retry(always_fails, max_retries=2, backoff_seconds=0.01)
        raise AssertionError("应该抛出异常，但没有")
    except GeminiCallError as exc:
        assert call_count == 3, f"应该总共尝试3次（1次+2次重试），实际 {call_count}"
        assert "第3次失败" in str(exc), "抛出的应该是最后一次的异常"


async def test_config_error_is_not_retried():
    call_count = 0

    async def config_broken():
        nonlocal call_count
        call_count += 1
        raise GeminiConfigError("没配Key")

    try:
        await call_with_retry(config_broken, max_retries=2, backoff_seconds=0.01)
        raise AssertionError("应该抛出异常，但没有")
    except GeminiConfigError:
        assert call_count == 1, f"配置错误不该重试，应该只调用1次，实际 {call_count}"


async def test_integration_with_call_agent_retries_on_503():
    """接进真实 call_agent() 之后，模拟第一次503、第二次成功。"""
    from app.services.agent_client import call_agent

    call_count = 0

    def fake_response_503():
        m = MagicMock()
        m.status_code = 503
        m.text = '{"error": {"message": "当前需求量大，请稍后再试"}}'
        return m

    def fake_response_200():
        payload = {"reply": "已记录", "profile": {"province": "江苏"}, "conflicts": []}
        m = MagicMock()
        m.status_code = 200
        m.text = "OK"
        m.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": json.dumps(payload, ensure_ascii=False)}]}}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
        }
        return m

    async def fake_post(self, url, headers=None, json=None, timeout=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return fake_response_503()
        return fake_response_200()

    with patch("httpx.AsyncClient.post", new=fake_post):
        result = await call_agent('{"province": null}', [], "我是江苏考生")

    assert call_count == 2, f"应该第一次503、第二次成功，总共2次调用，实际 {call_count}"
    assert result["profile"]["province"] == "江苏"
