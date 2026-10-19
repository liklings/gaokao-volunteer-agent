"""
/api/chat 流程：用假的 Gemini 响应验证"多轮对话正确合并条件、不覆盖已收集
字段、missing/ready 计算正确"这条核心链路，不需要真实网络。

从 scripts/test_chat_stub.py 迁移（原脚本是打印结果人工看，这里改成断言）。

注意：mock 的目标是 httpx.AsyncClient.post 本身（而不是某个具体模块里的
httpx.post），因为 agent_client.py 用的是异步 httpx.AsyncClient().post()。
"""
import json
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _fake_response(payload: dict):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "OK"
    mock_resp.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": json.dumps(payload, ensure_ascii=False)}]}}]
    }
    return mock_resp


async def _fake_gemini_response_1(self, url, headers=None, json=None, timeout=None):
    return _fake_response({
        "reply": "好的，已记录你的省份和科类，还需要补充选科组合和专业方向。",
        "profile": {"province": "江苏", "track": "物理类"},
        "conflicts": [],
    })


async def _fake_gemini_response_2(self, url, headers=None, json=None, timeout=None):
    return _fake_response({
        "reply": "了解，我记录了你的选科和专业方向，条件已经收集齐了。",
        "profile": {"subjects": ["物理", "化学"], "major_keyword": "计算机", "score": 637, "rank": 5200},
        "conflicts": [],
    })


def test_chat_multi_turn_merges_profile_without_overwriting(db, auth_headers):
    with patch("httpx.AsyncClient.post", new=_fake_gemini_response_1):
        r1 = client.post(
            "/api/chat", json={"message": "我是江苏物理类考生，想找计算机相关专业"}, headers=auth_headers,
        )
    assert r1.status_code == 200
    body1 = r1.json()
    assert body1["profile"]["province"] == "江苏"
    assert body1["profile"]["track"] == "物理类"
    assert body1["ready"] is False

    pid = body1["profile_id"]

    with patch("httpx.AsyncClient.post", new=_fake_gemini_response_2):
        r2 = client.post(
            "/api/chat",
            json={"profile_id": pid, "message": "选科物理+化学，想学计算机，637分位次5200"},
            headers=auth_headers,
        )
    assert r2.status_code == 200
    body2 = r2.json()
    # 第一轮收集的字段应该还在（没被第二轮覆盖/丢失）
    assert body2["profile"]["province"] == "江苏"
    assert body2["profile"]["track"] == "物理类"
    # 第二轮新收集的字段应该合并进来
    assert body2["profile"]["subjects"] == ["物理", "化学"]
    assert body2["profile"]["major_keyword"] == "计算机"
    assert body2["ready"] is True
    assert body2["missing"] == []
