"""
跨接口复用的 FastAPI 依赖。核心是从 Bearer token 解出当前登录用户，
任何要求登录的接口都用 `current_user: User = Depends(get_current_user)` 挂上这一层。

放在单独的模块（而不是塞进某个 api/*.py 里）是因为 chat.py / plans.py /
candidates.py / auth.py 都要用到它，避免互相 import 造成循环依赖。
"""
from fastapi import Depends, HTTPException, Query
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.services.security import InvalidTokenError, decode_access_token

# tokenUrl 只是给 /docs 里的"Authorize"按钮用来知道去哪个接口换 token，
# 不影响这个依赖本身的校验逻辑（校验逻辑是纯粹解析 Authorization 头里的 Bearer token）。
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


def _resolve_user(token: str | None, db: Session) -> User:
    if token is None:
        raise HTTPException(status_code=401, detail="未登录，请先登录后再操作", headers={"WWW-Authenticate": "Bearer"})
    try:
        user_id = decode_access_token(token)
    except InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc), headers={"WWW-Authenticate": "Bearer"}) from exc

    user = db.get(User, user_id)
    if user is None:
        # token 本身签名合法，但对应的用户已经不存在了（比如账号被删除）——
        # 同样按未认证处理，不能让一个"技术上有效但指向空用户"的 token 蒙混过关。
        raise HTTPException(status_code=401, detail="用户不存在，请重新登录", headers={"WWW-Authenticate": "Bearer"})
    return user


def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    return _resolve_user(token, db)


def get_current_user_allow_query_token(
    header_token: str | None = Depends(oauth2_scheme),
    query_token: str | None = Query(None, alias="token"),
    db: Session = Depends(get_db),
) -> User:
    """
    跟 get_current_user 逻辑完全一样，唯一区别是 token 除了能从 Authorization
    头拿，也能从 ?token= 这个查询参数拿。专门给 /api/plans/{id}/export 这一个
    接口用——前端是通过浏览器 <a href target=_blank> 直接打开这个链接来"下载"，
    不是走 fetch，没法附带自定义请求头，只能退而求其次放在 URL 上。

    这个折中是有安全代价的：URL（包括查询参数）比请求头更容易被记进浏览器历史、
    服务器访问日志、代理日志里。只在这一个只读、非破坏性、专门为"直接在浏览器里
    打开/下载"设计的导出接口上开这个口子，不作为通用认证方式用在其他任何接口。
    """
    return _resolve_user(header_token or query_token, db)
