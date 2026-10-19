"""
Advisor Agent 的完整集成测试：真实 Gemini function calling + 真实 ReAct 循环 +
真实数据库，跟 test_retrieval_agent_real.py 是同一套结构，只是换成 Advisor
的系统提示词和工具集。只有在 scripts/test_advisor_scripted.py 全部通过之后
才运行这个——脚本化测试已经验证过状态机和工具集限制是对的，这里只需要
确认真实模型在这套约束下的实际表现。

三个场景覆盖的行为期望：
- 场景1：明确要求对比 -> 应该调用 compare_candidates，回复里同时提到两所学校
- 场景2：设置了 city_pref -> 回复里应该明确提到"符合你的城市偏好"这类表述，
         不能只是排序变了但用户看不出为什么（对应 agent_prompts.py 里
         ADVISOR_SYSTEM_PROMPT 第2条规则的验证点）
- 场景3：追问某个候选的细节 -> 应该调用 get_candidate_detail，不是凭空编

用法（backend/ 目录下，激活 venv，.env 里配好真实 GEMINI_API_KEY，
数据库已经跑通种子数据导入）：
    python scripts/test_advisor_real.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio

from app.db import SessionLocal
from app.schemas.candidate import ProfileIn
from app.services.agent_prompts import ADVISOR_SYSTEM_PROMPT
from app.services.gemini_tool_caller import call_gemini_with_tools
from app.services.orchestrator import ADVISOR_TOOL_SCHEMAS
from app.services.react_loop import run_react_loop
from app.services.tools import ToolContext


async def run_case(label: str, message: str, profile: ProfileIn):
    print(f"\n{'=' * 60}\n场景: {label}\n用户消息: {message}\nprofile.city_pref = {profile.city_pref!r}\n{'=' * 60}")
    db = SessionLocal()
    ctx = ToolContext(db=db, profile=profile)

    text, trace = await run_react_loop(
        agent_name="advisor",
        system_prompt=ADVISOR_SYSTEM_PROMPT,
        initial_message=message,
        ctx=ctx,
        model_caller=call_gemini_with_tools,
        tool_schemas=ADVISOR_TOOL_SCHEMAS,
    )

    print(f"\n--- 最终回复 ---\n{text}")
    print("\n--- 执行轨迹 ---")
    status_line = f"状态: {trace.status}"
    if trace.abort_reason:
        status_line += f"（原因: {trace.abort_reason}）"
    print(status_line)
    print(f"步数: {trace.total_steps}, 总token: {trace.total_tokens}")
    for i, step in enumerate(trace.steps):
        detail = f"tool={step.tool_name}" if step.tool_name else ""
        print(f"  第{i}步: {step.action_type} {detail} status={step.status}")

    db.close()
    return text, trace


async def main():
    profile_no_pref = ProfileIn(
        province="江苏", track="物理类", subjects=["物理", "化学"],
        score=637, rank=5200, major_keyword="计算机",
    )

    await run_case(
        "明确要求对比（期望调用compare_candidates）",
        "帮我对比一下东南大学和南京邮电大学这两个，我该选哪个",
        profile_no_pref,
    )

    # city_pref 用南京之外的城市，确保能看出排序/理由是不是真的因为偏好起了作用，
    # 而不是碰巧本来就排第一——具体换成种子数据里实际存在的某个非默认第一名城市。
    profile_with_pref = ProfileIn(
        province="江苏", track="物理类", subjects=["物理", "化学"],
        score=637, rank=5200, major_keyword="计算机", city_pref="苏州",
    )
    await run_case(
        "设置了城市偏好，笼统问该怎么选（期望理由里明确提到城市偏好）",
        "这几个候选我该怎么选比较好",
        profile_with_pref,
    )

    await run_case(
        "追问细节（期望调用get_candidate_detail）",
        "东南大学那个专业组具体是什么情况，学费多少",
        profile_no_pref,
    )

    print(f"\n{'=' * 60}")
    print("全部场景跑完，请逐条检查：")
    print("1. 场景1是否真的调用了 compare_candidates，最终回复里是否同时提到两所学校")
    print("2. 场景2的最终回复里是否明确出现'符合你的城市偏好'这类表述，"
          "而不是只是候选顺序变了但没说明原因——这是 city_pref 缺口修复的验证点")
    print("3. 场景3是否调用了 get_candidate_detail，不是凭自己的知识编造学费数字")
    print("4. 三个场景的 trace 里，工具调用是否都只出现在 get_candidate_detail/"
          "compare_candidates 里，没有出现 search_candidates（Advisor 不该重新检索）")
    print("5. 观察每个场景的步数和token消耗是否在合理范围内（不应该出现max_steps耗尽）")


if __name__ == "__main__":
    asyncio.run(main())
