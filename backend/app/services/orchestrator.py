"""
Orchestrator：根据当前 profile 状态和用户消息，决定这一轮该走哪个 Agent、
依次执行、汇总结果。

"要不要收集条件"这条判断（profile 字段齐不齐）刻意做成确定性代码
（_build_plan 里的 compute_missing），不是每次都问一次模型——这类判断
本身有明确规则，没必要为此消耗一次 Gemini 调用/额度，出问题时也更容易
调试（不用怀疑是不是模型分类错了）。这跟项目里"missing/ready 由服务器
计算，不采信模型自报"是同一条设计原则的延伸。

现在编排了 profile（收集条件）、retrieval（检索，已实测跑通）、
advisor（对比/取舍建议，基于 city_pref 等偏好）三个 Agent。字段收集齐了
之后"该走 retrieval 还是 advisor"，最初是用关键词匹配判断的，覆盖不全
（"这两个我该怎么选"没命中任何关键词），后来换成了 app/services/
intent_router.py 里一次独立的、轻量级 Gemini 结构化调用做真实的语义
判断——这是刻意跟"字段齐不齐"那条判断区别对待的：字段是否为 None 是
客观、可编程验证的标准，永远不该交给模型；但"这句话的意图是检索还是
对比"是真正需要语言理解的语义判断，关键词匹配天然覆盖不全，这种情况
才值得为准确性多花一次 Gemini 调用。这次路由调用失败时会退回原来的
关键词匹配兜底（_ADVISOR_KEYWORDS），保证 Gemini 不可用时系统依然能
工作，只是路由不够准确。

联网搜索（web_search）：最初调研过接 Gemini 自带的 google_search grounding
功能，但实测这个免费 Key 对该功能额度是0（不是普通调用额度用完，是这个
功能本身免费层不给额度），需要开通计费账号才能用，当时按设计文档里"联网
检索单独作为可整体开关的增量能力接入"的建议先搁置了。后来改用 MCP
（Model Context Protocol）接入的方式解决——web_search 现在通过
app/services/mcp_client.py 这个 MCP 客户端转发给外部 MCP server 执行
（默认接的是仓库自带的离线参考实现 app/mcp_servers/policy_search_server.py，
真实联网搜索 server 可以通过 mcp_servers.json 配置文件直接替换，不用改
这里的编排代码），彻底绕开了 Gemini grounding 额度这个问题，同时保留了
"以后随时能换成真实联网数据源"的扩展性。

同样通过 MCP 这次一起接入的还有 Skill 机制（app/services/skills.py）：
分领域的政策知识（专项计划、平行志愿、征集志愿、专业调剂）不再考虑硬编码
进系统提示词，而是做成"目录常驻、正文按需通过 load_skill 工具加载"，
retrieval 和 advisor 两个 Agent 都能用。

【当前测试状态】
_build_plan()"字段齐不齐"这层判断是纯函数、不碰网络；"该走 retrieval 还是
advisor"这层现在依赖 intent_router.classify_intent()，测试里用 monkeypatch
换掉这个依赖（tests/test_orchestrator_planning.py、tests/test_advisor.py），
不发真实请求——都已经用真实数据库的 Profile 行测试过分支判断和失败兜底。
run_orchestrator() 里 profile/retrieval 两步已经用真实 Gemini 端到端跑通
（scripts/test_orchestrator_real.py），advisor 这一步先用
scripts/test_advisor_scripted.py 脚本化验证过状态机和分支路由，
真实 Gemini 联调用 scripts/test_advisor_real.py。

【接入真实接口】
app/api/chat.py 的 /api/chat 现在直接调用这里的 run_orchestrator，
不再只调用 Profile Agent——之前这个接口一直没接到 Orchestrator 上，
条件收集齐了之后也不会往下走 retrieval/advisor，是测试脚本能跑通、
但真实接口跑不通完整流程的一个缺口，现在补上了。

【成本控制与可观测性】
run_orchestrator() 开头做两件事：调用 check_rate_limit()（见
app/services/limits.py）挡掉同一个 profile_id 短时间内过于频繁的请求，
调用 new_run_id()（见 app/services/logging_config.py）生成这次请求的
run_id 并设进日志上下文——下游 react_loop.py/gemini_tool_caller.py/
agent_client.py 里任何一条日志都会自动带上它，同一个 run_id 也存进了
persist_trace() 落库的 AgentRun 行，日志和数据库记录能直接对上。
ReAct 循环本身的单轮 token 预算/单步工具超时/整轮总耗时上限，实现在
react_loop.py 里，阈值定义在 limits.py。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ChatMessage, Profile
from app.schemas.candidate import ProfileIn
from app.services.agent_client import call_agent as call_profile_agent
from app.services.agent_prompts import ADVISOR_SYSTEM_PROMPT, RETRIEVAL_SYSTEM_PROMPT
from app.services.gemini_errors import GeminiCallError, GeminiConfigError
from app.services.gemini_tool_caller import call_gemini_with_tools
from app.services.intent_router import INTENT_COMPARE, classify_intent
from app.services.limits import RateLimitExceededError, check_rate_limit
from app.services.logging_config import new_run_id
from app.services.profile_rules import compute_missing
from app.services.react_loop import RunTrace, run_react_loop
from app.services.tools import TOOL_SCHEMAS, ToolContext
from app.services.trace_persistence import persist_trace

logger = logging.getLogger(__name__)

MAX_PLAN_STEPS = 4  # 一轮编排最多执行几步，防止规划本身也失控（跟ReAct的MAX_STEPS是同一类保护）

PROFILE_FIELDS = [
    "province", "year", "track", "subjects", "score", "rank",
    "major_keyword", "city_pref", "budget_max", "home_province_only",
    "accept_sino_foreign", "accept_special_program",
]

# Advisor Agent 只负责"基于已有候选给建议"，不负责重新检索、也不负责联网
# 搜索——这是刻意的职责划分，检索/联网信息收集是 Retrieval Agent 的事。
# 所以工具集要过滤掉 search_candidates 和 web_search，只留
# get_candidate_detail / compare_candidates / load_skill（Skill 是纯知识
# 加载，跟"要不要重新检索候选"这条职责边界无关，Advisor 给取舍建议时
# 同样可能需要引用政策知识，所以两个 Agent 都保留这个工具）。
ADVISOR_TOOL_SCHEMAS = [
    s for s in TOOL_SCHEMAS
    if s["name"] in (
        "get_candidate_detail", "compare_candidates", "load_skill", "search_policy", "finalize_recommendation",
    )
]

# 服务器端兜底文案——Advisor 这一轮没能拿到一次成功的 finalize_recommendation
# 校验时，直接用这句话替换掉模型原本想说的任何内容，宁可让用户体验"这次
# 没给出建议、请再问一次"，也不能把一份没经过底线校验的推荐名单当成正式
# 建议发出去。这跟 react_loop.py 里 _fallback_text() 是同一类"确定性降级
# 文案"，只是触发条件不是"超时/超预算"，是"该走的强制校验没走完"。
_UNVALIDATED_RECOMMENDATION_FALLBACK = (
    "这次生成的建议还没有完成必须的底线校验（选科/预算等硬性规则核验），"
    "为避免给出不准确的推荐，暂时不能直接展示这份建议。可以换个方式重新问一次，"
    "或者先通过'查询候选'看一下有哪些候选，再回来问取舍建议。"
)

# 退回兜底用的简化版分类器——正常情况下"该走 retrieval 还是 advisor"
# 现在由 intent_router.classify_intent() 做真实的语义判断（见下面 _build_plan
# 的调用），这份关键词表只在那次 Gemini 调用失败（网络/配额限流/没配Key）
# 时才会被用到，保证 Gemini 不可用时系统依然能工作，哪怕退化到路由不够准确。
_ADVISOR_KEYWORDS = ("对比", "比较", "选哪个", "更推荐", "取舍", "哪个好")


@dataclass
class PlanStep:
    step_id: int
    agent: str  # "profile" | "retrieval" | "advisor"
    goal: str
    status: str = "pending"  # pending | done | failed
    result_summary: str | None = None


@dataclass
class OrchestratorResult:
    reply: str
    profile_id: int
    missing: list[str]
    ready: bool
    plan_steps: list[PlanStep] = field(default_factory=list)
    # 命名沿用最早只有 retrieval 分支时定的名字，没有跟着改成更通用的名字
    # 是为了不破坏已经跑通的 scripts/test_orchestrator_real.py 里对这个字段的引用。
    # 实际上这个字段装的是"这一轮如果走了 retrieval 或 advisor 分支，那一步的
    # ReAct 执行轨迹"——两者复用同一套 RunTrace 结构，谁执行了就是谁的 trace。
    retrieval_trace: RunTrace | None = None


async def _build_plan(
    profile: Profile, user_message: str = "", history: list[dict] | None = None,
) -> list[PlanStep]:
    """
    Orchestrator 里唯一"决策"的地方，根据 profile 当前状态 + 这句话（+ 最近
    对话历史）决定这一轮走哪个 Agent。

    这里有两层判断，刻意分开处理：
    1. "必填字段收集齐了没有"——客观、可编程验证的标准，永远是确定性代码
       判断（compute_missing），不交给模型。字段没收集齐就去检索候选只会
       得到错误结果，这条硬门槛不能有任何不确定性。
    2. "字段齐了之后，这句话该走 retrieval 还是 advisor"——这是真正的语义
       判断，交给 intent_router.classify_intent() 做一次独立的轻量级 Gemini
       调用（详见该模块顶部的设计说明）。这次调用失败时不能让整轮对话
       失败，退回 _ADVISOR_KEYWORDS 关键词匹配兜底。

    user_message/history 都给了默认值，是为了兼容"缺条件"场景下的旧调用
    方式（那种场景在第 1 层判断就直接返回了，用不上这两个参数）。
    """
    missing = compute_missing(profile)
    if missing:
        return [PlanStep(step_id=0, agent="profile", goal=f"补充缺失条件：{'、'.join(missing)}")]

    intent = None
    try:
        intent = await classify_intent(history or [], user_message)
    except (GeminiCallError, GeminiConfigError) as exc:
        logger.warning("路由判断（classify_intent）调用失败，退回关键词匹配兜底：%s", exc)

    is_compare = (
        intent == INTENT_COMPARE if intent is not None
        else any(kw in user_message for kw in _ADVISOR_KEYWORDS)
    )
    if is_compare:
        return [PlanStep(step_id=0, agent="advisor", goal="基于已检索的候选生成对比/取舍建议")]
    return [PlanStep(step_id=0, agent="retrieval", goal="根据已确认的条件检索候选或回答用户问题")]


def _profile_to_profile_in(profile: Profile) -> ProfileIn:
    return ProfileIn(
        province=profile.province, track=profile.track, subjects=profile.subjects or [],
        score=profile.score, rank=profile.rank, major_keyword=profile.major_keyword,
        city_pref=profile.city_pref, budget_max=profile.budget_max,
        home_province_only=profile.home_province_only,
        accept_sino_foreign=profile.accept_sino_foreign,
        accept_special_program=profile.accept_special_program,
    )


def _merge_profile_update(p: Profile, update: dict) -> None:
    for f in PROFILE_FIELDS:
        if f in update and update[f] is not None:
            setattr(p, f, update[f])


def _advisor_recommendation_was_validated(trace: RunTrace) -> bool:
    """判断这一轮 Advisor 的执行轨迹里，是否真的有一次成功的
    finalize_recommendation 调用——只看"调用过且成功"，不关心是不是最后
    一步、中间还调用过几次别的工具，模型在校验通过之后再补查一次细节
    是完全合理的行为，不该因此被判定为"没通过校验"。

    已知的局限（明确记录，不假装这层能兜住一切）：这里只能确认"至少有
    一次名单通过了 finalize_recommendation 校验"，没法确认模型最后给出的
    文字回复里提到的学校，跟通过校验的那份名单完全一致——如果模型在校验
    通过之后又在文字里额外提了一个没校验过的学校，这一层拦不住。这是
    "服务器能确定性校验的" 和 "模型自由文本里写了什么" 之间天然存在的
    边界，缩小这个差距需要要求模型的最终文字回复也走结构化生成，属于
    更大的改动，先如实记录在这里，不在文档里含糊带过。
    """
    return any(
        step.action_type == "tool_call"
        and step.tool_name == "finalize_recommendation"
        and step.status == "ok"
        for step in trace.steps
    )


def _load_history(db: Session, profile_id: int) -> list[dict]:
    """取出这个 profile 到目前为止的完整对话历史（不含这一轮还没落库的这句话），
    格式统一成 [{"role": "user"/"assistant", "content": "..."}, ...]，早的在前。

    三个 Agent 的 _run_xxx_step 都要用这个，之前只有 Profile Agent 那步在读，
    Retrieval/Advisor 两步完全没传历史进去，每一轮都是"失忆"的——用户追问
    "刚才那个学校""这几个里你更推荐哪个"这类依赖上下文的问题，模型根本看不到
    上一轮聊了什么，只能靠这一轮孤立的一句话猜。这里统一提供，避免同样的
    查询逻辑分散写三份、以后改的时候漏改。
    """
    rows = db.scalars(
        select(ChatMessage).where(ChatMessage.profile_id == profile_id).order_by(ChatMessage.created_at)
    ).all()
    return [{"role": m.role, "content": m.content} for m in rows]


async def _run_profile_step(db: Session, profile: Profile, user_message: str) -> str:
    """执行 Profile Agent 这一步：需要真实调用 Gemini（agent_client.call_agent）。"""
    history = _load_history(db, profile.id)
    profile_json = json.dumps(_profile_to_profile_in(profile).model_dump(), ensure_ascii=False)

    db.add(ChatMessage(profile_id=profile.id, role="user", content=user_message))
    db.flush()

    result = await call_profile_agent(profile_json, history, user_message)
    _merge_profile_update(profile, result.get("profile") or {})
    db.add(ChatMessage(profile_id=profile.id, role="assistant", content=result["reply"]))
    db.commit()
    return result["reply"]


async def _refresh_optional_profile_fields(db: Session, profile: Profile, user_message: str) -> None:
    """必填字段已经收集齐、_build_plan 决定直接走 retrieval/advisor 时，仍然
    补跑一次静默的字段提取。

    真实撞到的 bug：必填字段（省份/类别/选科/分数或位次/专业关键词）一旦
    在更早的对话里collect齐了，_build_plan 就再也不会选中 profile 分支——
    用户后面才补充的"学费上限一万""优先考虑省内城市"这类可选字段
    （budget_max/city_pref/home_province_only）因此永远不会被写进 Profile
    行：右侧"当前条件"面板一直显示"待补充"，直接查询候选用的还是没有
    预算/省内限制的旧条件，聊天里 Retrieval Agent 也只能靠自己读这句话
    临时猜、猜完这轮工具调用一结束又忘了，下一轮同样的问题重新来一遍。

    这里不改变"必填字段齐不齐"这条硬门槛判断（compute_missing 还是
    唯一权威），只是在门槛已经过了、要往下走 retrieval/advisor 之前，
    补跑一次跟 _run_profile_step 一样的字段抽取+合并，但不生成、不落库
    对话回复——用户这一轮真正想看到的是 retrieval/advisor 给出的实质性
    内容，不需要在中间插一句"已收到"这类机械确认，也不需要为此多存一条
    容易让前端对话气泡对不上号的 assistant 消息。这次调用失败（网络/配额）
    时直接跳过，不阻塞这一轮的检索/建议——刷新可选字段是"锦上添花"，
    不是能让整轮请求失败的硬依赖，跟 intent_router 的失败兜底是同一种
    取舍。
    """
    history = _load_history(db, profile.id)
    profile_json = json.dumps(_profile_to_profile_in(profile).model_dump(), ensure_ascii=False)
    try:
        result = await call_profile_agent(profile_json, history, user_message)
    except (GeminiCallError, GeminiConfigError) as exc:
        logger.warning("刷新可选字段（学费上限/城市偏好/只看省内等）失败，跳过：%s", exc)
        return
    _merge_profile_update(profile, result.get("profile") or {})
    db.commit()


async def _run_retrieval_step(
    db: Session, profile: Profile, user_message: str, run_id: str | None = None,
) -> tuple[str, RunTrace]:
    """执行 Retrieval Agent 这一步：需要真实调用 Gemini function calling（已实测跑通）。

    之前这里只跑 ReAct 循环、把 trace 落库，没有把这一轮的用户消息/助手回复
    写进 ChatMessage 表，也没把之前的对话历史传给 run_react_loop——脚本化
    测试不关心这个（断言只看 trace），但接到真实 /api/chat 接口后就意味着
    这条分支既不写历史、也不读历史，用户追问会答不上来。这里两处一起补上，
    写法跟 _run_profile_step 一致：历史在调用 Gemini 之前先查好，用户消息
    在调用 Gemini 之前就先提交，避免调用失败时这句话跟着丢。
    """
    history = _load_history(db, profile.id)

    db.add(ChatMessage(profile_id=profile.id, role="user", content=user_message))
    db.commit()

    ctx = ToolContext(db=db, profile=_profile_to_profile_in(profile))
    text, trace = await run_react_loop(
        agent_name="retrieval", system_prompt=RETRIEVAL_SYSTEM_PROMPT,
        initial_message=user_message, ctx=ctx,
        model_caller=call_gemini_with_tools, tool_schemas=TOOL_SCHEMAS,
        history=history,
    )
    db.add(ChatMessage(profile_id=profile.id, role="assistant", content=text))
    persist_trace(db, trace, profile_id=profile.id, run_id=run_id)  # 内部会 commit，助手消息和 trace 一起落库
    return text, trace


async def _run_advisor_step(
    db: Session, profile: Profile, user_message: str, run_id: str | None = None,
) -> tuple[str, RunTrace]:
    """执行 Advisor Agent 这一步：复用和 Retrieval 同一套 ReAct 循环骨架，
    只是系统提示词和工具集不同（不含 search_candidates / web_search）。

    同上，补上历史读取+ChatMessage 落库，理由和 _run_retrieval_step 那段
    注释一致——这一步尤其重要，因为"帮我对比一下"这类请求经常紧跟在
    Retrieval 已经检索出候选之后，Advisor 需要知道前面聊到了哪些候选，
    不然用户说"这两个我该选哪个"时，Advisor 根本不知道"这两个"是谁。
    """
    history = _load_history(db, profile.id)

    db.add(ChatMessage(profile_id=profile.id, role="user", content=user_message))
    db.commit()

    ctx = ToolContext(db=db, profile=_profile_to_profile_in(profile))
    text, trace = await run_react_loop(
        agent_name="advisor", system_prompt=ADVISOR_SYSTEM_PROMPT,
        initial_message=user_message, ctx=ctx,
        model_caller=call_gemini_with_tools, tool_schemas=ADVISOR_TOOL_SCHEMAS,
        history=history,
    )

    # 底线校验网关的服务器端强制点：不管 ReAct 循环本身是不是"正常完成"、
    # 不管模型最后的文字回复读起来多么言之凿凿，只要这一轮的执行轨迹里
    # 找不到一次成功的 finalize_recommendation 调用，就不允许这段文字
    # 作为最终建议写进聊天记录、返回给用户——必须在这里、写入 ChatMessage
    # 之前拦截，写进去之后再"事后校验"就晚了，聊天记录已经把没校验过的
    # 建议当成正式内容存下来了。
    if trace.status == "completed" and not _advisor_recommendation_was_validated(trace):
        logger.warning(
            "Advisor 本轮没有成功调用 finalize_recommendation，拦截回复：profile_id=%d",
            profile.id,
        )
        text = _UNVALIDATED_RECOMMENDATION_FALLBACK
        trace.status = "aborted"
        trace.abort_reason = "recommendation_not_validated"

    db.add(ChatMessage(profile_id=profile.id, role="assistant", content=text))
    persist_trace(db, trace, profile_id=profile.id, run_id=run_id)  # 内部会 commit，助手消息和 trace 一起落库
    return text, trace


async def run_orchestrator(db: Session, profile: Profile, user_message: str) -> OrchestratorResult:
    # run_id 在这里生成、设进 contextvar——这是这次请求真正开始处理的起点，
    # 之后不管走 profile/retrieval/advisor 哪个分支、不管调了几次 Gemini、
    # 中间打了多少条日志，全部自动带上这同一个 run_id（见 logging_config.py），
    # 事后出问题直接 grep 这个 run_id 就能把这次请求的完整过程摘出来。
    run_id = new_run_id()
    logger.info("run_orchestrator 开始：profile_id=%d message=%r", profile.id, user_message)

    # 限流检查放在最前面，比 _build_plan 还早——不管这一轮最终会走哪个分支，
    # 只要要走到某个分支就一定会真正调用 Gemini、花掉额度，所以必须在
    # 任何分支执行之前就拦下来，不能等分支选完了再判断。这里现在还有
    # 一层额外理由：_build_plan 本身也会调用一次 Gemini（intent_router
    # 路由判断），限流检查更不能放在它后面。
    check_rate_limit(db, profile.id)

    # _build_plan 里的路由判断（classify_intent）需要"最近聊了什么"这点
    # 上下文才能判断准，这里先查一次历史传进去；下面 profile/retrieval/
    # advisor 各步骤内部还会各自查一次自己需要的完整历史（写法保持不变，
    # 避免这次改动牵连到那几步的历史加载逻辑），多出来的这次查询开销
    # 可以忽略不计，不值得为了省一次 SELECT 去重构那几步的历史传递方式。
    history_for_routing = _load_history(db, profile.id)
    plan = await _build_plan(profile, user_message, history_for_routing)
    executed_steps: list[PlanStep] = []
    reply_text = ""
    retrieval_trace: RunTrace | None = None

    for step in plan[:MAX_PLAN_STEPS]:
        logger.info("执行 plan step：agent=%s goal=%s", step.agent, step.goal)

        if step.agent == "profile":
            reply_text = await _run_profile_step(db, profile, user_message)
            step.status = "done"
            step.result_summary = "已更新考生条件"

        elif step.agent in ("retrieval", "advisor"):
            # 必填字段已经齐了、这一轮要走 retrieval/advisor 分支之前，先静默
            # 刷新一次可选字段（学费上限/城市偏好/只看省内等）——这句话仍然
            # 可能包含这类新信息，见 _refresh_optional_profile_fields 的说明。
            # 必须在这里、构造 ToolContext 之前完成，_run_retrieval_step/
            # _run_advisor_step 内部都是拿当前 profile 现读现建 ProfileIn。
            await _refresh_optional_profile_fields(db, profile, user_message)

        if step.agent == "retrieval":
            reply_text, retrieval_trace = await _run_retrieval_step(db, profile, user_message, run_id=run_id)
            step.status = "done" if retrieval_trace.status == "completed" else "failed"
            step.result_summary = f"status={retrieval_trace.status}, steps={retrieval_trace.total_steps}"

        elif step.agent == "advisor":
            reply_text, retrieval_trace = await _run_advisor_step(db, profile, user_message, run_id=run_id)
            step.status = "done" if retrieval_trace.status == "completed" else "failed"
            step.result_summary = f"status={retrieval_trace.status}, steps={retrieval_trace.total_steps}"

        executed_steps.append(step)

    missing = compute_missing(profile)
    logger.info(
        "run_orchestrator 结束：profile_id=%d ready=%s steps=%s",
        profile.id, len(missing) == 0, [s.agent for s in executed_steps],
    )
    return OrchestratorResult(
        reply=reply_text, profile_id=profile.id, missing=missing, ready=(len(missing) == 0),
        plan_steps=executed_steps, retrieval_trace=retrieval_trace,
    )
