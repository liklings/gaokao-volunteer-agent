"""
密码哈希 + JWT 签发/校验。这两件事故意放在同一个文件里而不是分开：
它们一起构成"认证"这一层，被 app/api/auth.py（登录/注册）和
app/dependencies.py（保护接口时解出当前用户）两处调用。

设计取舍：
- 密码哈希用 bcrypt 直接调用（不用 passlib）——passlib 最近几个版本
  跟新版 bcrypt 库的兼容性一直有问题（`__about__` 属性缺失之类），
  bcrypt 库本身的 API 已经很简单，没必要多一层依赖。
- JWT 用 PyJWT，只放最少信息在 payload 里（用户 id + 过期时间），
  不把 email 这类可能变化的信息编进 token——查用户信息时应该实时查库，
  而不是相信一个可能已经过期的"缓存"。
"""
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.config import get_settings


def hash_password(plain_password: str) -> str:
    """bcrypt 自带加盐，同一个密码每次哈希出来的结果都不一样，这是预期行为。"""
    hashed = bcrypt.hashpw(plain_password.encode("utf-8"), bcrypt.gensalt())
    return hashed.decode("utf-8")


def verify_password(plain_password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        # password_hash 格式不对（比如数据库里存了脏数据）时 bcrypt 会抛异常，
        # 这种情况应该当成"校验失败"而不是让整个请求 500。
        return False


def create_access_token(user_id: int) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


class InvalidTokenError(Exception):
    """token 缺失/格式错/签名不对/过期，统一归一成这一种异常，
    调用方（get_current_user 依赖）只需要处理这一种情况就能返回 401。"""


def decode_access_token(token: str) -> int:
    """校验并解出 token 里的 user_id。任何校验失败的原因（过期、签名不对、
    payload 里没有 sub 字段）都统一抛 InvalidTokenError，不区分给调用方，
    避免给客户端泄露"是过期了还是签名被篡改了"这种对攻击者有用的细节。
    """
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise InvalidTokenError("token 无效或已过期") from exc

    sub = payload.get("sub")
    if sub is None:
        raise InvalidTokenError("token 缺少必要字段")
    try:
        return int(sub)
    except (TypeError, ValueError) as exc:
        raise InvalidTokenError("token 内容格式不对") from exc
