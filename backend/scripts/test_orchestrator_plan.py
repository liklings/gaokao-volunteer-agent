"""
Orchestrator 规划逻辑的真实测试——_build_plan() 是纯函数，不碰网络，
用真实数据库里不同完整度的 Profile 行测试它的分支判断对不对。
这部分完全不需要 Gemini，跟 API 额度无关。

run_orchestrator() 里真正执行 profile/retrieval 两步需要真实调用 Gemini，
这个脚本不测那部分——那部分等额度恢复后用单独的端到端测试脚本验证。

用法（backend/ 目录下，激活 venv，数据库已经跑通迁移+种子数据）：
    python scripts/test_orchestrator_plan.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal
from app.models import Profile
from app.services.orchestrator import _build_plan


def main() -> int:
    db = SessionLocal()

    print("=== 场景1：全新的空 Profile，应该走 profile 分支 ===")
    empty_profile = Profile()
    db.add(empty_profile)
    db.commit()
    db.refresh(empty_profile)
    plan = _build_plan(empty_profile)
    assert len(plan) == 1
    assert plan[0].agent == "profile"
    assert "省份" in plan[0].goal
    print(f"通过。plan[0].goal = {plan[0].goal!r}")

    print("\n=== 场景2：只缺一个字段（专业关键词），应该还走 profile 分支 ===")
    almost_complete = Profile(
        province="江苏", track="物理类", subjects=["物理", "化学"],
        score=637, rank=5200, major_keyword=None,
    )
    db.add(almost_complete)
    db.commit()
    db.refresh(almost_complete)
    plan = _build_plan(almost_complete)
    assert len(plan) == 1
    assert plan[0].agent == "profile"
    assert "专业关键词" in plan[0].goal
    assert "省份" not in plan[0].goal, "已经有省份了，不该被列进缺失项"
    print(f"通过。plan[0].goal = {plan[0].goal!r}")

    print("\n=== 场景3：条件完全齐全，应该走 retrieval 分支 ===")
    complete_profile = Profile(
        province="江苏", track="物理类", subjects=["物理", "化学"],
        score=637, rank=5200, major_keyword="计算机",
    )
    db.add(complete_profile)
    db.commit()
    db.refresh(complete_profile)
    plan = _build_plan(complete_profile)
    assert len(plan) == 1
    assert plan[0].agent == "retrieval"
    print(f"通过。plan[0].goal = {plan[0].goal!r}")

    print("\n=== 场景4：只有分数没有位次（分数或位次算满足），应该走 retrieval 分支 ===")
    score_only_profile = Profile(
        province="江苏", track="物理类", subjects=["物理", "化学"],
        score=637, rank=None, major_keyword="计算机",
    )
    db.add(score_only_profile)
    db.commit()
    db.refresh(score_only_profile)
    plan = _build_plan(score_only_profile)
    assert len(plan) == 1
    assert plan[0].agent == "retrieval", "只有分数没有位次，'分数或位次'这条应该算满足"
    print(f"通过。plan[0].goal = {plan[0].goal!r}")

    db.close()
    print("\n全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
