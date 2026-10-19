"""
完整集成测试：真实 Gemini function calling + 真实 ReAct 循环 + 真实数据库。
只有在 test_gemini_function_calling_minimal.py 和
test_gemini_function_calling_roundtrip.py 都跑通之后才运行这个——
这两个已经验证过真实连通性，现在可以放心接入完整链路了。

四个场景刻意覆盖了不同的行为期望，跑完按最后打印的检查清单逐条对照：
- 场景1：基础检索，应该调用 search_candidates
- 场景2：追问已查过候选的细节，应该调用 get_candidate_detail 而不是重新查列表
- 场景3：要求对比，应该调用 compare_candidates
- 场景4：数据库里没有的专业方向（医学），应该如实说查不到，不能编——
         这个场景直接对应之前发现的"问医学出来计算机"那个真实bug，
         现在用系统提示词的第4条规则约束住，这里专门验证有没有生效

用法（backend/ 目录下，激活 venv，.env 里配好真实 GEMINI_API_KEY，
数据库已经跑通种子数据导入）：
    python scripts/test_retrieval_agent_real.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio

from app.db import SessionLocal
from app.schemas.candidate import ProfileIn
from app.services.agent_prompts import RETRIEVAL_SYSTEM_PROMPT
from app.services.gemini_tool_caller import call_gemini_with_tools
from app.services.react_loop import run_react_loop
from app.services.tools import TOOL_SCHEMAS, ToolContext


async def run_case(label: str, message: str, profile: ProfileIn):
    print(f"\n{'=' * 60}\n场景: {label}\n用户消息: {message}\n{'=' * 60}")
    db = SessionLocal()
    ctx = ToolContext(db=db, profile=profile)

    text, trace = await run_react_loop(
        agent_name="retrieval",
        system_prompt=RETRIEVAL_SYSTEM_PROMPT,
        initial_message=message,
        ctx=ctx,
        model_caller=call_gemini_with_tools,
        tool_schemas=TOOL_SCHEMAS,
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
    profile = ProfileIn(
        province="江苏", track="物理类", subjects=["物理", "化学"],
        score=637, rank=5200, major_keyword="计算机",
    )

    await run_case(
        "基础检索",
        "帮我查一下有哪些计算机相关的候选，重点介绍最推荐的一个",
        profile,
    )

    await run_case(
        "追问细节（期望调用get_candidate_detail而不是重新search）",
        "东南大学那个专业组具体是什么情况，历年分数怎么样",
        profile,
    )

    await run_case(
        "要求对比（期望调用compare_candidates）",
        "帮我对比一下东南大学和南京邮电大学这两个",
        profile,
    )

    await run_case(
        "数据库里没有的专业方向（期望如实告知查不到，不能编）",
        "帮我查一下临床医学相关的专业",
        profile,
    )

    print(f"\n{'=' * 60}")
    print("全部场景跑完，请逐条检查：")
    print("1. 每个场景是否都真的调用了工具（trace里能看到tool_call，不是模型自己编）")
    print("2. 场景2是否复用了已知group_id，用get_candidate_detail而不是重新search_candidates")
    print("3. 场景3的最终回复里是否同时提到了两所学校的对比信息")
    print("4. 场景4（数据库里没有医学数据）模型有没有如实说'没查到'，"
          "而不是编造一个不存在的医学院校列表——这是之前发现的真实bug对应的验证点")
    print("5. 观察每个场景的步数和token消耗是否在合理范围内（不应该出现max_steps耗尽）")


if __name__ == "__main__":
    asyncio.run(main())
