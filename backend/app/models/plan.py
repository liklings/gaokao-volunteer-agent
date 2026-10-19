from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Plan(Base):
    """
    某个 Profile 下确认过的一个方案版本。version_n 从1开始递增——
    每次用户改条件重新确认，就是同一个 profile 下的新版本，
    这样"较上一版本"的对比才有据可查，而不是每次都新建一套无关记录。
    """
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id"), nullable=False)
    version_n: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    profile: Mapped["Profile"] = relationship(back_populates="plans")
    candidates: Mapped[list["PlanCandidate"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan"
    )
