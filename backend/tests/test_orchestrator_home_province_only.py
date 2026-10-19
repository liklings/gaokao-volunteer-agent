"""
home_province_only 这个开关接进 Profile Agent 聊天采集流程之后的测试——
之前它只存在于前端"查询候选"按钮的单次请求参数里，聊天里用户说"只想看
省内的学校"是完全没用的，因为 Profile（数据库行）和 ProfileIn（喂给
rules_engine 的对象）都不认识这个字段。这里测两件事：
1. Profile（数据库行，新增的持久化列）能正确转换成 ProfileIn，供
   tools.py 的 search_candidates 工具使用。
2. Profile Agent 返回的 profile 更新（PROFILE_FIELDS 合并逻辑）能正确
   写入这个字段，包括"显式 false 要生效"和"没提到就不动"两种情况——
   这两种情况在 _merge_profile_update() 的 update[f] is not None 判断
   里很容易搞混（history 里其他字段都不需要区分"false"和"没提到"，
   这个字段是第一个有这个区分需求的布尔字段）。
"""
from app.models import Profile
from app.services.orchestrator import PROFILE_FIELDS, _merge_profile_update, _profile_to_profile_in


def test_profile_to_profile_in_carries_home_province_only(db):
    p = Profile(province="江苏", track="物理类", subjects=["物理", "化学"], home_province_only=True)
    db.add(p)
    db.commit()
    db.refresh(p)
    profile_in = _profile_to_profile_in(p)
    assert profile_in.home_province_only is True


def test_new_profile_defaults_home_province_only_to_false(db):
    # 用户从没提过这件事——数据库默认值应该是 False（不过滤），不是 None
    # 或者报错，这样 rules_engine.evaluate_group() 里的 getattr(...) 兜底
    # 才有意义（真实撞到过：加这个字段最开始就是因为 getattr 兜底炸过一次
    # AttributeError，这里补一条测试锁住"默认值确实是 False"这个前提）。
    p = Profile(province="江苏")
    db.add(p)
    db.commit()
    db.refresh(p)
    assert p.home_province_only is False


def test_merge_profile_update_sets_explicit_true():
    p = Profile(province="江苏", home_province_only=False)
    _merge_profile_update(p, {"home_province_only": True})
    assert p.home_province_only is True


def test_merge_profile_update_ignores_missing_key_field():
    # Profile Agent 这一轮的回复里，如果用户这句话完全没提"要不要只看省内"，
    # 模型应该填 null，_merge_profile_update 要跳过它、不能悄悄把已经设成
    # True 的值覆盖回默认值。
    p = Profile(province="江苏", home_province_only=True)
    _merge_profile_update(p, {"province": "浙江", "home_province_only": None})
    assert p.home_province_only is True
    assert p.province == "浙江"


def test_home_province_only_is_in_profile_fields_list():
    assert "home_province_only" in PROFILE_FIELDS
