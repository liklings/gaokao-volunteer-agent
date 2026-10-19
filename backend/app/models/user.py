from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class User(Base):
    """
    账号体系（方向⑤）：邮箱+密码登录，签发 JWT，profiles.user_id 外键指向这里，
    各接口按 current_user 隔离各自的数据。email 从"可空"改成必填+唯一——
    这张表之前只是占位，从没写过真实数据，现在补约束不影响任何存量数据。
    """
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
