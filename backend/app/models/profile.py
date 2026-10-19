from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Profile(Base):
    """
    一次对话收集到的考生条件。user_id 可空——
    这次里程碑不做登录，所以匿名会话也能产生 Profile，
    以后接了账号体系，登录用户的 Profile 才会带上真实 user_id。
    """
    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    province: Mapped[str | None] = mapped_column(String(16))
    year: Mapped[int | None] = mapped_column(Integer)
    track: Mapped[str | None] = mapped_column(String(16))
    subjects: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    score: Mapped[int | None] = mapped_column(Integer)
    rank: Mapped[int | None] = mapped_column(Integer)
    major_keyword: Mapped[str | None] = mapped_column(String(64))
    city_pref: Mapped[str | None] = mapped_column(String(64))
    budget_max: Mapped[int | None] = mapped_column(Integer)
    home_province_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # 只看"学校实际物理所在省份 == province"的候选——跟 ProfileIn 里的同名
    # 字段是同一个意思（见 app/schemas/candidate.py 的注释），这里补上持久化
    # 存储，是这次把这个开关接进 Profile Agent 聊天采集流程时新加的：之前
    # 这个开关只存在于前端"查询候选"按钮直连接口的单次请求里，聊天里没法用，
    # 现在要跟 city_pref/budget_max 一样，用户说一次、后续轮次和聊天工具
    # 调用（tools.py 的 search_candidates）都记得住，就必须落库。
    # server_default="false" 是为了老数据库升级时，已有的历史 Profile 行
    # 不会因为加了这个 NOT NULL 列而报错——迁移脚本里会给存量行统一补 false。

    accept_sino_foreign: Mapped[bool | None] = mapped_column(Boolean)
    accept_special_program: Mapped[bool | None] = mapped_column(Boolean)
    # "多轮槽位填充表单化"新增——跟上面 home_province_only 不一样，这两个
    # 字段刻意用可空的三态（None/True/False），不是 NOT NULL 默认 False：
    # None 表示"还没问过考生/考生还没明确表态"，rules_engine.py 那边会把
    # None 按"没有明确同意"处理（效果等同 False，默认排除），但 None 和
    # False 在这里语义不同——False 是"问过、考生明确表示不接受"，这个
    # 区分是给前端/Profile Agent 判断"要不要主动追问一次"用的（None 才
    # 需要问，已经是 False 的不用重复问）。见
    # app/schemas/candidate.py ProfileIn 里同名字段的详细说明。

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    plans: Mapped[list["Plan"]] = relationship(back_populates="profile", cascade="all, delete-orphan")
    chat_messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan", order_by="ChatMessage.created_at"
    )
