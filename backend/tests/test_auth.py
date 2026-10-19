"""
账号体系（方向⑤）：注册/登录/token 校验，以及最关键的一条——
一个用户绝对不能通过 profile_id/plan_id 碰到另一个用户的数据
（多用户数据隔离，这是这次改动的核心安全目标）。

不需要真实 Gemini Key，chat 相关场景用 mock httpx 拦掉网络请求。
"""
import json as json_mod
import uuid
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _unique_email(prefix: str = "user") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}@example.com"


def _register(email: str, password: str = "testpass123") -> dict:
    r = client.post("/api/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    return r.json()


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_register_returns_token_and_user(db):
    body = _register(_unique_email())
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["user"]["id"] > 0


def test_register_duplicate_email_rejected(db):
    email = _unique_email()
    _register(email)
    r = client.post("/api/auth/register", json={"email": email, "password": "anotherpass123"})
    assert r.status_code == 409


def test_register_short_password_rejected(db):
    r = client.post("/api/auth/register", json={"email": _unique_email(), "password": "short"})
    assert r.status_code == 422


def test_login_with_correct_password_succeeds(db):
    email = _unique_email()
    _register(email, password="correcthorse123")
    r = client.post("/api/auth/login", json={"email": email, "password": "correcthorse123"})
    assert r.status_code == 200
    assert r.json()["access_token"]


def test_login_with_wrong_password_rejected(db):
    email = _unique_email()
    _register(email, password="correcthorse123")
    r = client.post("/api/auth/login", json={"email": email, "password": "wrongpassword"})
    assert r.status_code == 401


def test_login_with_nonexistent_email_rejected(db):
    r = client.post("/api/auth/login", json={"email": _unique_email(), "password": "whatever123"})
    assert r.status_code == 401


def test_me_requires_valid_token(db):
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/me", headers=_auth_headers("not-a-real-token")).status_code == 401

    token = _register(_unique_email())["access_token"]
    r = client.get("/api/auth/me", headers=_auth_headers(token))
    assert r.status_code == 200


def test_protected_endpoints_reject_missing_token(db):
    assert client.post("/api/chat", json={"message": "hi"}).status_code == 401
    assert client.post(
        "/api/candidates/search",
        json={"province": "江苏", "track": "物理类", "subjects": ["物理", "化学"],
              "score": 637, "rank": 5200, "major_keyword": "计算机"},
    ).status_code == 401
    assert client.post("/api/plans", json={"profile_id": 1, "group_ids": ["seu-cs-04"]}).status_code == 401


def test_users_cannot_access_each_others_profiles_via_chat(db):
    token_a = _register(_unique_email("owner"))["access_token"]
    token_b = _register(_unique_email("intruder"))["access_token"]

    async def fake_post(self, url, headers=None, json=None, timeout=None):
        m = MagicMock()
        m.status_code = 200
        m.text = "OK"
        payload = {"reply": "已记录", "profile": {"province": "江苏"}, "conflicts": []}
        m.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": json_mod.dumps(payload, ensure_ascii=False)}]}}]
        }
        return m

    with patch("httpx.AsyncClient.post", new=fake_post):
        r_owner = client.post("/api/chat", json={"message": "我是江苏考生"}, headers=_auth_headers(token_a))
    assert r_owner.status_code == 200
    profile_id = r_owner.json()["profile_id"]

    # 陌生用户拿这个 profile_id 去追问，应该被当成"不存在"，不能续写别人的对话
    with patch("httpx.AsyncClient.post", new=fake_post):
        r_intruder = client.post(
            "/api/chat", json={"profile_id": profile_id, "message": "继续"}, headers=_auth_headers(token_b),
        )
    assert r_intruder.status_code == 404

    # profile 本人可以正常续写，证明 404 是权限问题而不是这个 profile_id 本身有问题
    with patch("httpx.AsyncClient.post", new=fake_post):
        r_owner_again = client.post(
            "/api/chat", json={"profile_id": profile_id, "message": "继续"}, headers=_auth_headers(token_a),
        )
    assert r_owner_again.status_code == 200


def test_users_cannot_access_each_others_plans(db):
    from app.models import Profile

    token_a = _register(_unique_email("owner"))["access_token"]
    token_b = _register(_unique_email("intruder"))["access_token"]

    # 拿到 token_a 对应的 user_id，直接建一条属于它的 profile（跳过 chat，减少这个测试对 Gemini mock 的依赖）
    me = client.get("/api/auth/me", headers=_auth_headers(token_a)).json()
    profile = Profile(
        user_id=me["id"], province="江苏", year=2026, track="物理类",
        subjects=["物理", "化学"], score=637, rank=5200, major_keyword="计算机",
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)

    from app.services.tools import ToolContext, _tool_search_candidates
    from .conftest import make_profile_in
    ctx = ToolContext(db=db, profile=make_profile_in())
    result = _tool_search_candidates({}, ctx)
    group_id = result.data["candidates"][0]["group_id"]

    # 陌生用户不能用别人的 profile_id 确认方案
    r_intruder_confirm = client.post(
        "/api/plans", json={"profile_id": profile.id, "group_ids": [group_id]}, headers=_auth_headers(token_b),
    )
    assert r_intruder_confirm.status_code == 404

    # 本人可以正常确认
    r_owner_confirm = client.post(
        "/api/plans", json={"profile_id": profile.id, "group_ids": [group_id]}, headers=_auth_headers(token_a),
    )
    assert r_owner_confirm.status_code == 200
    plan_id = r_owner_confirm.json()["id"]

    # 陌生用户不能查看/导出别人的方案
    assert client.get(f"/api/plans/{plan_id}", headers=_auth_headers(token_b)).status_code == 404
    assert client.get(f"/api/plans/{plan_id}/export", headers=_auth_headers(token_b)).status_code == 404

    # 本人可以正常查看/导出
    assert client.get(f"/api/plans/{plan_id}", headers=_auth_headers(token_a)).status_code == 200
    assert client.get(f"/api/plans/{plan_id}/export", headers=_auth_headers(token_a)).status_code == 200


def test_export_plan_accepts_query_string_token(db):
    """
    /api/plans/{id}/export 这一个接口特殊：前端用 <a href target=_blank> 直接打开
    下载，没法带 Authorization 请求头，所以额外支持 ?token= 查询参数传 token
    （见 get_current_user_allow_query_token）。这里单独验证这条口子本身：
    正确 token 能用、没 token 不行、别人的 token 拿不到你的方案。
    """
    from app.models import Profile
    from app.services.tools import ToolContext, _tool_search_candidates

    from .conftest import make_profile_in

    token_owner = _register(_unique_email("export-owner"))["access_token"]
    token_stranger = _register(_unique_email("export-stranger"))["access_token"]

    me = client.get("/api/auth/me", headers=_auth_headers(token_owner)).json()
    profile = Profile(
        user_id=me["id"], province="江苏", year=2026, track="物理类",
        subjects=["物理", "化学"], score=637, rank=5200, major_keyword="计算机",
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)

    ctx = ToolContext(db=db, profile=make_profile_in())
    group_id = _tool_search_candidates({}, ctx).data["candidates"][0]["group_id"]

    plan_resp = client.post(
        "/api/plans", json={"profile_id": profile.id, "group_ids": [group_id]}, headers=_auth_headers(token_owner),
    )
    assert plan_resp.status_code == 200
    plan_id = plan_resp.json()["id"]

    assert client.get(f"/api/plans/{plan_id}/export?token={token_owner}").status_code == 200
    assert client.get(f"/api/plans/{plan_id}/export").status_code == 401
    assert client.get(f"/api/plans/{plan_id}/export?token={token_stranger}").status_code == 404
