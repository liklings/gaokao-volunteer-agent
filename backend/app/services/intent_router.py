"""
路由决策：判断用户这句话该交给 Retrieval Agent（检索候选/回答候选相关
问题）还是 Advisor Agent（对比/取舍建议）。

背景：orchestrator._build_plan() 里"考生条件收集齐了没有"那条判断是
确定性代码、刻意不交给模型——字段是否为 None 是一个客观、可编程验证的
标准，没必要为了"更像 agent"就放弃更便宜可靠的方案，字段没收集齐就去
检索候选只会得到错误结果，这条硬门槛必须留在代码里。

但"字段收集齐了之后，这句话到底是想要检索还是想要对比取舍建议"，之前
用的是关键词匹配（_ADVISOR_KEYWORDS：对比/比较/选哪个/更推荐/取舍/哪个好），
覆盖不全——用户说"这两个我该怎么选"完全没命中任何关键词，明显是要对比
却会被当成检索处理。这里改成一次独立的、轻量级 Gemini 结构化调用来做
这个语义判断（用户在"要不要为了路由准确性多花一次调用"和"把判断硬塞进
已有调用里省成本"之间，明确选了前者）：

1. 职责单一、好调试——路由决策和字段抽取（agent_client.py）、检索/建议
   本身（react_loop.py）完全解耦，出问题时容易定位是哪一层判断错的。
2. 输出只有一个枚举字段、maxOutputTokens 压得很低、关掉了 thinking，
   尽量控制这次新增调用的成本和延迟。
3. 调用失败（网络错误、配额限流、没配 Key）不能让整轮对话失败——这次
   判断是"锦上添花"的准确性提升，不是必需的硬依赖，orchestrator 里
   调用失败时会退回原来的关键词匹配兜底，保证 Gemini 不可用时系统依然
   能工作（哪怕退化到路由不够精确）。
"""
from __future__ import annotations

import json
import logging

import httpx

from app.config import get_settings
from app.services.agent_client import GEMINI_ENDPOINT
from app.services.gemini_errors import GeminiCallError, GeminiConfigError
from app.services.gemini_retry import call_with_retry

logger = logging.getLogger(__name__)

INTENT_SEARCH = "search"
INTENT_COMPARE = "compare"

# 只做一次轻量判断，不值得跟核心 Agent 调用一样重试 2 次+线性退避到底——
# 失败了就尽快让 orchestrator 退回关键词兜底，用户不该为了"路由更准"
# 多等好几秒。
_MAX_RETRIES = 1

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": [INTENT_SEARCH, INTENT_COMPARE]},
    },
    "required": ["intent"],
}

_SYSTEM_PROMPT = """你是一个只做一件事的路由判断器：判断用户最新这句话，接下来应该交给
"search（检索）"还是"compare（对比建议）"来处理。只看最近的对话历史和这句话本身，
不要多想、不要展开解释。

- search：用户想要查候选院校/专业组、想知道有哪些学校符合条件、想了解
  某个候选的具体信息（学费、历年数据、专业方向、录取概率），或者只是
  笼统地继续聊报考相关的事，没有明确表达"要在已有候选之间做取舍"。
- compare：用户明确想在两个或多个候选之间做取舍、对比、选择，比如
  "这两个我该选哪个""A和B比起来呢""你更推荐哪个""该不该服从调剂"
  这类需要给出倾向性建议的问题。

只输出符合给定 JSON Schema 的对象，不要输出 JSON 之外的任何文字。"""


async def _call_once(history: list[dict], user_message: str) -> str:
    settings = get_settings()
    if not settings.gemini_api_key:
        raise GeminiConfigError("未配置 GEMINI_API_KEY，无法调用路由判断。")

    contents = []
    # 只取最近几轮——路由判断只需要"最近在聊什么"这点上下文就够判断意图了，
    # 不需要完整历史，控制这次调用的输入 token 数量（也是控制成本的一部分）。
    for m in history[-6:]:
        role = "model" if m["role"] == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": m["content"]}]})
    contents.append({"role": "user", "parts": [{"text": user_message}]})

    body = {
        "systemInstruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
        "contents": contents,
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _RESPONSE_SCHEMA,
            "maxOutputTokens": 128,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }

    url = GEMINI_ENDPOINT.format(model=settings.gemini_model)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                headers={"x-goog-api-key": settings.gemini_api_key, "Content-Type": "application/json"},
                json=body,
                timeout=30.0,
            )
    except httpx.HTTPError as exc:
        raise GeminiCallError(f"路由判断请求失败（网络层，{type(exc).__name__}）：{exc}") from exc

    if resp.status_code != 200:
        raise GeminiCallError(f"路由判断 Gemini API 返回 {resp.status_code}：{resp.text[:1000]}")

    try:
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text)
        intent = parsed["intent"]
    except (KeyError, IndexError, ValueError) as exc:
        raise GeminiCallError(f"解析路由判断返回内容失败：{exc}；原始响应：{resp.text[:1000]}") from exc

    if intent not in (INTENT_SEARCH, INTENT_COMPARE):
        raise GeminiCallError(f"路由判断返回了非法的 intent 值：{intent!r}")
    return intent


async def classify_intent(history: list[dict], user_message: str) -> str:
    """返回 INTENT_SEARCH 或 INTENT_COMPARE。失败时抛出 GeminiCallError /
    GeminiConfigError——调用方（orchestrator._build_plan）负责捕获并退回
    关键词匹配兜底，这里不做兜底、只负责"判断准的时候给出准确结果"。
    """
    return await call_with_retry(lambda: _call_once(history, user_message), max_retries=_MAX_RETRIES)
