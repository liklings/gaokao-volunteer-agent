from datetime import date

from sqlalchemy import Date, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class AdmissionRecord(Base):
    """
    一个专业组在某一年的真实录取数据。这是"资格校验"和"冲稳保"打分
    真正依赖的数字，每条都带 source_url/fetched_at，方便追溯来源。
    (group_id, year) 唯一，同一年不该有两条记录。
    """
    __tablename__ = "admission_records"
    __table_args__ = (UniqueConstraint("group_id", "year", name="uq_group_year"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(ForeignKey("admission_groups.id"), nullable=False)

    year: Mapped[int] = mapped_column(Integer, nullable=False)
    group_name: Mapped[str | None] = mapped_column(String(64))  # 当年官方专业组名称，逐年可能变
    plan_count: Mapped[int | None] = mapped_column(Integer)
    min_score: Mapped[int | None] = mapped_column(Integer)
    min_rank: Mapped[int | None] = mapped_column(Integer)

    source_url: Mapped[str | None] = mapped_column(String(500))  # 允许为空，前端显示"来源链接待补充"
    fetched_at: Mapped[date | None] = mapped_column(Date)

    group: Mapped["AdmissionGroup"] = relationship(back_populates="records")
