"""
/api/chat 流程的桩测试——用假的 Gemini 响应验证"多轮对话正确合并条件、
不覆盖已收集字段、missing/ready 计算正确"这条核心链路，不需要真实网络。

【2026-08-24更新】agent_client.py 在这之前已经从同步 httpx.post 改成了
异步 httpx.AsyncClient().post()，这个脚本原来 mock 的目标
（app.services.agent_client.httpx.post）从那次改动后就已经失效——
不会报错，但会悄悄放行真实网络请求，是个容易被忽略的坏测试。
这次改成 mock httpx.AsyncClient.post 本身，才是真正拦住请求的地方。
"""
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 让脚本能 import app 包

from fastapi.testclient import TestClient

os.environ["GEMINI_API_KEY"] = "fake-key-for-pipeline-test-only"

from app.config import get_settings  # noqa: E402
get_settings.cache_clear()

from app.main import app  # noqa: E402

client = TestClient(app)


def _fake_response(payload: dict):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "OK"
    mock_resp.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": json.dumps(payload, ensure_ascii=False)}]}}]
    }
    return mock_resp


async def fake_gemini_response_1(self, url, headers=None, json=None, timeout=None):
    return _fake_response({
        "reply": "好的，已记录你的省份和科类，还需要补充选科组合和专业方向。",
        "profile": {"province": "江苏", "track": "物理类"},
        "conflicts": [],
    })


async def fake_gemini_response_2(self, url, headers=None, json=None, timeout=None):
    return _fake_response({
        "reply": "了解，我记录了你的选科和专业方向，条件已经收集齐了。",
        "profile": {"subjects": ["物理", "化学"], "major_keyword": "计算机", "score": 637, "rank": 5200},
        "conflicts": [],
    })


with patch("httpx.AsyncClient.post", new=fake_gemini_response_1):
    r1 = client.post("/api/chat", json={"message": "我是江苏物理类考生，想找计算机相关专业"})
print("=== first call ===")
print("status:", r1.status_code)
print(json.dumps(r1.json(), ensure_ascii=False, indent=2))

pid = r1.json()["profile_id"]

with patch("httpx.AsyncClient.post", new=fake_gemini_response_2):
    r2 = client.post(
        "/api/chat",
        json={"profile_id": pid, "message": "选科物理+化学，想学计算机，637分位次5200"},
    )
print("\n=== second call ===")
print("status:", r2.status_code)
print(json.dumps(r2.json(), ensure_ascii=False, indent=2))
