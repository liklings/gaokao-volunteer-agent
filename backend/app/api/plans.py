from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.db import get_db
from app.dependencies import get_current_user, get_current_user_allow_query_token
from app.models import AdmissionGroup, Plan, PlanCandidate, Profile, User
from app.schemas.plan import DiffEntry, PlanCandidateOut, PlanConfirmRequest, PlanOut
from app.services.plan_service import build_export_text, compute_diff
from app.services.rules_engine import build_rank_scale_context, evaluate_group

router = APIRouter(tags=["plans"])


def _get_owned_profile(db: Session, profile_id: int, current_user: User) -> Profile:
    """查 profile 并顺带做归属校验——这个函数被 confirm_plan 直接用，
    get_plan/export_plan 则是先查到 plan 再反查 plan.profile_id 复用这同一个函数，
    确保"这份方案是不是当前用户的"这条校验逻辑只写一处。
    """
    profile = db.get(Profile, profile_id)
    if profile is None or profile.user_id != current_user.id:
        raise HTTPException(status_code=404, detail=f"未找到 profile_id={profile_id}")
    return profile


def _latest_plan_for_profile(db: Session, profile_id: int, before_version: int | None = None) -> Plan | None:
    stmt = select(Plan).where(Plan.profile_id == profile_id)
    if before_version is not None:
        stmt = stmt.where(Plan.version_n < before_version)
    stmt = stmt.order_by(Plan.version_n.desc())
    return db.scalars(stmt).first()


def _to_plan_out(db: Session, plan: Plan, diff: list[dict]) -> PlanOut:
    candidates = db.scalars(
        select(PlanCandidate).where(PlanCandidate.plan_id == plan.id)
    ).all()
    cand_out = []
    for c in candidates:
        snap = c.snapshot_json or {}
        cand_out.append(PlanCandidateOut(
            group_id=c.group_id,
            university=snap.get("university", c.group_id),
            city=snap.get("city", ""),
            group_name=snap.get("group_name"),
            code=snap.get("code"),
            tuition=snap.get("tuition"),
            rank_diff=c.rank_diff,
            category=snap.get("category"),
            category_label=snap.get("category_label"),
            all_hard_pass=snap.get("all_hard_pass", False),
        ))
    return PlanOut(
        id=plan.id, profile_id=plan.profile_id, version_n=plan.version_n, created_at=plan.created_at,
        candidates=cand_out,
        diff=[DiffEntry(**d) for d in diff],
    )


@router.post("/plans", response_model=PlanOut)
def confirm_plan(
    req: PlanConfirmRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PlanOut:
    """
    确认方案：把用户选中的候选连同【服务器重新算出来的】rank_diff 一起
    存成新版本。刻意不接受客户端传来的位次差数字——这个值必须由后端用当前
    考生条件和数据库里的真实数据重新计算，否则任何人都能在浏览器里伪造一个
    好看的数字存进方案记录。
    """
    profile = _get_owned_profile(db, req.profile_id, current_user)

    if not req.group_ids:
        raise HTTPException(status_code=400, detail="group_ids 不能为空——方案至少要包含一个候选。")

    groups = db.scalars(
        select(AdmissionGroup)
        .where(AdmissionGroup.id.in_(req.group_ids))
        .options(joinedload(AdmissionGroup.school), joinedload(AdmissionGroup.records))
    ).unique().all()

    found_ids = {g.id for g in groups}
    missing_ids = set(req.group_ids) - found_ids
    if missing_ids:
        raise HTTPException(status_code=404, detail=f"以下 group_id 不存在：{sorted(missing_ids)}")

    prev_plan = _latest_plan_for_profile(db, profile.id)
    version_n = (prev_plan.version_n + 1) if prev_plan else 1

    plan = Plan(profile_id=profile.id, version_n=version_n)
    db.add(plan)
    db.flush()  # 拿到 plan.id

    rank_scale = build_rank_scale_context(db, profile.province, profile.track)
    new_candidates: list[PlanCandidate] = []
    for group in groups:
        evaluated = evaluate_group(group, profile, rank_scale)  # 服务器重新计算，不信任客户端
        pc = PlanCandidate(
            plan_id=plan.id,
            group_id=group.id,
            rank_diff=evaluated.diff,
            strategy_type=None,
            snapshot_json=evaluated.model_dump(),
        )
        db.add(pc)
        new_candidates.append(pc)

    prev_candidates: list[PlanCandidate] = []
    if prev_plan:
        prev_candidates = list(db.scalars(
            select(PlanCandidate).where(PlanCandidate.plan_id == prev_plan.id)
        ).all())

    # 第一版没有"上一版"可比，直接给空 diff，而不是把每个候选都标成"新增"——
    # 那样对用户没有信息量，只是重复了candidates列表本身。
    diff = compute_diff(prev_candidates, new_candidates) if prev_plan else []

    db.commit()
    db.refresh(plan)

    return _to_plan_out(db, plan, diff)


@router.get("/plans/{plan_id}", response_model=PlanOut)
def get_plan(
    plan_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PlanOut:
    plan = db.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"未找到 plan_id={plan_id}")
    _get_owned_profile(db, plan.profile_id, current_user)  # 归属校验：不是自己的方案，一律当不存在

    prev_plan = _latest_plan_for_profile(db, plan.profile_id, before_version=plan.version_n)
    prev_candidates: list[PlanCandidate] = []
    if prev_plan:
        prev_candidates = list(db.scalars(
            select(PlanCandidate).where(PlanCandidate.plan_id == prev_plan.id)
        ).all())
    curr_candidates = list(db.scalars(
        select(PlanCandidate).where(PlanCandidate.plan_id == plan.id)
    ).all())
    diff = compute_diff(prev_candidates, curr_candidates) if prev_plan else []

    return _to_plan_out(db, plan, diff)


@router.get("/plans/{plan_id}/export", response_class=PlainTextResponse)
def export_plan(
    plan_id: int,
    db: Session = Depends(get_db),
    # 这一个接口允许 token 从 ?token= 查询参数传（而不是只认 Authorization 头）——
    # 前端是用 <a href target=_blank> 直接打开这个链接下载/预览，不是走 fetch，
    # 没法带自定义请求头。详见 get_current_user_allow_query_token 的说明。
    current_user: User = Depends(get_current_user_allow_query_token),
) -> str:
    plan = db.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"未找到 plan_id={plan_id}")
    profile = _get_owned_profile(db, plan.profile_id, current_user)  # 归属校验，顺便拿到 profile 供导出用
    candidates = list(db.scalars(
        select(PlanCandidate).where(PlanCandidate.plan_id == plan.id)
    ).all())
    return build_export_text(profile, plan, candidates)
