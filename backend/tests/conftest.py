"""
pytest 全局 fixtures。

设计取舍：这套测试直接对接真实 Postgres（跟 scripts/ 目录下那些人工验证过、
已经跑通的脚本用的是同一个数据库、同一份种子数据），不用 sqlite/内存库，
也不做"每个用例一个事务、跑完自动回滚"这种隔离——这些用例大多是从已经
验证正确的手动脚本原样迁移过来的，脚本本身就是靠真实自增 id 互相区分，
迁移成 pytest 用例后维持同样的运行方式，改动面最小、行为跟脚本时代完全一致。

跑之前要求（跟 scripts/ 目录下的脚本一样）：
- 数据库已经跑过 `alembic upgrade head`
- 已经执行过 `python -m app.services.seed_loader` 导入种子数据

标记为 @pytest.mark.integration 的用例需要真实的 GEMINI_API_KEY 和真实网络，
默认的 `pytest` 不会跑这些（见 pytest.ini），要单独跑用 `pytest -m integration`。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 必须在 import app.config 之前设置：所有非 integration 用例都靠 mock 拦截
# httpx 请求，不会真的用这个 key 发网络请求，但 Settings 校验需要它"看起来存在"。
os.environ.setdefault("GEMINI_API_KEY", "fake-key-for-pytest-only")
# jwt_secret 在 Settings 里是必填项（没有默认值），本地 .env 一般已经配了一个，
# 这里只是给"直接跑 pytest、没配 .env"这种边界情况兜底，不影响真实部署的密钥。
os.environ.setdefault("JWT_SECRET", "test-only-jwt-secret-not-for-production")

import pytest  # noqa: E402

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.db import SessionLocal  # noqa: E402
from app.schemas.candidate import ProfileIn  # noqa: E402


@pytest.fixture
def db():
    """每个用例一个独立的真实数据库 session，用完关闭。"""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def auth_headers():
    """注册一个全新的随机用户，返回可以直接放进 headers 里的
    `{"Authorization": "Bearer <token>"}`。每个用例调用一次就是一个新用户，
    不会跟其他用例的数据互相干扰。
    """
    import uuid

    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    email = f"fixture-user-{uuid.uuid4().hex[:10]}@example.com"
    r = client.post("/api/auth/register", json={"email": email, "password": "fixture-pass-123"})
    assert r.status_code == 201, f"测试 fixture 注册用户失败: {r.status_code} {r.text}"
    token = r.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def profile_in() -> ProfileIn:
    """标准测试考生画像：江苏物理类 637分 位次5200 计算机方向。"""
    return make_profile_in()


def make_profile_in(**overrides) -> ProfileIn:
    """跟 profile_in fixture 用同一份默认值，需要局部覆盖字段时用这个。"""
    base = dict(
        province="江苏", track="物理类", subjects=["物理", "化学"],
        score=637, rank=5200, major_keyword="计算机",
    )
    base.update(overrides)
    return ProfileIn(**base)
