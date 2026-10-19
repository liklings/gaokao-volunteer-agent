"""
/api/plans 接口链路：确认方案 -> 改条件生成新版本 -> 查看diff -> 导出文本
-> 边界情况（客户端伪造的 rank_diff 必须被服务器忽略）。不需要真实 Gemini Key。

从 scripts/test_plans_manual.py 迁移：原脚本要求一个已经在跑的 uvicorn，
用裸的 httpx 打 localhost:8000；这里改用 FastAPI 的 TestClient 直接内嵌调用，
不再需要额外起一个真实服务器，跑起来更快也更稳。

group_id 从数据库里真实查出来，不写死在测试里，避免种子数据变化后测试
悄悄地测的是不存在的候选（这条踩坑经验来自 test_advisor_scripted.py 里
类似的修复）。
"""
from fastapi.testclient import TestClient

from app.main import app
from app.models import Profile
from app.services.tools import ToolContext, _tool_search_candidates

from .conftest import make_profile_in

client = TestClient(app)


def _make_profile_row(db, user_id: int) -> int:
    profile = Profile(
        user_id=user_id, province="江苏", year=2026, track="物理类",
        subjects=["物理", "化学"], score=637, rank=5200,
        major_keyword="计算机", budget_max=None,
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile.id


def _fetch_real_group_ids(db, n: int) -> list[str]:
    ctx = ToolContext(db=db, profile=make_profile_in())
    result = _tool_search_candidates({}, ctx)
    assert result.ok and result.data["count"] >= n, f"种子数据里至少要有{n}条候选才能测这个场景"
    return [c["group_id"] for c in result.data["candidates"][:n]]


def test_plan_versions_diff_export_and_server_recomputes_rank_diff(db, auth_headers):
    user_id = client.get("/api/auth/me", headers=auth_headers).json()["id"]
    profile_id = _make_profile_row(db, user_id)
    group_ids = _fetch_real_group_ids(db, 3)

    r1 = client.post(
        "/api/plans", json={"profile_id": profile_id, "group_ids": group_ids[:2]}, headers=auth_headers,
    )
    assert r1.status_code == 200, r1.text
    plan1 = r1.json()
    assert plan1["version_n"] == 1
    assert plan1["diff"] == [], "第一版方案不应该有diff（没有更早的版本可比）"

    r2 = client.post(
        "/api/plans", json={"profile_id": profile_id, "group_ids": [group_ids[1], group_ids[2]]},
        headers=auth_headers,
    )
    assert r2.status_code == 200, r2.text
    plan2 = r2.json()
    assert plan2["version_n"] == 2
    assert plan2["diff"], "换了候选，第二版应该产生diff"

    r3 = client.get(f"/api/plans/{plan2['id']}/export", headers=auth_headers)
    assert r3.status_code == 200
    assert r3.text, "导出文本不应该是空的"

    # 边界测试：客户端伪造的 rank_diff 应该被服务器忽略，用真实重新算出来的值
    r4 = client.post(
        "/api/plans",
        json={"profile_id": profile_id, "group_ids": [group_ids[0]], "rank_diff": 99999},
        headers=auth_headers,
    )
    assert r4.status_code == 200, r4.text
    real_diff = r4.json()["candidates"][0]["rank_diff"]
    assert real_diff != 99999, "服务器应该重新计算 rank_diff，不能直接采信客户端传入的伪造值"
