from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class PlanCandidate(Base):
    """
    某个方案版本里包含的一条候选，连同当时算出来的 rank_diff 一起存成快照。
    这样即便种子数据以后更新了，历史版本对比时看到的还是"当时"的数字，
    不会因为数据源变化而让旧版本的方案对比失真。
    snapshot_json 存一份完整的候选卡渲染用数据（学费/标签/为什么推荐等），
    前端读取历史版本时不用再回头拼装。

    之前这里还有一个 prob_pct（参考概率百分比）字段，跟 CandidateOut.pct
    一起被去掉了——用户反馈这个百分比数字是拍脑袋校准出来的线性公式硬算
    出来的，容易被误当成有统计学依据，改成冲稳保梯度直接按位次差占比
    分档，不再经过百分比这一步（见 app/services/rules_engine.py 的说明）。
    """
    __tablename__ = "plan_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"), nullable=False)
    group_id: Mapped[str] = mapped_column(ForeignKey("admission_groups.id"), nullable=False)

    rank_diff: Mapped[int | None] = mapped_column(Integer)
    strategy_type: Mapped[str | None] = mapped_column(String(16))  # 院校优先 / 专业优先，当时的分类
    snapshot_json: Mapped[dict | None] = mapped_column(JSONB)

    plan: Mapped["Plan"] = relationship(back_populates="candidates")
