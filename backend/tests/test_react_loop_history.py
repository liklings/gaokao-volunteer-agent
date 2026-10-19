"""
验证 run_react_loop() 的 history 参数：不发真实网络请求，直接检查
拼给 model_caller 的 contents 列表长什么样。

1. 不传 history 时，行为跟没有这个参数时完全一样。
2. 传了 history 时，按时间顺序拼成 user/model 交替的 contents，
   "assistant" 这个角色名要转换成 Gemini 要求的 "model"。

从 scripts/test_react_loop_history_scripted.py 迁移，断言逻辑原样保留。
"""
from app.services.react_loop import ModelResponse, run_react_loop
from app.services.tools import ToolContext

from .conftest import make_profile_in


def make_ctx() -> ToolContext:
    # 这两个场景都不会走到工具调用那一步（model_caller 直接给最终回复），
    # 所以 db=None 也没关系，不会被用到。
    return ToolContext(db=None, profile=make_profile_in())


async def test_no_history_contents_unchanged():
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


async def test_history_is_prepended_correctly():
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
    assert captured["contents"] == expected, f"history 拼接得不对。实际：{captured['contents']}"
