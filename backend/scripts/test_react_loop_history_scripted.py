"""
验证 run_react_loop() 新加的 history 参数：不发真实网络请求，直接检查
拼给 model_caller 的 contents 列表长什么样。

背景：Retrieval/Advisor Agent 之前每一轮都是"失忆"的——run_react_loop 只拿
这一轮的 initial_message 现起一段新对话，用户追问"刚才那个学校"这类依赖
上下文的问题会答不上来。这次给 run_react_loop 加了可选的 history 参数，
这个脚本验证两件事：
1. 不传 history 时，行为跟以前完全一样（不能因为加了新参数就悄悄改变
   没传这个参数的调用方的行为，比如 test_advisor_scripted.py、
   test_react_loop_scripted.py 里那些已经跑通的调用）
2. 传了 history 时，正确按时间顺序拼成 user/model 交替的 contents，
   "assistant"这个角色名要转换成 Gemini 要求的"model"

用法（backend/ 目录下，激活 venv）：
    python scripts/test_react_loop_history_scripted.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio

from app.schemas.candidate import ProfileIn
from app.services.react_loop import ModelResponse, run_react_loop
from app.services.tools import ToolContext


def make_ctx() -> ToolContext:
    # 这两个场景都不会走到工具调用那一步（model_caller 直接给最终回复），
    # 所以 db=None 也没关系，不会被用到。
    profile = ProfileIn(
        province="江苏", track="物理类", subjects=["物理", "化学"],
        score=637, rank=5200, major_keyword="计算机",
    )
    return ToolContext(db=None, profile=profile)


async def scenario_no_history_unchanged():
    print("=== 场景1：不传 history，行为应该跟以前完全一样 ===")
    captured = {}

    async def fake_caller(system_prompt, contents, tool_schemas):
        captured["contents"] = contents
        return ModelResponse(is_final_answer=True, text="final reply", tokens_used=1)

    await run_react_loop(
        agent_name="advisor", system_prompt="sys", initial_message="hello",
        ctx=make_ctx(), model_caller=fake_caller, tool_schemas=[],
    )
    expected = [{"role": "user", "parts": [{"text": "hello"}]}]
    assert captured["contents"] == expected, (
        f"不传 history 时 contents 变了，说明改动破坏了旧的调用方式。实际：{captured['contents']}"
    )
    print(f"通过。contents = {captured['contents']}")


async def scenario_history_is_prepended_correctly():
    print("\n=== 场景2：传 history，应该按顺序拼成 user/model 交替，assistant 转成 model ===")
    captured = {}

    async def fake_caller(system_prompt, contents, tool_schemas):
        captured["contents"] = contents
        return ModelResponse(is_final_answer=True, text="final reply", tokens_used=1)

    history = [
        {"role": "user", "content": "帮我查一下计算机相关的候选"},
        {"role": "assistant", "content": "已经帮你查到东南大学、南京邮电大学两个候选"},
    ]
    await run_react_loop(
        agent_name="advisor", system_prompt="sys", initial_message="这两个我该选哪个",
        ctx=make_ctx(), model_caller=fake_caller, tool_schemas=[], history=history,
    )
    expected = [
        {"role": "user", "parts": [{"text": "帮我查一下计算机相关的候选"}]},
        {"role": "model", "parts": [{"text": "已经帮你查到东南大学、南京邮电大学两个候选"}]},
        {"role": "user", "parts": [{"text": "这两个我该选哪个"}]},
    ]
    assert captured["contents"] == expected, (
        f"history 拼接得不对。实际：{captured['contents']}"
    )
    print(f"通过。contents = {captured['contents']}")


async def main():
    await scenario_no_history_unchanged()
    await scenario_history_is_prepended_correctly()
    print("\n全部2个场景通过。")


if __name__ == "__main__":
    asyncio.run(main())
