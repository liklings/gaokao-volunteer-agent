"""
历年录取位次/选科限制/招生计划 Excel 导入逻辑的测试。用带官方院校代号/
专业组代号的行验证 id 生成，也专门测了"2026 只有计划数、没有分数线"
这种最低分/最低位次留空的行——这类行导入后不应该报错，最低分/最低位次
应该落成 None，不能悄悄编一个 0 或者报错崩溃。
"""
import pandas as pd

from app.models import AdmissionGroup, AdmissionRecord, School
from app.services.admission_data_loader import load_admission_dataframe

PROVINCE, YEAR = "测试省", 9999

COLUMNS = [
    "学校名称", "城市", "院校代号", "专业组代号", "专业组名称", "科类",
    "选考科目要求", "专业方向", "学费", "学制", "计划数", "最低分", "最低位次", "数据来源",
]


def make_df(rows):
    return pd.DataFrame(rows, columns=COLUMNS)


def test_basic_import_with_official_codes(db):
    df = make_df([
        ["测试大学", "南京", "T001", "01", "计算机类", "物理类",
         "物理+化学", "计算机", 6000, "四年", 50, 650, 3000, "https://example.com/T001"],
    ])
    result = load_admission_dataframe(db, df, PROVINCE, YEAR)
    db.commit()

    assert result.schools_upserted == 1
    assert result.groups_upserted == 1
    assert result.records_upserted == 1

    school = db.get(School, "scT001")
    assert school is not None and school.name == "测试大学" and school.city == "南京"

    group = db.get(AdmissionGroup, "scT001-01")
    assert group is not None
    assert group.subjects_required == ["物理", "化学"]
    assert group.tuition == 6000

    record = db.query(AdmissionRecord).filter_by(group_id="scT001-01", year=YEAR).one()
    assert record.plan_count == 50 and record.min_score == 650 and record.min_rank == 3000
    assert record.source_url == "https://example.com/T001"


def test_2026_plan_only_row_leaves_score_and_rank_none(db):
    """2026 只有招生计划、还没有分数线——最低分/最低位次两列在 Excel 里是空的。"""
    df = pd.DataFrame([
        ["测试大学2", "苏州", "T002", "01", "软件工程类", "物理类",
         "物理", "软件", None, None, 30, None, None, None],
    ], columns=COLUMNS)

    result = load_admission_dataframe(db, df, PROVINCE, 2026)
    db.commit()
    assert result.records_upserted == 1

    record = db.query(AdmissionRecord).filter_by(group_id="scT002-01", year=2026).one()
    assert record.plan_count == 30
    assert record.min_score is None
    assert record.min_rank is None, "没有分数线的计划数据，min_rank 应该是 None，不能凭空编造一个数字"


def test_subjects_required_no_limit_becomes_empty_list(db):
    df = make_df([
        ["测试大学3", "无锡", "T003", "01", "不限专业组", "物理类",
         "不限", "", None, None, 20, None, None, None],
    ])
    load_admission_dataframe(db, df, PROVINCE, YEAR)
    db.commit()
    group = db.get(AdmissionGroup, "scT003-01")
    assert group.subjects_required == []


def test_missing_group_code_generates_stable_fallback_id(db):
    """没有官方专业组代号时，用专业组名称生成一个稳定的短哈希当 id——
    "稳定"指同样的输入重复导入应该生成同一个 id（不然每次导入都新建一组，
    数据会越堆越多），用重复导入两次、group 数量不变来验证这一点。
    """
    df = make_df([
        ["测试大学4", "常州", "T004", None, "某专业组", "历史类",
         "历史", "", None, None, 10, None, None, None],
    ])
    load_admission_dataframe(db, df, PROVINCE, YEAR)
    db.commit()
    count_after_first = db.query(AdmissionGroup).filter(AdmissionGroup.id.like("scT004-%")).count()

    load_admission_dataframe(db, df, PROVINCE, YEAR)  # 同样的数据再导一次
    db.commit()
    count_after_second = db.query(AdmissionGroup).filter(AdmissionGroup.id.like("scT004-%")).count()

    assert count_after_first == 1
    assert count_after_second == 1, "同样的输入重复导入应该 upsert 同一条，不该生成新的 id"


def test_missing_required_column_raises_helpful_error(db):
    df = pd.DataFrame([["测试大学5", "南京"]], columns=["学校名称", "城市"])  # 缺科类/计划数等必填列
    try:
        load_admission_dataframe(db, df, PROVINCE, YEAR)
        assert False, "应该抛出 ValueError"
    except ValueError as exc:
        assert "track" in str(exc) or "科类" in str(exc)
