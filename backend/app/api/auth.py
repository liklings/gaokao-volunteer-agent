from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.dependencies import get_current_user
from app.models import User
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse, UserOut
from app.services.security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=TokenResponse, status_code=201)
def register(req: RegisterRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """
    注册即登录：注册成功直接签发 token 返回，不用再让用户多走一次登录——
    这是绝大多数产品的默认体验，没有理由在这里多加一步摩擦。

    邮箱唯一性这里先查一次再插入（提前给出清楚的错误提示），插入时
    再兜底捕获 IntegrityError——两次请求并发注册同一个邮箱这种边界情况，
    第一次查询查不出冲突，但唯一约束仍然会在数据库层面兜住，不会出现
    两条 email 相同的用户记录。
    """
    existing = db.scalar(select(User).where(User.email == req.email))
    if existing is not None:
        raise HTTPException(status_code=409, detail="该邮箱已被注册")

    user = User(email=req.email, password_hash=hash_password(req.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该邮箱已被注册") from None
    db.refresh(user)

    token = create_access_token(user.id)
    return TokenResponse(access_token=token, user=UserOut.model_validate(user))


@router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.scalar(select(User).where(User.email == req.email))
    # 故意不区分"邮箱不存在"和"密码错误"这两种情况，用同一句错误提示——
    # 区分开会让攻击者可以拿来批量探测哪些邮箱已经注册过（用户枚举攻击）。
    if user is None or not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="邮箱或密码不正确")

    token = create_access_token(user.id)
    return TokenResponse(access_token=token, user=UserOut.model_validate(user))


@router.get("/me", response_model=UserOut)
def get_me(current_user: User = Depends(get_current_user)) -> UserOut:
    return UserOut.model_validate(current_user)
