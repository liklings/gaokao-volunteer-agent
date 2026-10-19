"""
"专家版"大表导入逻辑的测试——这份文件三行合并表头、专业粒度（同一专业组
好几行），核心要测的是：
1. 同一专业组下多行专业能正确聚合成一条专业组记录，不会重复插入学校/专业组。
2. 2026 年用文件自带的"专业组计划人数"，2025 年（后缀1）优先用文件自带的
   "专业组最低分1/位次1"，2024/2023 年（后缀2/3，文件没有专业组级别字段）
   退化成"组内专业最低分/位次取最小值"近似，并且要在 notes 里说明这是近似值。
3. 科类"物理"/"历史" 要被归一化成"物理类"/"历史类"。
4. "选科要求"用"和"分隔（比如"政治和地理"）要能正确拆开。
5. 缺列时报错信息要清楚。

省份故意用"测试省"这个不会跟真实种子数据（江苏）冲突的测试专用值，学校代号
用"MT00x"前缀（跟 test_admission_data_loader.py 已经用掉的"T00x"区分开）——
这两点都是为了不跟 test_tools_real_data.py / test_react_loop.py 等依赖"真实
种子数据默认能查到10条候选"这类精确计数的测试互相污染（同一个 Postgres
实例被所有测试共用，province="江苏" 的测试数据会被默认搜索过滤条件命中）。
"""
import pandas as pd

from app.models import AdmissionGroup, AdmissionRecord, School
from app.services.admission_master_loader import load_admission_master_dataframe

PROVINCE = "测试省"

COLUMNS = [
    "ID", "年份", "生源地", "批次", "计划类别", "科类", "院校代码", "院校名称", "专业组代码", "专业组名称",
    "专业代码", "专业名称", "选科要求", "学费", "学制", "专业组计划人数", "招生章程",
    "院校标签", "本科/专科", "院校排名",
    "专业组最低分1", "专业组最低位次1", "最低分1", "最低位次1", "计划人数结果1",
    "最低分2", "最低位次2", "计划人数结果2",
    "最低分3", "最低位次3", "计划人数结果3",
]


def make_row(**overrides):
    base = {
        "ID": 1, "年份": 2026, "生源地": PROVINCE, "批次": "本科批", "计划类别": "本科院校", "科类": "物理",
        "院校代码": "MT001", "院校名称": "测试大学", "专业组代码": "01", "专业组名称": "计算机类",
        "专业代码": "01", "专业名称": "计算机科学与技术", "选科要求": "物理", "学费": 6000, "学制": 4, "专业组计划人数": 50,
        "招生章程": "https://example.com/MT001",
        "院校标签": "211/双一流/保研资格", "本科/专科": "本科", "院校排名": 88,
        "专业组最低分1": 650, "专业组最低位次1": 3000, "最低分1": 650, "最低位次1": 3000, "计划人数结果1": 25,
        "最低分2": 640, "最低位次2": 3500, "计划人数结果2": 25,
        "最低分3": 630, "最低位次3": 4000, "计划人数结果3": 20,
    }
    base.update(overrides)
    return base


def make_df(rows):
    return pd.DataFrame([[r[c] for c in COLUMNS] for r in rows], columns=COLUMNS)


def test_multiple_majors_in_same_group_aggregate_into_one_group_record(db):
    """同一专业组两个专业（专业代码01/02）应该只 upsert 一次学校/专业组，
    不是按专业行数重复插入。"""
    df = make_df([
        make_row(专业代码="01"),
        make_row(专业代码="02", 专业组最低分1=655, 专业组最低位次1=2800),  # 组内字段应取第一行代表值
    ])
    result = load_admission_master_dataframe(db, df, exam_province=PROVINCE)
    db.commit()

    assert result.total_major_rows == 2
    assert result.schools_upserted == 1
    assert result.groups_upserted == 1

    school = db.get(School, "scMT001")
    assert school is not None and school.name == "测试大学" and school.province == PROVINCE

    group = db.get(AdmissionGroup, "scMT001-01")
    assert group is not None
    assert group.subjects_required == ["物理"]
    assert group.tuition == 6000
    assert group.duration == "4年"


def test_school_tags_and_overall_rank_parsed_from_file(db):
    """"院校标签"（"/"分隔）+ "本科/专科"应该拼成 School.tags，"院校排名"
    应该存进 School.overall_rank——这两个字段是"好学校优先展示"功能现在
    优先使用的权威数据源（见 school_quality.get_school_quality_for_school），
    比之前"拿学校名去匹配另一份静态表"可靠。"""
    df = make_df([make_row(院校标签="985/211/双一流/国重点/保研资格", **{"本科/专科": "本科", "院校排名": 6})])
    load_admission_master_dataframe(db, df, exam_province=PROVINCE)
    db.commit()

    school = db.get(School, "scMT001")
    assert set(school.tags) == {"985", "211", "双一流", "国重点", "保研资格", "本科"}
    assert school.overall_rank == 6


def test_school_rank_placeholder_slash_parses_to_none(db):
    """"院校排名"这一列有的是占位符"/"（没有排名数据），不是数字——
    应该安全存成 None，不能报错、也不能编一个假排名。"""
    df = make_df([make_row(**{"院校排名": "/"})])
    load_admission_master_dataframe(db, df, exam_province=PROVINCE)
    db.commit()

    school = db.get(School, "scMT001")
    assert school.overall_rank is None


def test_major_names_from_all_rows_aggregate_into_major_keywords(db):
    """之前这里一直硬编码 major_keywords=[]，候选卡片上只能看到"05专业组"
    这种编号、看不到具体是什么专业，用户没法判断该报哪个专业——现在应该把
    同一专业组下所有专业行的"专业名称"去重聚合起来，且保留出现顺序。"""
    df = make_df([
        make_row(专业代码="01", 专业名称="计算机科学与技术"),
        make_row(专业代码="02", 专业名称="软件工程"),
        make_row(专业代码="03", 专业名称="计算机科学与技术"),  # 重复，不该出现两次
    ])
    load_admission_master_dataframe(db, df, exam_province=PROVINCE)
    db.commit()

    group = db.get(AdmissionGroup, "scMT001-01")
    assert group.major_keywords == ["计算机科学与技术", "软件工程"]


def test_admission_category_is_captured_from_file(db):
    """"计划类别"是文件里现成的权威招生类型分类，应该原样存进
    admission_category，供 rules_engine.py 判断是不是军事/警务/专项计划
    等特殊类型招生（见 app/services/special_admission.py）。"""
    df = make_df([make_row(计划类别="提前录取本科院校—军队院校")])
    load_admission_master_dataframe(db, df, exam_province=PROVINCE)
    db.commit()

    group = db.get(AdmissionGroup, "scMT001-01")
    assert group.admission_category == "提前录取本科院校—军队院校"


def test_2026_uses_group_plan_count_and_leaves_score_none(db):
    df = make_df([make_row()])
    load_admission_master_dataframe(db, df, exam_province=PROVINCE)
    db.commit()

    record_2026 = db.query(AdmissionRecord).filter_by(group_id="scMT001-01", year=2026).one()
    assert record_2026.plan_count == 50
    assert record_2026.min_score is None
    assert record_2026.min_rank is None, "2026年只有计划、没有分数线，不能凭空编造"
    assert record_2026.source_url == "https://example.com/MT001"


def test_year1_uses_file_provided_group_level_fields(db):
    df = make_df([make_row()])
    load_admission_master_dataframe(db, df, exam_province=PROVINCE)
    db.commit()

    record_2025 = db.query(AdmissionRecord).filter_by(group_id="scMT001-01", year=2025).one()
    assert record_2025.min_score == 650
    assert record_2025.min_rank == 3000


def test_year2_and_year3_approximate_from_major_level_minimum(db):
    """2024/2023 文件没有专业组级别字段，组内两个专业取最低分/位次的最小值。"""
    df = make_df([
        make_row(专业代码="01", 最低分2=640, 最低位次2=3500, 最低分3=630, 最低位次3=4000),
        make_row(专业代码="02", 最低分2=635, 最低位次2=3800, 最低分3=628, 最低位次3=4100),
    ])
    result = load_admission_master_dataframe(db, df, exam_province=PROVINCE)
    db.commit()

    record_2024 = db.query(AdmissionRecord).filter_by(group_id="scMT001-01", year=2024).one()
    assert record_2024.min_score == 635  # 两个专业里更低的分数
    assert record_2024.min_rank == 3800  # 更差（数值更大）的位次

    record_2023 = db.query(AdmissionRecord).filter_by(group_id="scMT001-01", year=2023).one()
    assert record_2023.min_score == 628
    assert record_2023.min_rank == 4100

    assert any("近似" in n for n in result.notes)


def test_integer_school_and_group_codes_do_not_get_mangled_into_floats(db):
    """真实文件里"院校代码"/"专业组代码"整列都是纯数字（int64），不是字符串——
    这种情况下 pandas 按行 iterrows() 会把整行数值列统一转成 float64，把
    "132"变成"132.0"，导致历史年份记录的 group_id 拼出来跟第一遍 upsert_group
    时用的对不上（外键找不到对应专业组）。这里故意用整数类型的代号复现这个
    场景，而不是像其它测试那样用字符串代号（字符串代号不会触发这个 dtype
    坑，容易掩盖问题）。
    """
    df = make_df([
        make_row(院校代码=132, 专业组代码=3, 专业代码=1,
                  最低分2=610, 最低位次2=24000, 最低分3=600, 最低位次3=25000),
    ])
    load_admission_master_dataframe(db, df, exam_province=PROVINCE)
    db.commit()

    assert db.get(School, "sc132") is not None
    assert db.get(AdmissionGroup, "sc132-3") is not None

    record_2024 = db.query(AdmissionRecord).filter_by(group_id="sc132-3", year=2024).one()
    assert record_2024.min_score == 610
    assert record_2024.min_rank == 24000


def test_track_normalization_and_subjects_with_和_separator(db):
    df = make_df([make_row(院校代码="MT002", 科类="历史", 选科要求="政治和地理")])
    load_admission_master_dataframe(db, df, exam_province=PROVINCE)
    db.commit()

    group = db.get(AdmissionGroup, "scMT002-01")
    assert group.track == "历史类"
    assert group.subjects_required == ["政治", "地理"]


def test_tuition_dai_ding_becomes_none_not_crash(db):
    """"学费"列常见"待定"这种非数字占位符，不应该报错崩溃，应该存 None。"""
    df = make_df([make_row(院校代码="MT003", 学费="待定")])
    load_admission_master_dataframe(db, df, exam_province=PROVINCE)
    db.commit()
    group = db.get(AdmissionGroup, "scMT003-01")
    assert group.tuition is None


def test_missing_required_column_raises_helpful_error(db):
    df = pd.DataFrame([["测试大学", "MT001"]], columns=["院校名称", "院校代码"])
    try:
        load_admission_master_dataframe(db, df, exam_province=PROVINCE)
        assert False, "应该抛出 ValueError"
    except ValueError as exc:
        assert "专业组代码" in str(exc) or "科类" in str(exc)


def test_rerun_is_idempotent(db):
    df = make_df([make_row(院校代码="MT004")])
    load_admission_master_dataframe(db, df, exam_province=PROVINCE)
    db.commit()
    load_admission_master_dataframe(db, df, exam_province=PROVINCE)  # 再跑一次
    db.commit()

    count = db.query(AdmissionGroup).filter(AdmissionGroup.id.like("scMT004-%")).count()
    assert count == 1
