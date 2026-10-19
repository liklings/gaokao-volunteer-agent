"""
底线策略校验网关：Advisor Agent 给出最终推荐名单之前，必须经过这里的纯代码
校验，不采信模型自报"这几个学校都合适"——这是这个项目从一开始就坚持的
原则（missing/ready 由服务器计算、search_candidates 只返回真正通过硬性
规则的候选……）在"最终推荐结果"这一层的延伸，也是最后一道防线：前面
search_candidates/get_candidate_detail 工具返回的数据已经是真实数据，
但 Advisor 是不是真的只从里面挑、挑出来的这几个是不是真的同时满足硬性
规则、组合起来是不是有明显的滑档风险——这些只有在"模型说要推荐哪几个"
这个动作发生之后才能校验，没法靠"喂给它的数据是真的"来保证。

设计上刻意区分"违规"（violations，硬性规则不通过，直接判定校验不通过，
必须整改重提）和"警告"（warnings，数据不完整/结构风险这类不构成"一定
错误"、但应该让用户知道的情况，校验仍然算通过，但警告文字要透传给用户，
不能被模型悄悄吞掉）——跟 rules_engine.py 里"硬性规则 vs 学费数据缺失时
不算硬性排除但要明确告知"是同一种分层思路。

这一层只依赖已经在 rules_engine.py 里验证过的 evaluate_group()，不重新
发明一套判断逻辑——校验一个候选合不合格，跟"资格过滤"用的是同一套规则，
两处结果不可能对不上。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models import AdmissionGroup
from app.schemas.candidate import CandidateOut, ProfileIn
from app.services.rules_engine import build_rank_scale_context, evaluate_group

# 一份推荐名单里，達到这个数量以上时才检查"冲稳保结构"——名单只有一两个
# 候选时，本来就没有排布梯度的空间，不该因为"只推荐了一个保底"就判定校验
# 不通过。
MIN_LIST_SIZE_FOR_STRUCTURE_CHECK = 3

# 名单最多允许多少个候选——不是"不能推荐这么多"，是防止模型把
# search_candidates 返回的全部结果不加筛选地整个塞进 finalize_recommendation，
# 那样跟没做取舍是一回事，违背 Advisor Agent 本该做"取舍"这个职责。
# 定得比 tools.py 里 MAX_CANDIDATES_RETURNED（15）更小，确保"塞进全部
# 检索结果"这种偷懒行为一定会撞到这道限制。
MAX_LIST_SIZE = 8


@dataclass(frozen=True)
class RecommendationViolation:
    """硬性规则不通过——校验判定为不通过，必须整改（替换/移除该候选）后重提。"""
    group_id: str
    reason: str


@dataclass(frozen=True)
class RecommendationWarning:
    """不构成校验不通过，但必须让用户看到的提醒（数据不完整、结构性风险等）。"""
    group_id: str | None
    reason: str


@dataclass
class GuardResult:
    passed: bool
    violations: list[RecommendationViolation] = field(default_factory=list)
    warnings: list[RecommendationWarning] = field(default_factory=list)
    candidates: list[CandidateOut] = field(default_factory=list)


def _load_groups_by_ids(db: Session, group_ids: list[str]) -> dict[str, AdmissionGroup]:
    stmt = select(AdmissionGroup).options(
        joinedload(AdmissionGroup.school),
        joinedload(AdmissionGroup.records),
    ).where(AdmissionGroup.id.in_(group_ids))
    groups = db.scalars(stmt).unique().all()
    return {g.id: g for g in groups}


def validate_recommendation(group_ids: list[str], profile: ProfileIn, db: Session) -> GuardResult:
    """
    对一份"打算推荐给考生"的 group_id 列表做底线校验。纯粹依赖数据库里的
    真实数据和 rules_engine 的既有判断，不读取、不依赖模型给出的任何文字
    说明——校验的是"这份名单本身站不站得住"，不是"模型的理由讲得好不好"。
    """
    violations: list[RecommendationViolation] = []
    warnings: list[RecommendationWarning] = []

    if not group_ids:
        return GuardResult(passed=False, violations=[
            RecommendationViolation(group_id="-", reason="推荐名单不能为空，至少要包含一个候选。"),
        ])

    if len(group_ids) > MAX_LIST_SIZE:
        violations.append(RecommendationViolation(
            group_id="-",
            reason=f"一次最多推荐 {MAX_LIST_SIZE} 个候选，实际提交了 {len(group_ids)} 个，"
                   f"请做真正的取舍、只保留最推荐的几个，不要把检索结果整批提交。",
        ))

    seen: set[str] = set()
    duplicates: set[str] = set()
    for gid in group_ids:
        if gid in seen:
            duplicates.add(gid)
        seen.add(gid)
    for gid in duplicates:
        violations.append(RecommendationViolation(group_id=gid, reason="同一个 group_id 在名单里重复出现。"))

    unique_ids = list(dict.fromkeys(group_ids))  # 去重但保持顺序，方便后续按顺序展示
    group_map = _load_groups_by_ids(db, unique_ids)

    missing_ids = [gid for gid in unique_ids if gid not in group_map]
    for gid in missing_ids:
        violations.append(RecommendationViolation(group_id=gid, reason="不存在这个 group_id，数据库里查不到对应的专业组。"))

    rank_scale = build_rank_scale_context(db, profile.province, profile.track)
    candidates: list[CandidateOut] = []
    for gid in unique_ids:
        group = group_map.get(gid)
        if group is None:
            continue
        candidate = evaluate_group(group, profile, rank_scale)
        candidates.append(candidate)

        if not candidate.all_hard_pass:
            failed_rules = [r.desc for r in candidate.rules if not r.passed]
            violations.append(RecommendationViolation(
                group_id=gid,
                reason=f"未通过硬性规则：{'；'.join(failed_rules) if failed_rules else '资格校验不通过'}",
            ))

        if candidate.category is None:
            warnings.append(RecommendationWarning(
                group_id=gid,
                reason="该候选没有可参考的历年录取位次数据（可能是新增专业组，或只有招生计划、"
                       "还没有当年录取结果），无法计算冲稳保概率，回复里应明确告知考生这一点，"
                       "不能当成有把握的候选来描述。",
            ))

    # 结构性风险检查：候选数够多时，如果全部集中在"冲刺"及以上风险等级、
    # 没有一个"稳妥"或"保底"，滑档（全部落空）的风险明显偏高。
    if len(candidates) >= MIN_LIST_SIZE_FOR_STRUCTURE_CHECK:
        categories = [c.category for c in candidates if c.category is not None]
        if categories and all(cat in ("chong", "jichong") for cat in categories):
            warnings.append(RecommendationWarning(
                group_id=None,
                reason="这份名单里所有能计算出概率的候选都是'冲刺'或更高风险等级，没有'稳妥'或'保底'"
                       "选项，同时落空的风险偏高，建议至少保留一个把握更大的候选做兜底。",
            ))

    passed = len(violations) == 0
    return GuardResult(passed=passed, violations=violations, warnings=warnings, candidates=candidates)
