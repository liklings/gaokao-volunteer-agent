"""
验证 app/services/limits.py 的 check_rate_limit()：同一个 profile_id 在
时间窗口内发起过多请求应该被拦下。不碰 Gemini，只操作数据库——直接往
ChatMessage 表插入若干条 role='user' 的记录模拟"这个 profile 最近发起过
几次请求"，然后检查 check_rate_limit() 的判断对不对。

用法（backend/ 目录下，激活 venv，数据库已经跑通迁移）：
    python scripts/test_rate_limit_scripted.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal
from app.models import ChatMessage, Profile
from app.services.limits import RATE_LIMIT_MAX_REQUESTS, RateLimitExceededError, check_rate_limit


def make_profile(db) -> Profile:
    profile = Profile(
        province="江苏", track="物理类", subjects=["物理", "化学"],
        score=637, rank=5200, major_keyword="计算机",
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def scenario_under_limit_passes():
    print(f"=== 场景1：请求次数低于上限（{RATE_LIMIT_MAX_REQUESTS}次），应该正常通过 ===")
    db = SessionLocal()
    try:
        profile = make_profile(db)
        for _ in range(RATE_LIMIT_MAX_REQUESTS - 1):
            db.add(ChatMessage(profile_id=profile.id, role="user", content="测试消息"))
        db.commit()

        check_rate_limit(db, profile.id)  # 不应该抛异常
        print(f"通过。profile_id={profile.id} 已有 {RATE_LIMIT_MAX_REQUESTS - 1} 条记录，未触发限流。")
    finally:
        db.close()


def scenario_at_limit_blocks():
    print(f"\n=== 场景2：请求次数达到上限（{RATE_LIMIT_MAX_REQUESTS}次），应该被拦下 ===")
    db = SessionLocal()
    try:
        profile = make_profile(db)
        for _ in range(RATE_LIMIT_MAX_REQUESTS):
            db.add(ChatMessage(profile_id=profile.id, role="user", content="测试消息"))
        db.commit()

        try:
            check_rate_limit(db, profile.id)
            raise AssertionError("应该抛出 RateLimitExceededError，但没有抛")
        except RateLimitExceededError as exc:
            print(f"通过。正确抛出限流异常：{exc}")
    finally:
        db.close()


def scenario_different_profile_not_affected():
    print("\n=== 场景3：限流按 profile_id 隔离，别的 profile 不受影响 ===")
    db = SessionLocal()
    try:
        busy_profile = make_profile(db)
        for _ in range(RATE_LIMIT_MAX_REQUESTS):
            db.add(ChatMessage(profile_id=busy_profile.id, role="user", content="测试消息"))
        db.commit()

        fresh_profile = make_profile(db)
        check_rate_limit(db, fresh_profile.id)  # 不应该被 busy_profile 的记录影响
        print(f"通过。busy_profile_id={busy_profile.id} 已被限流，"
              f"fresh_profile_id={fresh_profile.id} 仍然正常通过。")
    finally:
        db.close()


def scenario_assistant_messages_not_counted():
    print("\n=== 场景4：只统计 role='user' 的消息，role='assistant' 的回复不该占限流额度 ===")
    db = SessionLocal()
    try:
        profile = make_profile(db)
        for _ in range(RATE_LIMIT_MAX_REQUESTS):
            db.add(ChatMessage(profile_id=profile.id, role="assistant", content="这是助手的回复"))
        db.commit()

        check_rate_limit(db, profile.id)  # 全是 assistant 消息，不应该触发限流
        print(f"通过。{RATE_LIMIT_MAX_REQUESTS} 条 assistant 消息不计入限流。")
    finally:
        db.close()


def main():
    scenario_under_limit_passes()
    scenario_at_limit_blocks()
    scenario_different_profile_not_affected()
    scenario_assistant_messages_not_counted()
    print("\n全部4个场景通过。")


if __name__ == "__main__":
    main()
