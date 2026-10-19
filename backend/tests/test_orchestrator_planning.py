"""
Orchestrator 规划逻辑：_build_plan() 根据 profile 当前状态 + 用户这句话
决定这一轮走哪个 Agent。

"必填字段收集齐了没有"这一层判断（缺字段就必须走 profile）是纯函数、
不碰网络——这部分测试完全不需要 Gemini，跟 API 额度无关，字段缺失时
_build_plan 会在调用 classify_intent 之前就直接返回，用不到下面的 mock。

"字段齐了之后，这句话该走 retrieval 还是 advisor"这一层，现在改成调用
app.services.intent_router.classify_intent() 做语义判断（见该模块顶部的
设计说明：关键词匹配覆盖不全，"这两个我该怎么选"这种没有命中任何预设
关键词的对比类问题会被误判成检索）。这部分测试用 monkeypatch 换成假的
classify_intent，不发真实请求——这个文件的定位一直是"确定性/可控输入下
的分支路由测试"，不是"真实 Gemini 语义判断准不准"（那部分交给
scripts/test_advisor_real.py 这类需要真实 Key 的联调脚本去覆盖）。

从 scripts/test_orchestrator_plan.py 迁移，断言逻辑原样保留。
"""
from app.models import Profile
from app.services import orchestrator
from app.services.gemini_errors import GeminiCallError
from app.services.intent_router import INTENT_COMPARE, INTENT_SEARCH


def _mock_classify_intent(monkeypatch, intent: str):
    """把 orchestrator 模块里引用的 classify_intent 换成一个不发网络请求的假实现。
    monkeypatch 的对象必须是 orchestrator.classify_intent（导入到这个模块
    命名空间里的那个引用），而不是 intent_router.classify_intent 本身——
    orchestrator.py 是用 `from ... import classify_intent` 导入的，patch
    源模块对已经导入到 orchestrator 命名空间里的这个名字不生效。
    """
    async def _fake(history, user_message):
        return intent
    monkeypatch.setattr(orchestrator, "classify_intent", _fake)


async def test_empty_profile_routes_to_profile_agent(db):
    empty_profile = Profile()
    db.add(empty_profile)
    db.commit()
    db.refresh(empty_profile)
    plan = await orchestrator._build_plan(empty_profile)
    assert len(plan) == 1
    assert plan[0].agent == "profile"
    assert "省份" in plan[0].goal


async def test_missing_one_field_still_routes_to_profile_agent(db):
    almost_complete = Profile(
        province="江苏", track="物理类", subjects=["物理", "化学"],
        score=637, rank=5200, major_keyword=None,
    )
    db.add(almost_complete)
    db.commit()
    db.refresh(almost_complete)
    plan = await orchestrator._build_plan(almost_complete)
    assert len(plan) == 1
    assert plan[0].agent == "profile"
    assert "专业关键词" in plan[0].goal
    assert "省份" not in plan[0].goal, "已经有省份了，不该被列进缺失项"


async def test_complete_profile_routes_to_retrieval_agent(db, monkeypatch):
    _mock_classify_intent(monkeypatch, INTENT_SEARCH)
    complete_profile = Profile(
        province="江苏", track="物理类", subjects=["物理", "化学"],
        score=637, rank=5200, major_keyword="计算机",
    )
    db.add(complete_profile)
    db.commit()
    db.refresh(complete_profile)
    plan = await orchestrator._build_plan(complete_profile)
    assert len(plan) == 1
    assert plan[0].agent == "retrieval"


async def test_score_without_rank_still_routes_to_retrieval_agent(db, monkeypatch):
    """只有分数没有位次（分数或位次算满足），应该走 retrieval 分支。"""
    _mock_classify_intent(monkeypatch, INTENT_SEARCH)
    score_only_profile = Profile(
        province="江苏", track="物理类", subjects=["物理", "化学"],
        score=637, rank=None, major_keyword="计算机",
    )
    db.add(score_only_profile)
    db.commit()
    db.refresh(score_only_profile)
    plan = await orchestrator._build_plan(score_only_profile)
    assert len(plan) == 1
    assert plan[0].agent == "retrieval", "只有分数没有位次，'分数或位次'这条应该算满足"


async def test_intent_router_says_compare_routes_to_advisor(db, monkeypatch):
    _mock_classify_intent(monkeypatch, INTENT_COMPARE)
    complete_profile = Profile(
        province="江苏", track="物理类", subjects=["物理", "化学"],
        score=637, rank=5200, major_keyword="计算机",
    )
    db.add(complete_profile)
    db.commit()
    db.refresh(complete_profile)
    plan = await orchestrator._build_plan(complete_profile, "这两个学校我该选哪个啊")
    assert plan[0].agent == "advisor"


async def test_classify_intent_failure_falls_back_to_keyword_matching(db, monkeypatch):
    """路由判断这次 Gemini 调用失败（网络/配额/没配Key）时，不能让整轮
    对话失败——应该退回原来的关键词匹配兜底，这是真实会发生的场景
    （免费层配额限流之前就撞到过好几次），不是假设的边缘情况。
    """
    async def _boom(history, user_message):
        raise GeminiCallError("模拟：路由判断调用失败")
    monkeypatch.setattr(orchestrator, "classify_intent", _boom)

    complete_profile = Profile(
        province="江苏", track="物理类", subjects=["物理", "化学"],
        score=637, rank=5200, major_keyword="计算机",
    )
    db.add(complete_profile)
    db.commit()
    db.refresh(complete_profile)

    plan = await orchestrator._build_plan(complete_profile, "这两个学校我该选哪个啊")
    assert plan[0].agent == "advisor", "路由判断调用失败时，命中关键词兜底应该还是能走到 advisor"

    plan2 = await orchestrator._build_plan(complete_profile, "帮我查一下计算机相关的候选")
    assert plan2[0].agent == "retrieval", "路由判断调用失败、也没命中关键词时，兜底应该走 retrieval"
