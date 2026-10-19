"""
一分一段表导入逻辑的测试——用构造出来的 pandas.DataFrame（不依赖真实
Excel 文件）测核心导入逻辑，用真实写出来的 .xlsx 文件测一遍"从磁盘读取"
这条路径也是通的。province/year 故意用不会跟真实数据冲突的测试专用值
（"测试省"/9999），这样重复跑测试不会污染 seed 数据，也不需要额外做
"跑完自动清库"这种隔离机制——upsert 本身是幂等的，重复跑结果一致。
"""
import pandas as pd
import pytest

from app.models import ScoreRankRow
from app.services.score_rank_loader import load_score_rank_dataframe, load_score_rank_excel

PROVINCE, YEAR, TRACK = "测试省", 9999, "物理类"


def make_df(rows):
    return pd.DataFrame(rows, columns=["分数", "本段人数", "累计人数"])


def test_basic_import(db):
    df = make_df([[680, 120, 500], [679, 130, 630], [678, 140, 770]])
    result = load_score_rank_dataframe(db, df, PROVINCE, YEAR, TRACK)
    db.commit()

    assert result.total_rows == 3
    assert result.upserted == 3
    assert result.skipped == 0

    row = db.query(ScoreRankRow).filter_by(province=PROVINCE, year=YEAR, track=TRACK, score=679).one()
    assert row.count_at_score == 130
    assert row.cumulative_count == 630


def test_rerun_is_idempotent(db):
    df = make_df([[680, 120, 500]])
    load_score_rank_dataframe(db, df, PROVINCE, YEAR, TRACK)
    db.commit()
    load_score_rank_dataframe(db, df, PROVINCE, YEAR, TRACK)  # 再跑一次
    db.commit()

    count = db.query(ScoreRankRow).filter_by(province=PROVINCE, year=YEAR, track=TRACK, score=680).count()
    assert count == 1, "同一个 (province, year, track, score) 重复导入应该是 upsert，不是插入两行"


def test_update_overwrites_existing_row(db):
    load_score_rank_dataframe(db, make_df([[680, 120, 500]]), PROVINCE, YEAR, TRACK)
    db.commit()
    load_score_rank_dataframe(db, make_df([[680, 999, 999]]), PROVINCE, YEAR, TRACK)  # 数字改过的重新导入
    db.commit()

    row = db.query(ScoreRankRow).filter_by(province=PROVINCE, year=YEAR, track=TRACK, score=680).one()
    assert row.count_at_score == 999 and row.cumulative_count == 999


def test_column_alias_matching(db):
    """不同措辞的表头（"累计人数(含并列)"）也应该能被自动识别。"""
    df = pd.DataFrame([[600, 50, 2000]], columns=["总分", "该分人数", "累计人数(含并列)"])
    result = load_score_rank_dataframe(db, df, PROVINCE, YEAR, TRACK)
    db.commit()
    assert result.upserted == 1


def test_missing_required_column_raises_helpful_error(db):
    df = pd.DataFrame([[680, 120]], columns=["分数", "本段人数"])  # 缺累计人数这一列
    with pytest.raises(ValueError) as exc_info:
        load_score_rank_dataframe(db, df, PROVINCE, YEAR, TRACK)
    assert "cumulative_count" in str(exc_info.value)
    assert "分数" in str(exc_info.value)  # 报错里应该带上实际的列名，方便对照


def test_invalid_rows_are_skipped_not_crashing(db):
    df = make_df([[680, 120, 500], ["异常数据", 1, 1], [99999, 1, 1], [600, -5, 100]])
    result = load_score_rank_dataframe(db, df, PROVINCE, YEAR, TRACK)
    db.commit()
    assert result.upserted == 1
    assert result.skipped == 3
    assert len(result.errors) == 3


def test_merged_top_range_stored_at_lower_bound(db):
    """官方表最高分那一档常见"683-750"这种合并区间写法，应按区间下限存一行。"""
    df = make_df([["683-750", 126, 126], [682, 18, 144]])
    result = load_score_rank_dataframe(db, df, PROVINCE, YEAR, TRACK)
    db.commit()
    assert result.upserted == 2
    assert result.skipped == 0

    row = db.query(ScoreRankRow).filter_by(province=PROVINCE, year=YEAR, track=TRACK, score=683).one()
    assert row.cumulative_count == 126


def test_mixed_track_file_only_imports_matching_track(db):
    """有的省份把物理类/历史类放在同一张表里，用一列科类区分——只应该导入
    跟 --track 参数匹配的行，另一个科类的行要被过滤掉而不是被错误地当成
    当前科类导入进去。
    """
    df = pd.DataFrame(
        [[720, 100, 100, "物理类"], [719, 50, 150, "历史类"], [718, 80, 180, "物理类"]],
        columns=["分数", "本段人数", "累计人数", "科类"],
    )
    result = load_score_rank_dataframe(db, df, PROVINCE, YEAR, "物理类")
    db.commit()

    assert result.upserted == 2
    assert result.skipped_other_track == 1
    assert result.skipped == 0  # 科类不匹配不算数据错误

    assert db.query(ScoreRankRow).filter_by(
        province=PROVINCE, year=YEAR, track="物理类", score=719,
    ).first() is None


def test_load_from_real_xlsx_file(db, tmp_path):
    """端到端测一遍真的从磁盘读 .xlsx 这条路径，不只是测内存里的 DataFrame。"""
    xlsx_path = tmp_path / "score_rank.xlsx"
    make_df([[690, 80, 300], [689, 90, 390]]).to_excel(xlsx_path, index=False)

    result = load_score_rank_excel(str(xlsx_path), PROVINCE, YEAR, TRACK)
    assert result.upserted == 2

    row = db.query(ScoreRankRow).filter_by(province=PROVINCE, year=YEAR, track=TRACK, score=690).one()
    assert row.cumulative_count == 300


def test_batch_column_is_read_when_present_and_null_when_absent(db):
    with_batch = pd.DataFrame(
        [["本科批", 690, 80, 300], ["专科批", 300, 90, 390]], columns=["批次", "分数", "本段人数", "累计人数"],
    )
    load_score_rank_dataframe(db, with_batch, PROVINCE, 9998, TRACK)
    load_score_rank_dataframe(db, make_df([[690, 80, 300]]), PROVINCE, 9997, TRACK)
    db.commit()
    rows = {r.score: r.batch for r in db.query(ScoreRankRow).filter_by(province=PROVINCE, year=9998, track=TRACK)}
    assert rows == {690: "本科批", 300: "专科批"}
    assert db.query(ScoreRankRow).filter_by(province=PROVINCE, year=9997, track=TRACK, score=690).one().batch is None
