"""
Advisor Agent 的脚本化测试——跟 test_react_loop_scripted.py 同一套思路：
用一个按预设顺序返回响应的"假模型"，不发真实网络请求、不需要真实 Gemini
Key，验证 Advisor Agent 这一层的关键行为：
1. 工具集确实被限制成只有 get_candidate_detail / compare_candidates
   （不该出现 search_candidates / web_search，那是 Retrieval Agent 的职责）
2. city_pref 排序在工具返回的数据里确实生效了（rules_engine.py 那次修复
   的验证点，不是空转）
3. Orchestrator 的 _build_plan 遇到"对比/取舍"类关键词时，确实路由到
   advisor 分支，而不是继续走 retrieval

用法（backend/ 目录下，激活 venv，数据库已经跑通迁移+种子数据）：
    python scripts/test_advisor_scripted.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio

from app.db import SessionLocal
from app.models import Profile
from app.schemas.candidate import ProfileIn
from app.services.orchestrator import ADVISOR_TOOL_SCHEMAS, _build_plan
from app.services.react_loop import ModelResponse, run_react_loop
from app.services.tools import ToolContext


def scripted_model(responses: list[ModelResponse]):
    """跟 test_react_loop_scripted.py 里的同名函数逻辑一致：按顺序吐出预设响应，
    用完还被调用就说明状态机没有在预期步骤停下来，直接让测试失败。
    """
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


def make_ctx(city_pref: str | None = None) -> ToolContext:
    db = SessionLocal()
    profile = ProfileIn(
        province="江苏", track="物理类", subjects=["物理", "化学"],
        score=637, rank=5200, major_keyword="计算机", city_pref=city_pref,
    )
    return ToolContext(db=db, profile=profile)


async def scenario_tool_schemas_are_restricted():
    print("=== 场景1：Advisor 的工具集只应该有 get_candidate_detail / compare_candidates ===")
    names = {s["name"] for s in ADVISOR_TOOL_SCHEMAS}
    assert names == {"get_candidate_detail", "compare_candidates"}, (
        f"Advisor 工具集不对，实际是 {names}——search_candidates/web_search "
        f"不该出现在这里，那是 Retrieval Agent 的职责"
    )
    print(f"通过。工具集 = {names}")


def _fetch_two_real_group_ids() -> list[str]:
    """从数据库里真实查两个存在的 group_id，不再靠猜——第一次写这个脚本时
    随手编了两个 group_id（"seu-cs-04"/"nuaa-cs-06"），后面那个种子数据里
    根本不存在，导致 compare_candidates 真实执行时返回了 ok=False，
    暴露了这个脚本本身的问题（不是 city_pref/Advisor 逻辑的问题）。
    """
    from app.services.tools import _tool_search_candidates

    ctx = make_ctx()
    result = _tool_search_candidates({}, ctx)
    ctx.db.close()
    assert result.ok and result.data["count"] >= 2, "种子数据里至少要有两条候选才能测对比场景"
    return [c["group_id"] for c in result.data["candidates"][:2]]


async def scenario_compare_flow_completes():
    print("\n=== 场景2：用户要求对比 -> 调用 compare_candidates -> 给出最终建议 ===")
    group_ids = _fetch_two_real_group_ids()
    ctx = make_ctx()
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
    print(f"通过。用真实group_id={group_ids}对比，text={text!r}, steps={trace.total_steps}")
    ctx.db.close()


async def scenario_city_pref_affects_tool_data():
    print("\n=== 场景3：city_pref 排序验证——设置城市偏好后，search_candidates 的结果里符合偏好的应该排前面 ===")
    # 这里直接调用工具层（不经过模型），验证的是 rules_engine.py 那次修复本身，
    # 跟 Advisor Agent 的 ReAct 循环无关，但因为是同一次修复的两个验证点，放在
    # 同一个脚本里一起跑，方便一次性确认"排序权重"这条链路真的通了。
    from app.services.tools import _tool_search_candidates

    ctx_no_pref = make_ctx(city_pref=None)
    result_no_pref = _tool_search_candidates({}, ctx_no_pref)
    ctx_no_pref.db.close()
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

    ctx_with_pref = make_ctx(city_pref=other_city)
    result_with_pref = _tool_search_candidates({}, ctx_with_pref)
    ctx_with_pref.db.close()
    assert result_with_pref.data["candidates"][0]["city"] == other_city, (
        f"设置了 city_pref={other_city!r} 后，排第一的候选城市应该是 {other_city}，"
        f"实际是 {result_with_pref.data['candidates'][0]['city']}——city_pref 排序没有生效"
    )
    print(f"通过。无偏好时第一名城市={first_city_when_no_pref!r}，"
          f"设置偏好为{other_city!r}后第一名城市={result_with_pref.data['candidates'][0]['city']!r}")


async def scenario_orchestrator_routes_to_advisor():
    print("\n=== 场景4：Orchestrator 的 _build_plan 遇到'对比'类关键词，应该路由到 advisor 分支 ===")
    db = SessionLocal()
    complete_profile = Profile(
        province="江苏", track="物理类", subjects=["物理", "化学"],
        score=637, rank=5200, major_keyword="计算机",
    )
    db.add(complete_profile)
    db.commit()
    db.refresh(complete_profile)

    plan = _build_plan(complete_profile, "这两个学校我该选哪个啊")
    assert len(plan) == 1
    assert plan[0].agent == "advisor", f"应该路由到 advisor，实际是 {plan[0].agent}"
    print(f"通过。plan[0].agent = {plan[0].agent!r}, goal = {plan[0].goal!r}")

    plan2 = _build_plan(complete_profile, "帮我查一下计算机相关的候选")
    assert plan2[0].agent == "retrieval", f"没有对比类关键词，应该还走 retrieval，实际是 {plan2[0].agent}"
    print(f"通过（对照组）。没有对比关键词时 plan[0].agent = {plan2[0].agent!r}")

    db.close()


async def main():
    await scenario_tool_schemas_are_restricted()
    await scenario_compare_flow_completes()
    await scenario_city_pref_affects_tool_data()
    await scenario_orchestrator_routes_to_advisor()
    print("\n全部4个场景通过。")


if __name__ == "__main__":
    asyncio.run(main())
