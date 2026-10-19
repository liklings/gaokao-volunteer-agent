"""
一分一段表查询服务的测试：精确匹配、分数缺失时退化到最接近的一档、
反向用位次查分数、查不到时如实返回 None（不编造）。
"""
from app.services.rank_lookup import get_total_candidates_by_year, lookup_rank_for_score, lookup_score_for_rank
from app.services.score_rank_loader import load_score_rank_dataframe

import pandas as pd

PROVINCE, YEAR, TRACK = "测试省2", 8888, "物理类"


def seed(db):
    df = pd.DataFrame(
        [[680, 100, 400], [679, 120, 520], [677, 150, 670]],  # 678 分故意缺失，测退化匹配
        columns=["分数", "本段人数", "累计人数"],
    )
    load_score_rank_dataframe(db, df, PROVINCE, YEAR, TRACK)
    db.commit()


def test_exact_match(db):
    seed(db)
    result = lookup_rank_for_score(db, PROVINCE, YEAR, TRACK, 679)
    assert result is not None
    assert result.exact_match is True
    assert result.rank == 520


def test_fallback_to_nearest_lower_score(db):
    seed(db)
    result = lookup_rank_for_score(db, PROVINCE, YEAR, TRACK, 678)  # 678分表里没有
    assert result is not None
    assert result.exact_match is False
    assert result.matched_score == 677
    assert result.rank == 670


def test_score_not_found_returns_none_not_guessed_value(db):
    seed(db)
    result = lookup_rank_for_score(db, PROVINCE, YEAR, TRACK, 100)  # 低于表里最低分
    assert result is None


def test_lookup_score_for_rank(db):
    seed(db)
    score = lookup_score_for_rank(db, PROVINCE, YEAR, TRACK, 500)
    assert score == 679  # 累计到679分才第一次覆盖到位次500


def test_lookup_score_for_rank_not_found(db):
    seed(db)
    score = lookup_score_for_rank(db, PROVINCE, YEAR, TRACK, 99999999)
    assert score is None


def test_get_total_candidates_by_year_takes_max_cumulative_count_per_year(db):
    """给"同位次跨年映射"用的总人数近似值——取每一年 cumulative_count 的
    最大值（最低分那一档的累计人数）。这里用两个不同年份的数据验证按年份
    分组正确，且没导入过的年份不会凭空出现在结果里。"""
    seed(db)  # 8888 年，最大 cumulative_count = 670
    df_9999 = pd.DataFrame(
        [[690, 80, 200], [685, 300, 500], [680, 400, 900]],
        columns=["分数", "本段人数", "累计人数"],
    )
    load_score_rank_dataframe(db, df_9999, PROVINCE, 9999, TRACK)
    db.commit()

    totals = get_total_candidates_by_year(db, PROVINCE, TRACK)
    assert totals == {8888: 670, 9999: 900}


def test_get_total_candidates_by_year_missing_returns_empty_dict(db):
    totals = get_total_candidates_by_year(db, "从未导入过的省份", TRACK)
    assert totals == {}


# ---- 口径：本科上线人数（各年官方表统计范围不一致）----

def _df_with_batch(rows):
    return pd.DataFrame(rows, columns=["批次", "分数", "本段人数", "累计人数"])


def test_totals_use_ben_ke_count_so_years_with_different_table_coverage_are_comparable(db):
    """复刻真实情况：2022~2024 的表一直列到专科线，2025 的表只列到本科批控制线。
    直接取表内最大累计人数会得到 2024=92399、2025=56398（比例 0.61，错）；
    用本科上线人数则是 50966 / 56398（比例 1.11，对）。"""
    prov = "口径测试省"
    full = _df_with_batch([["本科批", 500, 10, 50], ["本科批", 478, 10, 60], ["专科批", 400, 30, 90], ["专科批", 220, 10, 100]])
    only_ben = _df_with_batch([["本科批", 520, 10, 55], ["本科批", 482, 10, 65]])
    load_score_rank_dataframe(db, full, prov, 7001, TRACK)
    load_score_rank_dataframe(db, only_ben, prov, 7002, TRACK)
    db.commit()
    assert get_total_candidates_by_year(db, prov, TRACK) == {7001: 60, 7002: 65}


def test_totals_fall_back_to_max_cumulative_for_all_years_if_any_year_lacks_batch(db):
    """只要有一年没有批次信息（旧数据没重新导入），所有年份一律退回旧口径，不混用两种口径。"""
    prov = "口径回退省"
    with_batch = _df_with_batch([["本科批", 500, 10, 50], ["专科批", 220, 10, 100]])
    no_batch = pd.DataFrame([[500, 10, 70], [220, 30, 120]], columns=["分数", "本段人数", "累计人数"])
    load_score_rank_dataframe(db, with_batch, prov, 7101, TRACK)
    load_score_rank_dataframe(db, no_batch, prov, 7102, TRACK)
    db.commit()
    assert get_total_candidates_by_year(db, prov, TRACK) == {7101: 100, 7102: 120}
