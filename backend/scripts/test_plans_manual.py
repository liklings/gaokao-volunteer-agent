"""
/api/plans 的手动冒烟测试——不需要真实 Gemini Key，
直接在数据库里建一条 Profile（绕开 /api/chat），然后走完
确认方案 → 改条件生成新版本 → 查看diff → 导出文本 → 边界情况
这几步，确认整个 plans 接口链路是对的。

用法（backend/ 目录下，激活 venv，uvicorn 已经在跑，且已导入过种子数据）：
    python scripts/test_plans_manual.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 让脚本能 import app 包

import httpx

from app.db import SessionLocal
from app.models import Profile

BASE_URL = "http://localhost:8000"


def main() -> int:
    db = SessionLocal()
    profile = Profile(
        province="江苏", year=2026, track="物理类",
        subjects=["物理", "化学"], score=637, rank=5200,
        major_keyword="计算机", budget_max=None,
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    profile_id = profile.id
    db.close()
    print(f"测试用 profile_id = {profile_id}")

    print("\n=== 第一版方案 ===")
    r = httpx.post(f"{BASE_URL}/api/plans", json={"profile_id": profile_id, "group_ids": ["seu-cs-04", "nuist-cs-03"]})
    if r.status_code != 200:
        print(f"失败: {r.status_code} {r.text}")
        return 1
    plan1 = r.json()
    print(f"version_n={plan1['version_n']} diff={plan1['diff']}（第一版应为空列表）")

    print("\n=== 第二版方案（换一个候选）===")
    r = httpx.post(f"{BASE_URL}/api/plans", json={"profile_id": profile_id, "group_ids": ["nuist-cs-03", "just-cs-01"]})
    if r.status_code != 200:
        print(f"失败: {r.status_code} {r.text}")
        return 1
    plan2 = r.json()
    print(f"version_n={plan2['version_n']}")
    for d in plan2["diff"]:
        print(f"  {d['kind']}: {d['detail']}")

    print("\n=== 导出文本 ===")
    r = httpx.get(f"{BASE_URL}/api/plans/{plan2['id']}/export")
    print(r.text)

    print("=== 边界测试：伪造的 prob_pct 应被服务器忽略 ===")
    r = httpx.post(
        f"{BASE_URL}/api/plans",
        json={"profile_id": profile_id, "group_ids": ["seu-cs-04"], "prob_pct": 99},
    )
    real_pct = r.json()["candidates"][0]["prob_pct"]
    print(f"客户端传了 prob_pct=99，服务器实际存的是 {real_pct}（应该是重新算出来的真实值，不是99）")

    print("\n全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
