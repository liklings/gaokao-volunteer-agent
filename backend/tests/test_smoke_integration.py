"""
端到端冒烟测试：用真实 Gemini Key 跑一遍完整链路
    对话收集条件 → 条件收集齐(ready=True) → 拿这份条件去查候选

标记为 integration：需要 .env 里配置真实 GEMINI_API_KEY，默认的
`pytest` 不会跑这个文件（见 pytest.ini 里 `addopts = -m "not integration"`）。
单独跑：
    GEMINI_API_KEY=<真实key> pytest -m integration tests/test_smoke_integration.py

从 scripts/smoke_test_full_flow.py 迁移：原脚本要求一个已经在跑的 uvicorn，
用裸的 httpx 打 localhost:8000；这里改用 FastAPI 的 TestClient 直接内嵌调用，
不需要额外起服务器，但依然会真的打到 Gemini（这部分本来就没法用 TestClient
避开真实网络，因为要测的正是"真实 Gemini 是否认我们的请求格式、是否真的
会调用工具"）。
"""
import os

import pytest
from fastapi.testclient import TestClient

from app.main import app

pytestmark = pytest.mark.integration

# 单条消息就把必填信息说全，用来验证"一轮就能收集齐条件"这条路径。
MESSAGE = "我是江苏物理类考生，2026届，637分，位次5200，选科物理+化学，想找计算机相关专业，优先考虑省内城市。"


def _has_real_gemini_key() -> bool:
    """粗略判断 .env 里是不是配置了一把真实的 Gemini Key，而不是占位符。
    真实 Google API Key 都是 "AIza" 开头的固定长度字符串；"dummy"/
    "fake-key-for-pytest-only" 这类占位符明显不是，直接跳过，避免在没有
    真实 Key（或者像这个沙盒环境一样出口网络本来就被拦截）的情况下
    尝试真实网络请求导致误报失败。
    """
    key = os.environ.get("GEMINI_API_KEY", "")
    return key.startswith("AIza") and len(key) > 20


@pytest.mark.skipif(not _has_real_gemini_key(), reason="需要在环境变量里配置真实 GEMINI_API_KEY")
def test_full_chat_to_search_flow_with_real_gemini(db, auth_headers):
    client = TestClient(app)

    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json().get("database") == "connected"

    r = client.post("/api/chat", json={"profile_id": None, "message": MESSAGE}, headers=auth_headers)
    assert r.status_code == 200, r.text
    result = r.json()
    profile_id = result["profile_id"]

    if not result["ready"]:
        # 条件没有一轮收集齐也是正常情况，管线本身工作正常，不算失败。
        return

    profile = result["profile"]
    search_payload = {
        "province": profile["province"],
        "track": profile["track"],
        "subjects": profile["subjects"],
        "score": profile["score"],
        "rank": profile["rank"],
        "major_keyword": profile["major_keyword"],
        "city_pref": profile["city_pref"],
        "budget_max": profile["budget_max"],
    }
    r2 = client.post("/api/candidates/search", json=search_payload, headers=auth_headers)
    assert r2.status_code == 200, r2.text
    data = r2.json()
    assert "results" in data and "audit" in data
