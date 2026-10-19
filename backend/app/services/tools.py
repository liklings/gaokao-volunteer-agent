"""
工具适配层：把现有的业务逻辑（资格过滤打分、单条详情查询）包装成 ReAct 循环
能统一调度的"工具"，为将来接入真实的 Gemini function calling 做准备。

设计要点：
1. 工具函数签名统一为 (args: dict, ctx: ToolContext) -> ToolResult，
   ReAct 循环不需要关心每个工具内部到底怎么实现，只需要按名字分发、
   统一处理返回结果——这是这次刻意做的抽象层，避免以后加新工具时
   要去改循环本身的代码。
2. ToolContext 承载"这次对话已经确定、不需要模型每次调用工具都重新提供"
   的上下文（数据库连接、当前考生条件）。模型只需要传"这次调用工具
   特有的参数"（比如 group_id、要不要覆盖某个筛选条件），
   不用把整个 profile 塞进每一次工具调用的参数里——既省 token，
   也避免模型在转述 profile 时抄错字段。
3. 工具内部的任何异常，都在 execute_tool() 这一层被统一捕获转成
   ToolResult(ok=False, ...)，绝不让异常直接抛出到 ReAct 循环外面。
   工具失败也是一种"观察结果"，应该让模型看到、自己决定怎么应对
   （换个参数重试、或者告诉用户查不到），而不是让整个请求直接崩溃。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models import AdmissionGroup
from app.schemas.candidate import ProfileIn
from app.services.candidate_curation import curate
from app.services.mcp_client import get_mcp_manager
from app.services.policy_search import search_policy
from app.services.recommendation_guard import validate_recommendation
from app.services.rules_engine import (
    build_rank_scale_context,
    evaluate_group,
    search_candidates as run_search_candidates,
)
from app.services.skills import SkillNotFoundError, list_skills, load_skill_content

# 单次 search_candidates 最多把多少条候选喂回给模型——
# 完整的 CandidateOut 带着历年数据和规则明细，字段很多，
# 全量塞给模型既浪费 token 又没必要，工具这一层先做一次"瘦身"，
# 只保留决策阶段真正用得上的字段，需要更多细节时模型可以再调用
# get_candidate_detail 单独查。
MAX_CANDIDATES_RETURNED = 15


@dataclass
class ToolContext:
    """执行工具时的会话上下文，由 ReAct 循环在每次调用前注入，不暴露给模型。"""
    db: Session
    profile: ProfileIn


@dataclass
class ToolResult:
    ok: bool
    data: dict[str, Any] | None = None
    error: str | None = None

    def to_observation(self) -> dict:
        """转成喂回给模型的观察结果，格式统一，方便模型判断这一步是否成功。"""
        if self.ok:
            return {"ok": True, "data": self.data}
        return {"ok": False, "error": self.error}


def _load_groups(db: Session, group_id: str | None = None) -> list[AdmissionGroup]:
    stmt = select(AdmissionGroup).options(
        joinedload(AdmissionGroup.school),
        joinedload(AdmissionGroup.records),
    )
    if group_id is not None:
        stmt = stmt.where(AdmissionGroup.id == group_id)
    return list(db.scalars(stmt).unique().all())


#  只有这两个字段允许模型在单次 search_candidates 调用里临时覆盖 profile——
#  都是"软匹配"，就算传错/传得跟 profile 不一致，最多只是排序/关键词层面
#  有偏差，不会导致硬性资格判断本身不可预测。
#  province/track/subjects/score/rank/budget_max/home_province_only 这些
#  定义"硬性条件"的字段，一律不接受模型逐次调用时的临时覆盖——只能通过
#  _refresh_optional_profile_fields / 档案录入这条确定性链路写入 profile，
#  否则会出现"模型这次调用没传 home_province_only、上次传了 true"这种跟
#  真实 profile 状态脱节的情况，导致同一个 profile 在不同轮次里查出来的
#  硬性筛选结果不一致（例如放宽学费上限后结果反而变少，只因为模型这次
#  的调用参数里恰好漏传了/多传了某个硬性字段）。
_SAFE_OVERRIDE_FIELDS = {"major_keyword", "city_pref"}


def _tool_search_candidates(args: dict, ctx: ToolContext) -> ToolResult:
    """
    args 里只允许覆盖 major_keyword / city_pref 这两个软匹配字段（比如模型想换个
    专业关键词重新查一次），硬性条件字段一律忽略模型传参、只认 profile 当前值——
    未提供的字段（以及所有硬性字段）沿用当前已收集的 profile。
    """
    overrides = {
        k: v for k, v in args.items()
        if v is not None and k in _SAFE_OVERRIDE_FIELDS
    }
    try:
        # 用 model_validate 重新走一遍完整校验，而不是 model_copy(update=...)：
        # model_copy(update=...) 是 Pydantic v2 的已知行为——它不会重新校验，
        # 如果模型传来的参数类型不对（比如 budget_max 传成带单位的字符串
        # "10000元"），这里不会报错，等到 rules_engine 里拿它跟 int 类型的
        # 学费做比较时才会因为类型不匹配抛出 TypeError，那时候错误信息已经
        # 跟"到底是哪个参数传错了"脱节，模型也没法据此判断是自己传参错误
        # 还是真的查不到数据。这里改成校验，类型不对时能在这一步就拿到
        # 清晰的错误信息喂回给模型。
        merged_profile = ProfileIn.model_validate({**ctx.profile.model_dump(), **overrides})
    except Exception as exc:  # noqa: BLE001 - 参数格式不对时要把具体原因喂回模型，不是笼统报错
        return ToolResult(ok=False, error=f"参数不合法：{exc}")

    groups = _load_groups(ctx.db)
    results, _audit = run_search_candidates(groups, merged_profile, db=ctx.db)

    if not results:
        applied = {
            "省份": merged_profile.province, "科类": merged_profile.track,
            "选科": "+".join(merged_profile.subjects) if merged_profile.subjects else None,
            "专业关键词（软匹配，不影响本条）": merged_profile.major_keyword,
            "学费预算上限": merged_profile.budget_max,
            "只看省内院校": merged_profile.home_province_only,
        }
        applied_str = "；".join(f"{k}={v}" for k, v in applied.items() if v not in (None, False))
        return ToolResult(ok=True, data={
            "count": 0,
            "candidates": [],
            "note": (
                f"没有找到同时满足全部硬性条件的候选。这次实际生效的硬性条件是：{applied_str or '（无额外限制）'}。"
                "系统的硬性条件只有这些——不包含专业关键词（那是软匹配，不会导致 0 结果）、"
                "也不存在其他任何过滤维度，回复用户时不要提到这个列表之外的条件。"
                "可以尝试放宽学费预算、关闭'只看省内院校'，或换一个城市偏好后重新调用。"
            ),
        })

    simplified = [
        {
            "group_id": c.group_id,
            "university": c.university,
            "city": c.city,
            "group_name": c.group_name,
            "rank_diff": c.diff,
            "category_label": c.category_label,
            "tuition": c.tuition,
            "tags": c.tags,
        }
        for c in results[:MAX_CANDIDATES_RETURNED]
    ]

    # tiers：真实撞到的问题——之前模型只拿到上面这份"candidates"（纯按位次差
    # 降序取前15条），如果恰好有一堆学费便宜、历年最低位次很宽松的专科/
    # 职业本科院校，它们的位次差天然会算得很大（分数线越低、diff越大），
    # 能把真正适合考生（尤其是位次靠前、本该冲985/211）的本科
    # 院校全部挤出前15条之外——模型压根看不到这些学校，只能对着一屏幕专科
    # 院校犹豫"这些是不是不太对"，转而向用户抛出一堆澄清问题，而不是直接
    # 给出冲/稳/保结构化建议。tiers 复用候选卡片区已经在用、也已经测试过的
    # curate() 分档逻辑（app/services/candidate_curation.py）：按保底/稳妥/
    # 冲刺三档、且档内优先按学校质量（985/211/双一流标签、综合排名）排序，
    # 名额按各档候选数量比例分配，保证只要某档有候选，模型就至少能看到
    # 一部分——这样模型不需要自己重新判断分档、也不会被概率最高但质量一般
    # 的候选喂成"满屏都是专科"的错觉。上面的 simplified/candidates 字段
    # 保留不变（不少现有调用方、测试依赖它是"纯按位次差+城市偏好排序的
    # 扁平列表"这个既有语义，比如 compare_candidates 场景里随手挑两个
    # group_id），tiers 是给模型组织最终回复时用的结构化补充视图。
    curated = curate(results, city_pref=merged_profile.city_pref)
    # 空档位（比如冲刺档一个候选都没有）不放进 tiers——给模型看一个"冲刺: []"
    # 没有意义，还容易被误读成"系统查过冲刺档、但确实没有"，其实这两种情况
    # （没查/查了没有）在 total_in_category 里已经能看出来，不需要靠一个
    # 空列表的 key 存在与否来传达。
    tiers = {
        g.category_label: {
            "total_in_category": g.total_in_category,
            "candidates": [
                {
                    "group_id": c.group_id,
                    "university": c.university,
                    "city": c.city,
                    "group_name": c.group_name,
                    "rank_diff": c.diff,
                    "tuition": c.tuition,
                    "tags": c.tags,
                    "school_tier": c.school_tier,
                    # 有值时说明这条 rank_diff 是不是用"同位次跨年映射"折算过的
                    # （见 rules_engine.RankScaleContext），比如换算数据不全
                    # 时会如实说明"这条没能做跨年换算，按原始位次算的"——只在
                    # 极少数候选需要特别提醒时才非 None，不需要每条都念一遍，
                    # 但出现时应该转述给用户，不能吞掉。
                    "rank_equivalent_note": c.rank_equivalent_note,
                }
                for c in g.candidates
            ],
        }
        for g in curated.groups
        if g.candidates
    }

    note_parts = []
    if len(results) > MAX_CANDIDATES_RETURNED:
        note_parts.append(f"实际共有 {len(results)} 条候选通过硬性资格，candidates 字段只返回了前 {MAX_CANDIDATES_RETURNED} 条（按位次差排序）。")
    if curated.collapsed_count:
        note_parts.append(
            f"tiers 字段已经按保底/稳妥/冲刺三档整理、档内按学校质量优先排序（另有 {curated.collapsed_count} 条"
            "位次差过小（极冲/慎选）或算不出位次差的候选未列入 tiers，属于正常折叠，不代表出错）。"
        )
    else:
        note_parts.append("tiers 字段已经按保底/稳妥/冲刺三档整理、档内按学校质量优先排序，回复时优先按这个结构组织。")

    # discoveries："信息差挖掘"——够格985/211/双一流、但学校所在城市不是
    # 热门省会/一线城市的候选，单独挑出来（见 candidate_curation.find_discoveries
    # 的说明），因为这类候选哪怕已经在 tiers 里出现过，也容易被考生当成
    # "没听过的普通学校"直接跳过，需要额外提醒一下"这所学校其实是XX档次"。
    discoveries = [
        {
            "group_id": c.group_id,
            "university": c.university,
            "city": c.city,
            "group_name": c.group_name,
            "rank_diff": c.diff,
            "category_label": c.category_label,
            "tuition": c.tuition,
            "school_tier": c.school_tier,
        }
        for c in curated.discoveries
    ]
    if discoveries:
        note_parts.append(
            f"discoveries 字段列出了 {len(discoveries)} 所值得特别提醒的学校——"
            "都是985/211/双一流档次、但因为不在热门省会/一线城市，考生可能因为不认识校名/城市"
            "而直接忽略，回复时应该单独用一小段介绍一下（哪怕它们已经出现在 tiers 里了）。"
        )
    note = "".join(note_parts) or None
    return ToolResult(ok=True, data={
        "count": len(results), "candidates": simplified, "tiers": tiers,
        "discoveries": discoveries, "note": note,
    })


def _tool_get_candidate_detail(args: dict, ctx: ToolContext) -> ToolResult:
    group_id = args.get("group_id")
    if not group_id:
        return ToolResult(ok=False, error="缺少 group_id 参数")

    groups = _load_groups(ctx.db, group_id=group_id)
    if not groups:
        return ToolResult(
            ok=False,
            error=f"未找到 group_id={group_id}，请先调用 search_candidates 获取正确的 group_id 再查详情。",
        )

    rank_scale = build_rank_scale_context(ctx.db, ctx.profile.province, ctx.profile.track)
    detail = evaluate_group(groups[0], ctx.profile, rank_scale)
    return ToolResult(ok=True, data=detail.model_dump())


def _tool_compare_candidates(args: dict, ctx: ToolContext) -> ToolResult:
    group_ids = args.get("group_ids") or []
    if len(group_ids) < 2:
        return ToolResult(ok=False, error="compare_candidates 至少需要传入两个 group_id")

    groups = _load_groups(ctx.db)
    group_map = {g.id: g for g in groups}
    missing = [gid for gid in group_ids if gid not in group_map]
    if missing:
        return ToolResult(ok=False, error=f"以下 group_id 不存在：{missing}")

    rank_scale = build_rank_scale_context(ctx.db, ctx.profile.province, ctx.profile.track)
    rows = []
    for gid in group_ids:
        detail = evaluate_group(group_map[gid], ctx.profile, rank_scale)
        rows.append({
            "group_id": gid,
            "university": detail.university,
            "city": detail.city,
            "rank_diff": detail.diff,
            "category_label": detail.category_label,
            "tuition": detail.tuition,
            "tags": detail.tags,
        })
    return ToolResult(ok=True, data={"comparison": rows})


def _tool_web_search(args: dict, ctx: ToolContext) -> ToolResult:
    """
    真实实现：通过 MCP 客户端把请求转发给已连接的 web_search MCP 工具
    （见 app/services/mcp_client.py、默认接的是 app/mcp_servers/
    policy_search_server.py 这个离线参考实现）。这一层完全不关心背后
    到底是本地离线数据集还是真实联网搜索 API——那是 mcp_servers.json
    这份配置决定的事，跟这个工具函数无关，这正是接入 MCP 的意义所在：
    以后换真实联网搜索 server，这个函数不用改一行。
    """
    query = args.get("query")
    if not query:
        return ToolResult(ok=False, error="缺少 query 参数")

    manager = get_mcp_manager()
    if not manager.has_tool("web_search"):
        return ToolResult(
            ok=False,
            error="web_search 当前没有可用的 MCP server 连接（未配置或连接失败），"
                  "暂时无法联网查询，请基于已有的候选数据回答，或告知用户这项信息暂时查不到。",
        )

    outcome = manager.call_tool_sync("web_search", {
        "query": query,
        "recency": args.get("recency", "any"),
    })
    if not outcome.get("ok"):
        return ToolResult(ok=False, error=outcome.get("error") or "web_search 调用失败，原因未知")
    return ToolResult(ok=True, data=outcome.get("data"))


def _tool_load_skill(args: dict, ctx: ToolContext) -> ToolResult:
    """
    按需加载某个领域政策知识 Skill 的完整正文（见 app/services/skills.py）。
    模型在系统提示词里已经能看到 skill 目录（id+标题+摘要），判断这一轮
    问题涉及某个领域时自己决定要不要调用这个工具把完整内容加载进来，
    不是每次都把所有 skill 正文塞进系统提示词。
    """
    skill_id = args.get("skill_id")
    if not skill_id:
        available = "、".join(s.id for s in list_skills())
        return ToolResult(ok=False, error=f"缺少 skill_id 参数，可选值：{available}")

    try:
        content = load_skill_content(skill_id)
    except SkillNotFoundError:
        available = "、".join(s.id for s in list_skills())
        return ToolResult(ok=False, error=f"未知的 skill_id：{skill_id}，可选值：{available}")

    return ToolResult(ok=True, data={"skill_id": skill_id, "content": content})


def _tool_search_policy(args: dict, ctx: ToolContext) -> ToolResult:
    """
    在政策向量库（policy_chunks，见 app/services/policy_search.py）里做语义检索，
    返回最相关的几个原文片段，每个片段都带着来源文档标题（+ 页码，如果有）——
    这是"政策解读时能查清来源"这个目标的落地点。跟 web_search 的分工：web_search
    是走 MCP 联网查（默认接的是离线参考实现），这个工具查的是真正导入过的、
    权威的官方政策原文（省考试院发布的招生办法、专项计划通知等），两者不冲突，
    模型该用哪个由工具描述里的场景区分。

    execute_tool() 这一层是同步接口（在线程池里跑，见 react_loop.py 的
    _execute_tool_with_timeout），而 search_policy() 内部要 await 调用
    Gemini Embedding API，所以这里用 asyncio.run() 桥接——这个函数运行在
    线程池的独立线程里，不是主事件循环所在的线程，不会触发"asyncio.run()
    不能在已经运行的事件循环里调用"这个常见错误。
    """
    query = args.get("query")
    if not query:
        return ToolResult(ok=False, error="缺少 query 参数")

    top_k = args.get("top_k") or 5
    try:
        hits = asyncio.run(search_policy(ctx.db, query, top_k=top_k))
    except Exception as exc:  # noqa: BLE001 - Gemini Embedding API 调用失败时把原因喂回模型，不让整个请求崩溃
        return ToolResult(ok=False, error=f"政策检索调用失败：{exc}")

    if not hits:
        return ToolResult(ok=True, data={
            "count": 0, "results": [],
            "note": "向量库里还没有导入任何政策文档，或者没有检索到相关内容。"
                    "可以告知用户这项政策信息暂时查不到，不要凭自己的知识编造。",
        })

    return ToolResult(ok=True, data={
        "count": len(hits),
        "results": [
            {
                "document_title": h.document_title,
                "page_number": h.page_number,
                "content": h.content,
                "similarity": h.similarity,
            }
            for h in hits
        ],
    })


def _tool_finalize_recommendation(args: dict, ctx: ToolContext) -> ToolResult:
    """
    底线策略校验网关的入口——Advisor Agent 在给出最终推荐建议文字之前，
    必须调用这个工具提交完整的 group_id 名单，只有拿到 ok=True 才允许把
    这份名单当作最终建议的依据。真正的校验逻辑在 recommendation_guard.py，
    这里只是把它包装成工具，校验不通过时的原因原样喂回模型，让模型自己
    决定怎么调整名单，而不是在这一层替模型做决定。

    orchestrator.py 里还有一层服务器端强制：如果 Advisor 这一轮的执行轨迹
    里找不到一次成功的 finalize_recommendation 调用，不管模型最后的文字
    回复说了什么，都不会把它当作最终回复直接返回给用户——那一层是"物理
    上无法被绕过"的保证，这里是"给模型一个明确、可操作的重试入口"。
    """
    group_ids = args.get("group_ids")
    if not group_ids:
        return ToolResult(ok=False, error="缺少 group_ids 参数，且不能是空列表。")

    result = validate_recommendation(group_ids, ctx.profile, ctx.db)

    if not result.passed:
        lines = [f"- {v.group_id}：{v.reason}" for v in result.violations]
        return ToolResult(
            ok=False,
            error="以下候选未通过底线校验，请替换或移除这些候选后重新调用 finalize_recommendation：\n"
                  + "\n".join(lines),
        )

    return ToolResult(ok=True, data={
        "validated_group_ids": [c.group_id for c in result.candidates],
        "warnings": [f"{w.group_id or '（名单整体）'}：{w.reason}" for w in result.warnings],
    })


TOOL_REGISTRY: dict[str, Callable[[dict, ToolContext], ToolResult]] = {
    "search_candidates": _tool_search_candidates,
    "get_candidate_detail": _tool_get_candidate_detail,
    "compare_candidates": _tool_compare_candidates,
    "web_search": _tool_web_search,
    "load_skill": _tool_load_skill,
    "search_policy": _tool_search_policy,
    "finalize_recommendation": _tool_finalize_recommendation,
}

# Gemini function calling 的工具声明——第三步接入真实 API 时直接用这份，
# 现在先定义好，方便脚本化测试的场景设计跟真实场景保持一致的工具集合。
TOOL_SCHEMAS: list[dict] = [
    {
        "name": "search_candidates",
        "description": "根据考生条件检索符合资格的候选院校专业组，返回冲稳保梯度分档（保底/稳妥/冲刺/极冲，不含具体概率数字）。"
                        "当需要获取或刷新候选列表时调用。省份/科类/选科/分数/位次/学费上限/是否只看省内"
                        "这些硬性条件一律以系统已收集的考生 profile 为准，不接受在这里传参覆盖——"
                        "如果用户在对话里提到了新的硬性条件（比如改了学费上限、要求只看省内），"
                        "那些条件会被系统自动同步进 profile，你不需要、也不能在这次调用里自己传。"
                        "这里只能传 major_keyword（软匹配，换个专业关键词重新查）和 city_pref"
                        "（城市偏好，同样是软匹配、不影响硬性资格判断）——未提供的参数沿用当前已知的考生条件。",
        "parameters": {
            "type": "object",
            "properties": {
                "major_keyword": {"type": "string"},
                "city_pref": {"type": "string"},
            },
        },
    },
    {
        "name": "get_candidate_detail",
        "description": "查询某个专业组的完整信息，包括历年录取数据、规则核验明细、来源标注。"
                        "当用户对某个具体候选提出深入问题时调用。",
        "parameters": {
            "type": "object",
            "properties": {"group_id": {"type": "string"}},
            "required": ["group_id"],
        },
    },
    {
        "name": "compare_candidates",
        "description": "对比两个或多个候选专业组的差异（学费、冲稳保梯度、位次差等）。"
                        "当用户要求'比较'或需要做取舍分析时调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "group_ids": {"type": "array", "items": {"type": "string"}, "minItems": 2},
            },
            "required": ["group_ids"],
        },
    },
    {
        "name": "web_search",
        "description": "联网搜索本地数据库没有覆盖的信息：近期招生政策变化、报考趋势、就业去向。"
                        "数据库里已有的历年录取数据优先用 search_candidates / get_candidate_detail 查，"
                        "不要用联网搜索重复获取。返回结果会标注数据来源，注意甄别是否为权威最新信息。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "recency": {"type": "string", "enum": ["past_year", "past_3_years", "any"]},
            },
            "required": ["query"],
        },
    },
    {
        "name": "load_skill",
        "description": "加载某个分领域政策知识的完整说明文档（专项计划、平行志愿投档规则、"
                        "征集志愿、专业调剂等）。系统提示词里已经列出了可用的 skill 目录（id+简介），"
                        "当用户的问题涉及这类政策性概念、需要准确解释规则或权衡时调用，"
                        "不要凭自己的知识直接回答这类政策细节问题。",
        "parameters": {
            "type": "object",
            "properties": {"skill_id": {"type": "string"}},
            "required": ["skill_id"],
        },
    },
    {
        "name": "search_policy",
        "description": "在已导入的官方政策原文（省考试院发布的招生报名办法、专项计划通知、"
                        "艺术类/体育类招生办法等）里做语义检索，返回最相关的原文片段及来源文档"
                        "标题（可能带页码）。回答涉及具体政策条文、报名条件、时间节点、专项计划"
                        "资格这类问题时优先调用这个工具，用检索到的原文回答并注明来源，"
                        "不要凭自己的知识直接回答——如果检索不到相关内容，如实告知用户查不到，"
                        "不要编造。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "finalize_recommendation",
        "description": "提交这一轮要推荐给考生的完整院校专业组名单（group_id 列表），进行底线策略"
                        "校验（科类/选科/预算等硬性规则、group_id 是否真实存在、数据完整性、冲稳保"
                        "结构风险）。在给出最终推荐建议的文字回复之前必须调用这个工具，只有拿到"
                        "校验通过的结果才能把这份名单当作最终建议——校验没通过时，根据返回的具体"
                        "原因替换或移除不合格的候选后重新调用，不要绕开这个工具、直接在回复文字里"
                        "给出一份没有校验过的名单。",
        "parameters": {
            "type": "object",
            "properties": {
                "group_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            },
            "required": ["group_ids"],
        },
    },
]


def execute_tool(tool_name: str, args: dict, ctx: ToolContext) -> ToolResult:
    handler = TOOL_REGISTRY.get(tool_name)
    if handler is None:
        return ToolResult(ok=False, error=f"未知工具：{tool_name}，可用工具：{list(TOOL_REGISTRY)}")
    try:
        return handler(args, ctx)
    except Exception as exc:  # noqa: BLE001 - 工具执行的任何异常都要转成 ToolResult，不能让 ReAct 循环崩溃
        return ToolResult(ok=False, error=f"工具执行出错（{type(exc).__name__}）：{exc}")
