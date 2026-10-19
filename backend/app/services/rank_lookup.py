"""
一分一段表的查询服务：给定一个分数，查这个分数在全省对应第几名（位次）；
反过来给定一个位次，也能查大致对应多少分。这是这张表存在的真正价值——
不是只为了"把数据存起来"，而是让 Agent/前端能做"等位换算"：用今年的
一分一段表，把去年某个专业组的历史 min_rank 换算成今年对应大概什么分数，
或者用户只报了分数、没报位次时，自动推算出位次，不用用户自己去翻表。

设计上刻意不做"精确插值"——一分一段表本身是离散的（每一分一行），
用户报的分数如果在表里能直接查到，就是精确值；如果表里因为数据源本身
缺失某个分数（这种情况现实中很少见，官方一分一段表通常是连续的），
就退化到"取比这个分数低的最接近的一行"，不去凭空插值编造一个数字——
跟这个项目一路坚持的"没有数据就如实说没有，不编造"是同一个原则。
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import ScoreRankRow


@dataclass(frozen=True)
class RankLookupResult:
    score: int
    rank: int  # 累计人数，即这个分数对应的最好位次
    exact_match: bool  # True=表里精确查到这个分数；False=没有精确匹配，用的是最接近的一行
    matched_score: int  # 实际用来算这个结果的分数（exact_match=False 时跟 score 不同）


def lookup_rank_for_score(db: Session, province: str, year: int, track: str, score: int) -> RankLookupResult | None:
    """用分数查位次。表里没有这一年这个科类的数据时返回 None（如实告知
    "查不到"，不是返回一个编造的猜测值）。
    """
    exact = db.scalar(
        select(ScoreRankRow).where(
            ScoreRankRow.province == province, ScoreRankRow.year == year,
            ScoreRankRow.track == track, ScoreRankRow.score == score,
        )
    )
    if exact is not None:
        return RankLookupResult(score=score, rank=exact.cumulative_count, exact_match=True, matched_score=score)

    # 精确分数没有，退化到"小于等于这个分数里最高的一条"——因为一分一段表
    # 是按分数从高到低累计人数的，用户这个分数的真实位次不会比这一条更好。
    fallback = db.scalar(
        select(ScoreRankRow).where(
            ScoreRankRow.province == province, ScoreRankRow.year == year,
            ScoreRankRow.track == track, ScoreRankRow.score <= score,
        ).order_by(ScoreRankRow.score.desc()).limit(1)
    )
    if fallback is None:
        return None
    return RankLookupResult(
        score=score, rank=fallback.cumulative_count, exact_match=False, matched_score=fallback.score,
    )


def lookup_score_for_rank(db: Session, province: str, year: int, track: str, rank: int) -> int | None:
    """用位次反查大致对应的分数——取"累计人数刚好达到这个位次"的那一档分数。
    找不到（比如这个位次比全省总人数还大）时返回 None。
    """
    row = db.scalar(
        select(ScoreRankRow).where(
            ScoreRankRow.province == province, ScoreRankRow.year == year,
            ScoreRankRow.track == track, ScoreRankRow.cumulative_count >= rank,
        ).order_by(ScoreRankRow.cumulative_count.asc()).limit(1)
    )
    return row.score if row is not None else None


def get_total_candidates_by_year(db: Session, province: str, track: str) -> dict[int, int]:
    """给"同位次跨年映射"（rules_engine.py 的 RankScaleContext）用的底层数据：每一年一个"报考规模"数字，
    只用来算相邻年份的比例，所以**各年口径必须一致**。

    口径：优先用"本科上线人数"（该年一分一段表里批次=本科批的最大累计人数，即本科批控制线以上的人数）。
    不能直接用"表里最大累计人数"：江苏 2022~2024 年的表一直列到专科线（累计人数 = 全体考生），
    2025 年的表只列到本科批控制线（累计人数 = 本科上线人数），两者相比会把 2025 历史类误算成
    只有 2024 年的 61%，位次折算整体跑偏。本科上线人数各年都能取到，且随报考规模同向变化。

    回退：只有"每一个年份都有批次=本科批的行"才用本科上线人数；否则（表里没有批次列、旧数据没重新
    导入）所有年份一律退回"表里最大累计人数"——宁可保持旧口径，也不混用两种口径。
    重新导入一分一段表（score_rank_loader 会读取"批次"列）后自动切换到本科上线人数。

    一次查询把这个省份+科类下所有年份都取出来（GROUP BY year），不是每年单独查一次——调用方应该只查
    一次、缓存下来复用。某年份完全没有导入过一分一段表时，这年不会出现在返回的字典里，调用方需要能
    处理"这一年查不到"的情况（优雅退化成不做跨年换算）。
    """
    rows = db.execute(
        select(
            ScoreRankRow.year,
            func.max(ScoreRankRow.cumulative_count),
            func.max(case((ScoreRankRow.batch == "本科批", ScoreRankRow.cumulative_count))),
        )
        .where(ScoreRankRow.province == province, ScoreRankRow.track == track)
        .group_by(ScoreRankRow.year)
    ).all()
    if rows and all(ben is not None for _, _, ben in rows):
        return {year: ben for year, _, ben in rows}
    return {year: total for year, total, _ in rows}
