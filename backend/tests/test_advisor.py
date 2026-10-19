"""
Advisor Agent 的关键行为：
1. 工具集确实被限制成只有 get_candidate_detail / compare_candidates / load_skill
   （不该出现 search_candidates / web_search，那是 Retrieval Agent 的职责；
   load_skill 是纯知识加载，Advisor 保留）
2. city_pref 排序在工具返回的数据里确实生效了（rules_engine.py 那次修复的验证点）
3. Orchestrator 的 _build_plan 拿到"要对比/取舍"的路由判断结果时，确实
   路由到 advisor 分支，而不是继续走 retrieval——这一步的路由判断现在是
   app.services.intent_router.classify_intent() 做的真实 Gemini 语义判断
   （见该模块顶部说明：关键词匹配覆盖不全），这里用 monkeypatch 换掉它，
   不测"语义判断准不准"（那是 intent_router 自己的职责），只测"_build_plan
   拿到 compare 判断结果之后确实路由对了"。

不发真实网络请求、不需要真实 Gemini Key，用假模型 + 真实数据库。
从 scripts/test_advisor_scripted.py 迁移，断言逻辑原样保留。
"""
from app.models import Profile
from app.services import orchestrator
from app.services.orchestrator import ADVISOR_TOOL_SCHEMAS, _build_plan
from app.services.react_loop import ModelResponse, run_react_loop
from app.services.tools import ToolContext, _tool_search_candidates

from .conftest import make_profile_in


def scripted_model(responses: list[ModelResponse]):
    call_count = 0

    async def _caller(system_prompt, contents, tool_schemas):
        nonlocal call_count
        if call_count >= len(responses):
            raise AssertionError(
                f"脚本已经用完（预设了{len(responses)}次响应），但循环还在继续调用模型，"
                f"说明状态机没有在预期步骤停下来。"
            )
        r = responses[call_count]
        call_count += 1
        return r

    return _caller


def make_ctx(db, city_pref: str | None = None) -> ToolContext:
    return ToolContext(db=db, profile=make_profile_in(city_pref=city_pref))


def test_tool_schemas_are_restricted():
    names = {s["name"] for s in ADVISOR_TOOL_SCHEMAS}
    assert names == {
        "get_candidate_detail", "compare_candidates", "load_skill", "search_policy", "finalize_recommendation",
    }, (
        f"Advisor 工具集不对，实际是 {names}——search_candidates/web_search "
        f"不该出现在这里，那是 Retrieval Agent 的职责；load_skill/search_policy 是纯知识/"
        f"政策原文查询，跟检索/联网职责边界无关，Advisor 给取舍建议时也可能用到，应该保留；"
        f"finalize_recommendation 是底线校验网关的入口，Advisor 给出最终推荐前必须能调用"
    )


def _fetch_two_real_group_ids(db) -> list[str]:
    """从数据库里真实查两个存在的 group_id，不靠猜。"""
    ctx = make_ctx(db)
    result = _tool_search_candidates({}, ctx)
    assert result.ok and result.data["count"] >= 2, "种子数据里至少要有两条候选才能测对比场景"
    return [c["group_id"] for c in result.data["candidates"][:2]]


async def test_compare_flow_completes(db):
    """用户要求对比 -> 调用 compare_candidates -> 给出最终建议。"""
    group_ids = _fetch_two_real_group_ids(db)
    ctx = make_ctx(db)
    model = scripted_model([
        ModelResponse(is_final_answer=False, tool_name="compare_candidates",
                      tool_args={"group_ids": group_ids}, tokens_used=50),
        ModelResponse(is_final_answer=True, text="综合对比，给出推荐结论。", tokens_used=60),
    ])
    text, trace = await run_react_loop(
        agent_name="advisor", system_prompt="test", initial_message="帮我对比一下这两个",
        ctx=ctx, model_caller=model, tool_schemas=ADVISOR_TOOL_SCHEMAS,
    )
    assert trace.status == "completed", f"应该正常完成，实际状态: {trace.status}"
    assert trace.total_steps == 2
    assert trace.steps[0].tool_name == "compare_candidates"
    assert trace.steps[0].status == "ok", (
        f"compare_candidates 这一步应该成功，实际 tool_result={trace.steps[0].tool_result}"
    )
    assert trace.steps[0].tool_result["data"]["comparison"], "compare_candidates 应该真的返回了对比数据"
    assert len(trace.steps[0].tool_result["data"]["comparison"]) == 2


def test_city_pref_affects_tool_data(db):
    """city_pref 排序验证——设置城市偏好后，search_candidates 的结果里符合偏好的应该排前面。"""
    ctx_no_pref = make_ctx(db, city_pref=None)
    result_no_pref = _tool_search_candidates({}, ctx_no_pref)
    assert result_no_pref.ok and result_no_pref.data["count"] > 0, "至少要有候选数据才能验证排序"

    # 拿没有城市偏好时排第一的候选的城市，用它作为这次的偏好城市，
    # 这样不管种子数据具体内容是什么，都能验证"设置偏好后该城市候选排到最前"。
    first_city_when_no_pref = result_no_pref.data["candidates"][0]["city"]
    # 找一个不是默认第一名的城市作为偏好，这样排序变化才有意义可验证
    other_city = next(
        (c["city"] for c in result_no_pref.data["candidates"] if c["city"] != first_city_when_no_pref),
        None,
    )
    assert other_city is not None, "种子数据里至少要有两个不同城市的候选才能验证排序变化"

    ctx_with_pref = make_ctx(db, city_pref=other_city)
    result_with_pref = _tool_search_candidates({}, ctx_with_pref)
    assert result_with_pref.data["candidates"][0]["city"] == other_city, (
        f"设置了 city_pref={other_city!r} 后，排第一的候选城市应该是 {other_city}，"
        f"实际是 {result_with_pref.data['candidates'][0]['city']}——city_pref 排序没有生效"
    )


async def test_orchestrator_routes_to_advisor(db, monkeypatch):
    """Orchestrator 的 _build_plan 拿到 classify_intent 返回的'要对比'判断
    结果时，应该路由到 advisor 分支；返回'要检索'时走 retrieval。
    intent_router.classify_intent 被 monkeypatch 成假实现，不发真实请求。
    """
    from app.services.intent_router import INTENT_COMPARE, INTENT_SEARCH

    complete_profile = Profile(
        province="江苏", track="物理类", subjects=["物理", "化学"],
        score=637, rank=5200, major_keyword="计算机",
    )
    db.add(complete_profile)
    db.commit()
    db.refresh(complete_profile)

    async def _fake_compare(history, user_message):
        return INTENT_COMPARE
    monkeypatch.setattr(orchestrator, "classify_intent", _fake_compare)
    plan = await _build_plan(complete_profile, "这两个学校我该选哪个啊")
    assert len(plan) == 1
    assert plan[0].agent == "advisor", f"应该路由到 advisor，实际是 {plan[0].agent}"

    async def _fake_search(history, user_message):
        return INTENT_SEARCH
    monkeypatch.setattr(orchestrator, "classify_intent", _fake_search)
    plan2 = await _build_plan(complete_profile, "帮我查一下计算机相关的候选")
    assert plan2[0].agent == "retrieval", f"没有对比意图，应该还走 retrieval，实际是 {plan2[0].agent}"
