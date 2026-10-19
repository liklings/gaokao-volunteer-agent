from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.dependencies import get_current_user
from app.models import Profile, User
from app.schemas.chat import ChatRequest, ChatResponse, ProfileOut
from app.services.gemini_errors import GeminiCallError, GeminiConfigError
from app.services.limits import RateLimitExceededError
from app.services.orchestrator import run_orchestrator

router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ChatResponse:
    """
    这个接口原来只直接调用 Profile Agent（call_agent），条件收集齐了之后
    也不会往下走——Orchestrator 已经在测试脚本里跑通了 profile/retrieval/
    advisor 三个分支，但一直没有真正接到对外的 HTTP 接口上，导致这个接口
    实际上永远走不到检索和建议那两步。这里改成调用 run_orchestrator，
    让它自己根据 profile 当前状态和这句话的内容决定该走哪个 Agent。

    用户消息的落库（ChatMessage）现在下沉到 orchestrator.py 里每个
    _run_xxx_step 函数内部处理（在调用 Gemini 之前就提交），道理跟这里
    原来"先落库用户消息、即便 Gemini 调用失败对话也不会丢"一致，只是
    Profile/Retrieval/Advisor 三条分支都要有这个保护，所以挪到了更上游的
    地方统一做，而不是只有这个接口有这层保护、其他调用方（比如以后如果
    加别的入口）反而没有。
    """
    if req.profile_id is not None:
        profile = db.get(Profile, req.profile_id)
        # 找不到、或者存在但属于别的用户，统一返回同一种 404——不区分"不存在"和
        # "存在但不是你的"，避免暴露"这个 profile_id 确实有人在用"这种信息。
        if profile is None or profile.user_id != current_user.id:
            raise HTTPException(status_code=404, detail=f"未找到 profile_id={req.profile_id}")
    else:
        profile = Profile(user_id=current_user.id)
        db.add(profile)
        db.flush()  # 拿到自增 id，不用等 commit

    try:
        result = await run_orchestrator(db, profile, req.message)
    except RateLimitExceededError as exc:
        # 限流检查在 run_orchestrator 一开始、任何一步真正调用 Gemini 之前就
        # 发生，这里除了刚创建的 profile 行（如果是新会话）不会有其他待落库
        # 的内容，同样 commit 一下保留住，不因为这次被拦下就丢掉新建的 profile。
        db.commit()
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except GeminiConfigError as exc:
        db.commit()  # 就算这一步失败，前面已经落库的内容（比如新建的 profile 行）也不因此丢
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except GeminiCallError as exc:
        db.commit()
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    db.refresh(profile)

    return ChatResponse(
        profile_id=result.profile_id,
        reply=result.reply,
        profile=ProfileOut.model_validate(profile),
        missing=result.missing,
        # conflicts 目前只有 Profile Agent 那一步会产出（模型自己识别出的条件矛盾，
        # 比如同一轮里既说物理类又说历史类），retrieval/advisor 分支不涉及这个概念，
        # 固定给空列表，不是遗漏。
        conflicts=[],
        ready=result.ready,
    )
