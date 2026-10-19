from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class AgentRun(Base):
    """
    一次完整的 Agent 执行过程（对应 react_loop.py 跑完一次 run_react_loop 产生的
    RunTrace）。这次里程碑不做前端展示界面（之前拍板过 3.4 不展示），这两张表
    纯粹是后端内部排查用的：出问题时能查"这次到底调用了几次工具、每步结果是什么、
    token花了多少"，不用再靠肉眼盯着终端日志滚屏去猜。
    """
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id"), nullable=False)
    agent_name: Mapped[str] = mapped_column(String(32), nullable=False)

    # 跟 app/services/logging_config.py 生成的 run_id 是同一个值——这样出问题时
    # 能直接拿日志里 grep 到的 run_id 去数据库里查这次执行的完整 trace（每一步
    # 调用了什么工具、结果是什么），不用凭时间戳去猜是不是同一次请求。
    # 允许为空是因为这张表在加这个字段之前就有数据，旧记录没有 run_id。
    run_id: Mapped[str | None] = mapped_column(String(32))

    status: Mapped[str] = mapped_column(String(16), nullable=False)  # running | completed | aborted
    abort_reason: Mapped[str | None] = mapped_column(String(64))
    total_steps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    steps: Mapped[list["AgentStep"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="AgentStep.step_index"
    )
