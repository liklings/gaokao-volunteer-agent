"""
_refresh_optional_profile_fields 的回归测试。

真实撞到的 bug：必填字段（省份/类别/选科/分数或位次/专业关键词）一旦
在更早的对话里收集齐了，_build_plan 就再也不会选中 profile 分支——
用户后面才补充的"学费上限一万""优先考虑省内城市"这类可选字段
（budget_max/city_pref/home_province_only）因此永远不会被写进 Profile
行，右侧"当前条件"面板一直显示"待补充"，直接查询候选用的还是没有
预算限制的旧条件。这里锁住修复：必填字段已经齐了、run_orchestrator
要走 retrieval/advisor 分支之前，必须先补跑一次静默的字段刷新。

不发真实网络请求，call_profile_agent 用 monkeypatch 换掉。
"""
from app.models import Profile
from app.services import orchestrator


def _mock_call_profile_agent(monkeypatch, profile_update: dict, reply: str = "好的"):
    async def _fake(profile_json, history, user_message):
        return {"reply": reply, "profile": profile_update, "conflicts": []}
    monkeypatch.setattr(orchestrator, "call_profile_agent", _fake)


async def test_refresh_optional_profile_fields_merges_budget_max(db, monkeypatch):
    p = Profile(
        province="江苏", track="物理类", subjects=["物理", "化学"],
        score=637, rank=5200, major_keyword="计算机",
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    assert p.budget_max is None

    _mock_call_profile_agent(monkeypatch, {"budget_max": 10000, "city_pref": None})
    await orchestrator._refresh_optional_profile_fields(db, p, "学费上限10000元/年，优先考虑省内城市")
    assert p.budget_max == 10000


async def test_refresh_optional_profile_fields_does_not_persist_extra_chat_message(db, monkeypatch):
    """静默刷新不应该往聊天记录里多插一条 assistant 消息——用户这一轮真正
    想看到的是 retrieval/advisor 给出的实质性内容，不需要中间插一句机械确认。
    """
    from app.models import ChatMessage

    p = Profile(
        province="江苏", track="物理类", subjects=["物理", "化学"],
        score=637, rank=5200, major_keyword="计算机",
    )
    db.add(p)
    db.commit()
    db.refresh(p)

    _mock_call_profile_agent(monkeypatch, {"budget_max": 10000})
    await orchestrator._refresh_optional_profile_fields(db, p, "学费上限10000元/年")

    messages = db.query(ChatMessage).filter(ChatMessage.profile_id == p.id).all()
    assert messages == [], "静默刷新不应该往 chat_messages 表写任何记录"


async def test_refresh_optional_profile_fields_swallows_gemini_failure(db, monkeypatch):
    """刷新调用失败（网络/配额）不能让整轮请求失败——这是"锦上添花"的
    准确性提升，不是硬依赖，跟 intent_router 的失败兜底是同一种取舍。
    """
    from app.services.gemini_errors import GeminiCallError

    async def _boom(profile_json, history, user_message):
        raise GeminiCallError("模拟失败")
    monkeypatch.setattr(orchestrator, "call_profile_agent", _boom)

    p = Profile(
        province="江苏", track="物理类", subjects=["物理", "化学"],
        score=637, rank=5200, major_keyword="计算机", budget_max=None,
    )
    db.add(p)
    db.commit()
    db.refresh(p)

    await orchestrator._refresh_optional_profile_fields(db, p, "学费上限10000元/年")
    assert p.budget_max is None  # 没有崩，也没有误写入任何值
