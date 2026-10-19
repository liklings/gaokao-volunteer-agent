from sqlalchemy import Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ScoreRankRow(Base):
    """
    一分一段表：全省维度的"某年某科类某分数，对应多少人考了这个分、
    累计排到第几名"，不属于任何一个具体院校/专业组，这是它跟
    AdmissionRecord（某专业组某年的录取数据）本质的区别——
    AdmissionRecord 回答"这个专业组去年录取到多少位次"，
    这张表回答"某个分数在全省是什么位次"，两者配合起来才能做
    "用今年的分数在一分一段表里查位次，再用这个位次去比历年专业组的
    min_rank"这套完整的换算，也是"等位分"技术的数据基础——
    今年的一分一段表能把去年某个位次换算成今年对应的分数，比直接拿
    历年分数线比大小准确得多（不同年份题目难度不同，分数不能直接比，
    位次才是相对稳定的参照系）。

    (province, year, track, score) 唯一——同一省份同一年同一科类的
    同一个分数，只应该有一行。
    """
    __tablename__ = "score_rank_rows"
    __table_args__ = (UniqueConstraint("province", "year", "track", "score", name="uq_province_year_track_score"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    province: Mapped[str] = mapped_column(String(16), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    track: Mapped[str] = mapped_column(String(16), nullable=False)  # 物理类 / 历史类
    score: Mapped[int] = mapped_column(Integer, nullable=False)

    count_at_score: Mapped[int] = mapped_column(Integer, nullable=False)  # 恰好考了这个分数的人数（本段人数）
    cumulative_count: Mapped[int] = mapped_column(Integer, nullable=False)  # 大于等于这个分数的累计人数（即该分数的最好位次）

    # 该分数所属批次（"本科批"/"专科批"，来自官方表的"批次"列；表里没有这一列时为 NULL）。
    # 存它是因为各年官方表的统计范围不一样：2022~2024 年的表一直列到专科线，2025 年只列到本科批控制线——
    # 直接拿"表里最大累计人数"当总人数，前后年份口径不同（见 rank_lookup.get_total_candidates_by_year）。
    batch: Mapped[str | None] = mapped_column(String(16))

    source_url: Mapped[str | None] = mapped_column(String(500))
