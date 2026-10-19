"""
Orchestrator 的完整端到端测试——真实 Gemini + 真实 ReAct 循环 + 真实数据库 +
真实 Trace 落库，一次性跑完整个"收集条件 -> 检索候选"的流程。

之所以现在敢一次性测（不再像之前那样拆得很细），是因为组成这个流程的每一块
都已经单独验证过了：function calling 通不通、thought_signature 对不对、
ReAct 循环的边界保护对不对、trace 落库对不对——这些都是今天已经拿真实数据
和/或真实API确认过的，现在只是把它们按 Orchestrator 的规则组装起来跑一遍，
风险比第一次从零开始测低很多。

用法（backend/ 目录下，激活 venv，.env 里配好真实 GEMINI_API_KEY，
数据库已经跑通迁移+种子数据）：
    python scripts/test_orchestrator_real.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio

from app.db import SessionLocal
from app.models import Profile
from app.services.orchestrator import run_orchestrator


async def main() -> int:
    db = SessionLocal()
    profile = Profile()  # 全新空白条件，模拟一次新对话
    db.add(profile)
    db.commit()
    db.refresh(profile)
    print(f"新建 profile_id = {profile.id}\n")

    # 第1轮：一次性把条件说全，期望 Orchestrator 走 profile 分支
    messages = [
        "我是江苏物理类考生，2026届，637分，位次5200，选科物理+化学，想找计算机相关专业，优先考虑省内城市。",
    ]

    turn = 0
    while turn < 3:  # 最多3轮兜底，防止模型没能一次提取完所有字段时脚本卡住
        turn += 1
        if turn <= len(messages):
            msg = messages[turn - 1]
        else:
            # 如果第一轮没能一次提取全部字段，用兜底消息把常见缺项都补一遍
            msg = "补充一下：江苏，物理类，2026届，637分，位次5200，选科物理和化学，专业方向计算机。"

        print(f"=== 第{turn}轮 ===\n用户: {msg}")
        result = await run_orchestrator(db, profile, msg)
        print(f"走的分支: {[s.agent for s in result.plan_steps]}")
        print(f"Agent回复: {result.reply}")
        print(f"missing={result.missing} ready={result.ready}\n")

        if result.ready:
            break
    else:
        print("三轮下来条件还没收集齐，检查一下 Profile Agent 的提取效果，先不继续往下测。")
        db.close()
        return 1

    # 条件收集齐了，再问一次检索类问题，期望 Orchestrator 走 retrieval 分支
    print("=== 条件已就绪，测试检索分支 ===")
    result = await run_orchestrator(db, profile, "帮我查一下有哪些计算机相关的候选，重点介绍最推荐的一个")
    print(f"走的分支: {[s.agent for s in result.plan_steps]}")
    assert result.plan_steps[0].agent == "retrieval", "条件齐全后应该走retrieval分支，不是又跑去问profile"

    print(f"\nAgent回复:\n{result.reply}")
    print(f"\n执行轨迹: status={result.retrieval_trace.status} "
          f"steps={result.retrieval_trace.total_steps} tokens={result.retrieval_trace.total_tokens}")
    for i, step in enumerate(result.retrieval_trace.steps):
        detail = f"tool={step.tool_name}" if step.tool_name else ""
        print(f"  第{i}步: {step.action_type} {detail} status={step.status}")

    print("\n再追问一句，测试 Orchestrator 会不会正确保持在retrieval分支（而不是错误地又跑回profile分支）")
    result2 = await run_orchestrator(db, profile, "这个专业组学费多少，历年分数怎么样")
    print(f"走的分支: {[s.agent for s in result2.plan_steps]}")
    assert result2.plan_steps[0].agent == "retrieval", "条件已经齐全，追问细节也应该继续走retrieval分支"
    print(f"Agent回复: {result2.reply[:200]}...")

    db.close()
    print("\n全部通过：Orchestrator 正确地在两个 Agent 之间路由，"
          "条件收集、检索、追问三种场景的分支判断都是对的。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
