from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.db import get_db
from app.dependencies import get_current_user
from app.models import AdmissionGroup, User
from app.schemas.candidate import ProfileIn, SearchResponse
from app.services.candidate_curation import curate
from app.services.rules_engine import search_candidates

router = APIRouter(tags=["candidates"])

# 接入真实数据后单次查询能匹配到几千条候选（比如"江苏物理类"这个组合，符合
# 硬性规则的专业组本身就有大几百到上千条）——之前这里把 search_candidates
# 返回的全部结果原样塞进响应体，前端也照单全收地渲染，页面直接卡死、也没法
# 找到自己关心的那个候选。截断到一个合理的数字，配合 rules_engine.py 里新加的
# 排序（按参考概率从高到低），保证"最相关的候选排在最前面、且数量可控"。
# audit 数量上限给得比 results 松一些——它包含硬性规则未通过的候选（这些通常
# 不会太多），且资格核验面板本身就是给用户核对"为什么被排除"用的，需要的
# 覆盖面比候选卡片更大。
MAX_RESULTS_RETURNED = 60
MAX_AUDIT_RETURNED = 150


@router.post("/candidates/search", response_model=SearchResponse)
def search(
    profile: ProfileIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SearchResponse:
    """
    资格过滤 + 冲稳保打分。用 joinedload 把 school 和 records 一次性带出来，
    避免对每个专业组各发一次查询（N+1）——候选就算只有几十条，
    这里不做优化以后候选量涨起来也会立刻变慢。

    这个接口本身不读写任何"属于某个用户"的数据（profile 是请求体直接传入的，
    不是按 id 查库），但仍然要求登录——不然任何人都能绕开账号体系直接扫库里
    的全部院校数据，跟"认证覆盖所有接口"这条安全要求冲突。current_user 参数
    没有在函数体里用到，只是为了触发这层校验。
    """
    groups = db.scalars(
        select(AdmissionGroup).options(
            joinedload(AdmissionGroup.school),
            joinedload(AdmissionGroup.records),
        )
    ).unique().all()

    results, audit = search_candidates(list(groups), profile, db=db)
    total_results, total_audit = len(results), len(audit)
    # curate() 是"好学校优先展示"精选逻辑（app/services/candidate_curation.py）：
    # 从完整的 results（未截断，用真实的全量候选算比例分配才准）里挑出一份
    # 保底/稳妥/冲刺三档、按学校质量优先排序的精选名单，其余候选折叠。用
    # 截断前的 results 而不是 results[:MAX_RESULTS_RETURNED]，避免三档比例
    # 分配被上面这个跟"好学校优先"无关的截断上限意外影响。
    curated = curate(results, city_pref=profile.city_pref)
    return SearchResponse(
        results=results[:MAX_RESULTS_RETURNED],
        audit=audit[:MAX_AUDIT_RETURNED],
        total_results=total_results,
        total_audit=total_audit,
        curated=curated,
    )
