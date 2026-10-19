"""
app/services/limits.py 的 check_rate_limit()：同一个 profile_id 在时间窗口内
发起过多请求应该被拦下。不碰 Gemini，只操作数据库——直接往 ChatMessage 表
插入若干条 role='user' 的记录模拟"这个 profile 最近发起过几次请求"，然后
检查 check_rate_limit() 的判断对不对。

从 scripts/test_rate_limit_scripted.py 迁移，断言逻辑原样保留。
"""
import pytest

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


def test_under_limit_passes(db):
    """请求次数低于上限，应该正常通过。"""
    profile = make_profile(db)
    for _ in range(RATE_LIMIT_MAX_REQUESTS - 1):
        db.add(ChatMessage(profile_id=profile.id, role="user", content="测试消息"))
    db.commit()

    check_rate_limit(db, profile.id)  # 不应该抛异常


def test_at_limit_blocks(db):
    """请求次数达到上限，应该被拦下。"""
    profile = make_profile(db)
    for _ in range(RATE_LIMIT_MAX_REQUESTS):
        db.add(ChatMessage(profile_id=profile.id, role="user", content="测试消息"))
    db.commit()

    with pytest.raises(RateLimitExceededError):
        check_rate_limit(db, profile.id)


def test_different_profile_not_affected(db):
    """限流按 profile_id 隔离，别的 profile 不受影响。"""
    busy_profile = make_profile(db)
    for _ in range(RATE_LIMIT_MAX_REQUESTS):
        db.add(ChatMessage(profile_id=busy_profile.id, role="user", content="测试消息"))
    db.commit()

    fresh_profile = make_profile(db)
    check_rate_limit(db, fresh_profile.id)  # 不应该被 busy_profile 的记录影响


def test_assistant_messages_not_counted(db):
    """只统计 role='user' 的消息，role='assistant' 的回复不该占限流额度。"""
    profile = make_profile(db)
    for _ in range(RATE_LIMIT_MAX_REQUESTS):
        db.add(ChatMessage(profile_id=profile.id, role="assistant", content="这是助手的回复"))
    db.commit()

    check_rate_limit(db, profile.id)  # 全是 assistant 消息，不应该触发限流
