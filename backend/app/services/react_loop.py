"""
ReAct 循环：模型交替"决定调用哪个工具"和"看到工具结果后继续想"，
直到给出最终回复，或者触发某种边界保护被强制终止。

这一步刻意把"模型怎么被调用"（model_caller）做成可替换的依赖：
测试阶段传一个按脚本顺序返回响应的假模型，完全不发真实网络请求；
真实接入 Gemini function calling 时，只需要另外实现一个符合同一个
签名的 model_caller，循环本身的状态机逻辑不用动。这是从这次项目
一路调试 Gemini 连通性问题里直接得到的教训：状态机对不对、
和"这次网络通不通"，必须是两件可以分开验证的事。
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from app.services.limits import MAX_TOKENS_PER_RUN, RUN_TOTAL_TIMEOUT_SECONDS, TOOL_CALL_TIMEOUT_SECONDS
from app.services.tools import ToolContext, ToolResult, execute_tool

logger = logging.getLogger(__name__)

MAX_STEPS = 6


@dataclass
class ModelResponse:
    """model_caller 的统一返回结构，不管背后是脚本模拟还是真实 Gemini 调用。

    thought_signature：Gemini 3 系列模型的强制要求——模型返回 functionCall 时
    会附带这个字段，下一轮把这次工具调用喂回去时必须原样带上，否则报400
    "missing thought_signature"。Gemini 2.x 及更早的模型没有这个要求，
    脚本化测试用的假模型也不需要填这个字段（值为 None 时下面拼装请求体的
    地方会自动跳过，不影响其他场景）。
    """
    is_final_answer: bool
    text: str | None = None
    tool_name: str | None = None
    tool_args: dict | None = None
    tokens_used: int = 0
    thought_signature: str | None = None


# model_caller: (system_prompt, contents, tool_schemas) -> ModelResponse
ModelCaller = Callable[[str, list[dict], list[dict]], Awaitable[ModelResponse]]


@dataclass
class StepTrace:
    step_index: int
    action_type: str  # tool_call | final_answer | aborted_duplicate | aborted_max_steps
    tool_name: str | None = None
    tool_args: dict | None = None
    tool_result: dict | None = None
    tokens_used: int = 0
    status: str = "ok"  # ok | failed


@dataclass
class RunTrace:
    agent_name: str
    steps: list[StepTrace] = field(default_factory=list)
    status: str = "running"  # running | completed | aborted
    abort_reason: str | None = None
    total_tokens: int = 0

    @property
    def total_steps(self) -> int:
        return len(self.steps)


def _call_signature(tool_name: str, tool_args: dict | None) -> str:
    """把"工具名+参数"序列化成一个可比较的签名，用来检测重复调用。
    参数用 sort_keys 保证字典顺序不同但内容相同时签名一致。
    """
    return f"{tool_name}::{json.dumps(tool_args or {}, sort_keys=True, ensure_ascii=False)}"


def _fallback_text(trace: RunTrace) -> str:
    if trace.abort_reason == "duplicate_tool_call":
        return "检测到重复调用同一个工具、参数也一样，已终止本次请求，避免浪费资源。"
    if trace.abort_reason == "max_steps_exceeded":
        return "这个问题需要的步骤比较多，我先基于已经查到的信息回答，如果不够完整可以再补充问题。"
    if trace.abort_reason == "token_budget_exceeded":
        return "这个问题需要消耗的资源比较多，我先基于已经查到的信息回答，如果不够完整可以再补充问题。"
    if trace.abort_reason == "run_timeout_exceeded":
        return "这次处理耗时超出了预期，我先基于已经查到的信息回答，如果不够完整可以再试一次。"
    return "抱歉，处理这个请求时遇到了问题。"


async def _execute_tool_with_timeout(tool_name: str, tool_args: dict, ctx: ToolContext) -> ToolResult:
    """
    execute_tool() 本身是同步函数（内部就是几次数据库查询），用
    run_in_executor 扔到线程池里跑，再用 asyncio.wait_for 卡一个超时——
    asyncio.wait_for 只能取消"可等待对象"，卡不住一个正在跑的同步阻塞调用，
    所以必须先用 run_in_executor 包一层。现在这几个工具都很快，超时基本
    不会触发；这层保护主要是为了以后接入真实 web_search 之类会真正发外部
    请求的工具时，一步变慢不会拖死整个 ReAct 循环。
    """
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, execute_tool, tool_name, tool_args, ctx),
            timeout=TOOL_CALL_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("工具调用超时：tool=%s args=%s，超过 %s 秒", tool_name, tool_args, TOOL_CALL_TIMEOUT_SECONDS)
        return ToolResult(
            ok=False,
            error=f"工具执行超时（超过{TOOL_CALL_TIMEOUT_SECONDS}秒），已跳过。"
                  f"可以换个参数重试，或者直接基于已有信息回答。",
        )


async def run_react_loop(
    agent_name: str,
    system_prompt: str,
    initial_message: str,
    ctx: ToolContext,
    model_caller: ModelCaller,
    tool_schemas: list[dict],
    max_steps: int = MAX_STEPS,
    history: list[dict] | None = None,
) -> tuple[str, RunTrace]:
    """
    history：这一轮之前的对话历史，格式跟 ChatMessage 落库时一致——
    [{"role": "user"/"assistant", "content": "..."}, ...]，早的在前。
    默认 None（等价于空列表），是为了不破坏已有脚本化测试（test_react_loop_scripted.py、
    test_advisor_scripted.py 等）调用 run_react_loop 时不传这个参数的写法——
    这些测试验证的是状态机本身，不需要跨轮上下文。

    之前这里不管传不传历史，每次都从一句 initial_message 现起一段全新对话，
    Retrieval/Advisor Agent 因此是"失忆"的：用户追问"刚才那个学校"这类
    依赖上下文的问题，模型完全看不到上一轮聊了什么。这里补上历史拼接——
    只拼历史里的最终文字往返（user/assistant 文本），不重放上一轮内部的
    工具调用细节（functionCall/functionResponse），因为那些是这一轮
    ReAct 循环自己会重新决定要不要查的执行细节，不是需要延续的"事实"。
    """
    trace = RunTrace(agent_name=agent_name)
    contents: list[dict] = []
    for turn in (history or []):
        role = "model" if turn["role"] == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": turn["content"]}]})
    contents.append({"role": "user", "parts": [{"text": initial_message}]})
    seen_calls: set[str] = set()

    logger.info(
        "ReAct 循环开始：agent=%s max_steps=%d history_turns=%d",
        agent_name, max_steps, len(history or []),
    )

    async def _run_steps() -> str:
        for step_index in range(max_steps):
            # 保护3：单轮累计 token 达到预算上限，不管步数还剩多少都强制中断——
            # 放在每一步开始前检查（而不是模型给出最终回复之后再检查），这样
            # 已经超预算时就不会再多花一次模型调用去确认"是不是该停了"。
            if trace.total_tokens >= MAX_TOKENS_PER_RUN:
                logger.warning(
                    "单轮 token 预算超限：agent=%s total_tokens=%d 上限=%d，强制中断",
                    agent_name, trace.total_tokens, MAX_TOKENS_PER_RUN,
                )
                trace.status = "aborted"
                trace.abort_reason = "token_budget_exceeded"
                return _fallback_text(trace)

            response = await model_caller(system_prompt, contents, tool_schemas)
            trace.total_tokens += response.tokens_used

            if response.is_final_answer:
                trace.steps.append(StepTrace(
                    step_index=step_index, action_type="final_answer", tokens_used=response.tokens_used,
                ))
                trace.status = "completed"
                logger.info(
                    "ReAct 循环完成：agent=%s steps=%d total_tokens=%d",
                    agent_name, trace.total_steps, trace.total_tokens,
                )
                return response.text or ""

            # --- 模型请求调用工具 ---
            signature = _call_signature(response.tool_name, response.tool_args)

            # 保护1：同一个工具+同样的参数被重复调用，立即终止，不等到步数用完
            if signature in seen_calls:
                logger.warning(
                    "检测到重复工具调用：agent=%s tool=%s args=%s，强制中断",
                    agent_name, response.tool_name, response.tool_args,
                )
                trace.steps.append(StepTrace(
                    step_index=step_index, action_type="aborted_duplicate",
                    tool_name=response.tool_name, tool_args=response.tool_args,
                    tokens_used=response.tokens_used, status="failed",
                ))
                trace.status = "aborted"
                trace.abort_reason = "duplicate_tool_call"
                return _fallback_text(trace)
            seen_calls.add(signature)

            # 之前这里只记了工具名，没记参数、也没记结果——真实撞到过好几次
            # "查不到候选，但说不清模型到底传了什么参数"的排查困境，只能靠
            # 猜。这里把参数和结果摘要都打进日志（结果只摘 count/note 这两个
            # 关键字段，不把完整候选列表堆进日志，那个可能有几百条太大），
            # 出问题时直接看后端终端就能确认模型是不是真的传对了参数、工具
            # 到底返回了什么，不用再靠聊天回复的文字反推。
            logger.info(
                "第%d步调用工具：agent=%s tool=%s args=%s",
                step_index, agent_name, response.tool_name, response.tool_args,
            )
            result = await _execute_tool_with_timeout(response.tool_name, response.tool_args or {}, ctx)
            if result.ok:
                summary = {k: v for k, v in (result.data or {}).items() if k in ("count", "note")}
                logger.info(
                    "第%d步工具返回成功：agent=%s tool=%s summary=%s",
                    step_index, agent_name, response.tool_name, summary,
                )
            else:
                logger.warning(
                    "第%d步工具返回失败：agent=%s tool=%s error=%s",
                    step_index, agent_name, response.tool_name, result.error,
                )
            observation = result.to_observation()
            trace.steps.append(StepTrace(
                step_index=step_index, action_type="tool_call",
                tool_name=response.tool_name, tool_args=response.tool_args,
                tool_result=observation, tokens_used=response.tokens_used,
                status="ok" if result.ok else "failed",
            ))

            # 把这一步的"模型请求调用工具"和"工具返回的结果"都写回对话历史，
            # 下一轮模型能看到完整的往返记录再决定下一步——
            # 工具失败也原样喂回去，让模型自己判断要不要换个参数重试、
            # 还是直接告诉用户这项信息查不到，而不是循环在这里替模型做决定。
            # thoughtSignature 如果模型返回过，必须原样带回去（Gemini 3系列的强制
            # 要求），不然下一轮会报400 "missing thought_signature"。
            function_call_part: dict = {"functionCall": {
                "name": response.tool_name, "args": response.tool_args or {},
            }}
            if response.thought_signature:
                function_call_part["thoughtSignature"] = response.thought_signature
            contents.append({"role": "model", "parts": [function_call_part]})
            contents.append({"role": "user", "parts": [{"functionResponse": {
                "name": response.tool_name, "response": observation,
            }}]})

        # 保护2：达到最大步数仍未给出最终回复
        logger.warning("达到最大步数仍未完成：agent=%s max_steps=%d", agent_name, max_steps)
        trace.status = "aborted"
        trace.abort_reason = "max_steps_exceeded"
        return _fallback_text(trace)

    # 保护4：整轮耗时上限。cancel 掉 _run_steps() 时，trace 里已经记录的步骤
    # 不会丢——trace 是在外层创建、_run_steps() 内部只是往上面追加，被取消时
    # trace 保留着"超时之前已经跑到哪一步"的完整记录，方便事后排查是卡在
    # 哪一步耗时过长。
    try:
        text = await asyncio.wait_for(_run_steps(), timeout=RUN_TOTAL_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.warning(
            "整轮 ReAct 循环超时：agent=%s 已完成步数=%d 超过 %s 秒",
            agent_name, trace.total_steps, RUN_TOTAL_TIMEOUT_SECONDS,
        )
        trace.status = "aborted"
        trace.abort_reason = "run_timeout_exceeded"
        text = _fallback_text(trace)

    return text, trace
