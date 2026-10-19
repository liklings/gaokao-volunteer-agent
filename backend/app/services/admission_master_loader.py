"""
专门解析"江苏-2026-专家版数据（23-26）"这一类合并了 2026 招生计划 + 2025/2024/2023
三年历史录取数据的"专家版"大表，跟通用的 admission_data_loader.py 是两个独立文件，
不复用同一套列名匹配逻辑——因为这份专家版表的结构跟普通招生计划表不一样，值得
单独处理，而不是硬塞进通用别名匹配里：

1. 表头是三行合并单元格（"2026年招生计划/2025年专业组数据/…"这种大分类 → 更细的
   中分类 → 真正的字段名"ID/年份/生源地/…"），真正能当列名用的字段名在第三行，也就是
   pandas 里 header=2（0 起始，第 3 行）。如果你的文件表头结构不完全一样，用命令行的
   --header-row 指定正确的行号。

2. 这份表是"专业"粒度——同一个专业组（比如"01专业组"）会有好几行，每行对应组内一个
   具体专业。而现有数据模型（AdmissionGroup/AdmissionRecord）是"专业组"粒度，还没有
   细到专业这一级。这里的处理方式是：把同一专业组下所有专业行聚合成一条专业组记录——
      - 2026 年：用文件里现成的"专业组计划人数"（组内所有专业行这一列本来就应该相同）。
      - 2025 年（后缀1）：文件里同时给了"专业组最低分1/专业组最低位次1"这两个专业组
        级别字段，直接用。
      - 2024/2023 年（后缀2/3）：文件里**没有**专业组级别的最低分/位次字段，只有每个
        专业自己的"最低分2/最低位次2"等。这里改用"同一专业组下所有专业最低分/位次里的
        最小值"来近似专业组投档线——江苏新高考"专业组"平行志愿投档时，一个专业组只有
        一条投档线，理论上等于组内被投进来的所有专业里分数最低的那个，所以这个近似在
        方法上是合理的，但**不是**省考试院公布的投档线原文，跟 2025 年那种"文件自带
        官方专业组字段"的可信度不完全一样，已经在返回结果的 notes 里注明，导入后建议
        对照官网核实几条关键数据。

3. 科类在这份文件里写的是"物理"/"历史"（没有"类"字），这里统一转换成系统其他地方一致
   使用的"物理类"/"历史类"，避免同一份数据在系统里出现两种写法。

用法（在 backend/ 目录、激活 venv 之后）：
    python -m app.services.admission_master_loader \\
        --file ~/Downloads/江苏-2026-专家版数据.xlsx

省份（生源地）默认从文件的"生源地"列自动读取（这份文件是"江苏"），也可以用
--province 手动指定/覆盖。
"""
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field

import pandas as pd
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.services.seed_loader import upsert_group, upsert_record, upsert_school

_TRACK_MAP = {"物理": "物理类", "历史": "历史类", "物理类": "物理类", "历史类": "历史类"}

# "政治和地理" 这种写法用"和"分隔，跟别的文件里常见的 +、/、， 等分隔符不一样，
# 这里额外把"和"也当成一个分隔符。
_SUBJECT_SPLIT_PATTERN = re.compile(r"[+、/，,\s]+|和")

_REQUIRED_COLUMNS = ["院校代码", "院校名称", "专业组代码", "科类", "选科要求", "专业组计划人数", "年份"]

_DEFAULT_HEADER_ROW = 2  # 见模块文档：真正的字段名在文件第 3 行（0 起始第 2 行）


def _normalize_track(raw) -> str:
    text = str(raw).strip()
    return _TRACK_MAP.get(text, text)


def _parse_subjects(raw) -> list[str]:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return []
    text = str(raw).strip()
    if not text or text in ("不限", "无限制"):
        return []
    return [s for s in _SUBJECT_SPLIT_PATTERN.split(text) if s]


def _parse_int_or_none(raw) -> int | None:
    """比"学费"这种字段常见的"待定"之类非数字占位符也能安全跳过，不报错、不编造。"""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    try:
        return int(raw)
    except (ValueError, TypeError):
        return None


def _parse_duration(raw) -> str | None:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    try:
        return f"{int(raw)}年"
    except (ValueError, TypeError):
        text = str(raw).strip()
        return text or None


def _parse_school_tags(row) -> list[str]:
    """从"院校标签"（"/"分隔，比如"985/211/双一流/国重点/保研资格"）和
    "本科/专科"（"本科"/"专科"/"职业本科"）这两列拼出这所学校的标签列表。
    这是数据源里现成的、跟学校本身绑定的权威信息，app/services/
    school_quality.py 现在优先用这个字段判断学校档次（985/211/双一流/
    专科），比之前那套"拿学校名去匹配《全国高等院校信息汇总》静态表"
    可靠得多——同一行数据，不存在匹配不上的问题。
    """
    tags: list[str] = []
    raw_labels = row.get("院校标签")
    if pd.notna(raw_labels):
        tags.extend(t.strip() for t in str(raw_labels).split("/") if t.strip())
    level = row.get("本科/专科")
    if pd.notna(level):
        level_text = str(level).strip()
        if level_text and level_text not in tags:
            tags.append(level_text)
    return tags


def _parse_school_rank(raw) -> int | None:
    """"院校排名"这一列有的是数字，有的是占位符"/"（没有排名数据），
    跟"学费"字段常见的"待定"是同一类情况——安全跳过，不报错、不编造。"""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    try:
        return int(raw)
    except (ValueError, TypeError):
        return None


@dataclass
class LoadResult:
    total_major_rows: int = 0
    schools_upserted: int = 0
    groups_upserted: int = 0
    records_upserted: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def load_admission_master_dataframe(
    db: Session, df: pd.DataFrame, exam_province: str | None = None,
) -> LoadResult:
    """核心导入逻辑，接受已经读好的 DataFrame——拆出来是为了能用构造出来的
    pandas.DataFrame 直接写单元测试，不用每次都真的读一个几千行的 Excel 文件。
    """
    missing = [c for c in _REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            "这份专家版文件缺少必需的列：" + "、".join(missing) +
            f"\n表里实际的列是：{list(df.columns)}\n"
            "这份文件的表头一般在第 3 行（也就是 --header-row 2），如果你确认表头位置"
            "不是这样，用 --header-row 指定正确的行号重新试一次。"
        )

    result = LoadResult(total_major_rows=len(df))

    if exam_province:
        province = exam_province
    elif "生源地" in df.columns and df["生源地"].notna().any():
        province = str(df["生源地"].dropna().iloc[0]).strip()
    else:
        province = "未知"

    # ---- 第一遍：按 (院校代码, 专业组代码) 去重，取每组第一行代表这个专业组的
    # 公共字段（选科要求/学费/学制等组内各专业行应该一致），upsert 专业组
    # + 2026 年（文件自带的当前年）计划记录 ----
    #
    # 学校要单独再去重一层（按院校代码，不带专业组代码）——同一所学校在文件里
    # 通常对应几十个专业组，如果 upsert_school 跟着专业组循环重复调用同一个
    # 学校 id，在同一个数据库会话、还没 flush 的情况下，db.get() 查不到"刚
    # add() 但还没真正写进数据库"的那一条，会导致同一个主键被反复 db.add()，
    # 提交时数据库报主键冲突（真实数据量小的测试样本很难暴露这个问题，因为
    # 大部分学校在小样本里只出现一两次；真实全量文件里一所学校几十个专业组，
    # 这个 bug 必现）。所以这里把"学校"单独去重、单独一轮 upsert，保证每个
    # 学校 id 在整个导入过程里只 upsert 一次。
    school_first_row: dict[str, pd.Series] = {}
    group_first_row: dict[tuple[str, str], pd.Series] = {}
    # 这份文件是"专业"粒度——同一专业组下每个专业单独一行，"专业名称"这一列
    # 才是干净的专业名（不带"(军事翻译...)(只招男生)"这类括号备注，那些在
    # "专业全称"/"专业备注"里）。之前这里一直传 major_keywords=[]，候选卡片
    # 上完全看不到具体专业是什么、只有"05专业组"这种编号，用户没法判断该报
    # 哪个专业——这里改成把同一专业组下所有专业行的"专业名称"去重聚合成一个
    # 列表，保留出现顺序（不用 set，set 会打乱顺序，专业组里第一个专业通常
    # 是最主要的方向）。
    major_names_by_group: dict[tuple[str, str], list[str]] = {}
    for idx, row in df.iterrows():
        school_code_raw = row.get("院校代码")
        group_code_raw = row.get("专业组代码")
        if pd.isna(school_code_raw) or pd.isna(group_code_raw):
            result.skipped += 1
            result.errors.append(f"第 {idx + _DEFAULT_HEADER_ROW + 2} 行：院校代码/专业组代码为空，已跳过")
            continue
        school_code = str(school_code_raw).strip()
        school_first_row.setdefault(school_code, row)
        key = (school_code, str(group_code_raw).strip())
        group_first_row.setdefault(key, row)

        major_name = row.get("专业名称")
        if pd.notna(major_name):
            names = major_names_by_group.setdefault(key, [])
            major_name = str(major_name).strip()
            if major_name and major_name not in names:
                names.append(major_name)

    for school_code, row in school_first_row.items():
        try:
            school_name = str(row["院校名称"]).strip()
            city = str(row["城市"]).strip() if pd.notna(row.get("城市")) else ""
        except (ValueError, TypeError, KeyError) as exc:
            result.skipped += 1
            result.errors.append(f"学校 {school_code}：字段解析异常，已跳过——{exc}")
            continue
        upsert_school(db, {
            "id": f"sc{school_code}", "name": school_name, "city": city, "province": province,
            "tags": _parse_school_tags(row), "overall_rank": _parse_school_rank(row.get("院校排名")),
        })
        result.schools_upserted += 1

    current_year = 2026
    for (school_code, group_code), row in group_first_row.items():
        try:
            track = _normalize_track(row["科类"])
            subjects_required = _parse_subjects(row["选科要求"])
            tuition = _parse_int_or_none(row.get("学费"))
            duration = _parse_duration(row.get("学制"))
            plan_count = _parse_int_or_none(row.get("专业组计划人数"))
            source_url = str(row["招生章程"]).strip() if pd.notna(row.get("招生章程")) else None
            group_name = str(row["专业组名称"]).strip() if pd.notna(row.get("专业组名称")) else None
            # "计划类别"是这份文件里现成的、权威的招生类型分类（"本科院校"/
            # "专科院校"/"提前录取本科院校—军队院校"/"提前录取本科院校—公安
            # 政法院校(专业)"/"专科院校-定向培养军士"/"提前录取本科院校—高校
            # 专项计划"等），比拿学校名/专业组名做关键词猜测可靠得多——这类
            # 特殊类型招生通常还需要政治审查/体检/户籍资格等额外条件，见
            # rules_engine.py 和 app/services/special_admission.py 里怎么用
            # 这个字段。用 .get 容错，不在 _REQUIRED_COLUMNS 里，旧格式文件
            # 没有这一列时就是 None，退回关键词匹配兜底。
            admission_category = (
                str(row["计划类别"]).strip() if pd.notna(row.get("计划类别")) else None
            )
            if pd.notna(row.get("年份")):
                current_year = int(row["年份"])
        except (ValueError, TypeError, KeyError) as exc:
            result.skipped += 1
            result.errors.append(f"专业组 {school_code}-{group_code}：字段解析异常，已跳过——{exc}")
            continue

        school_id = f"sc{school_code}"
        group_id = f"{school_id}-{group_code}"

        upsert_group(db, school_id, {
            "id": group_id, "code": group_code, "track": track,
            "subjects_required": subjects_required,
            "major_keywords": major_names_by_group.get((school_code, group_code), []),
            "tuition": tuition, "duration": duration,
            "admission_category": admission_category,
        })
        result.groups_upserted += 1

        upsert_record(db, group_id, {
            "year": current_year, "group_name": group_name,
            "plan_count": plan_count, "min_score": None, "min_rank": None, "source_url": source_url,
        })
        result.records_upserted += 1

    # ---- 第二遍：历史年份（后缀 1/2/3，对应 current_year-1/-2/-3）。
    # 后缀1（去年）文件自带"专业组最低分1/专业组最低位次1"，优先直接用；
    # 后缀2/3 文件没有专业组级别字段，改用组内所有专业最低分/位次的最小值近似。----
    for suffix, offset in ((1, 1), (2, 2), (3, 3)):
        year = current_year - offset
        major_score_col, major_rank_col = f"最低分{suffix}", f"最低位次{suffix}"
        group_score_col, group_rank_col = f"专业组最低分{suffix}", f"专业组最低位次{suffix}"
        plan_col = f"计划人数结果{suffix}"

        if major_score_col not in df.columns or major_rank_col not in df.columns:
            result.notes.append(f"{year} 年（后缀{suffix}）在文件里找不到最低分/最低位次列，已跳过这一年")
            continue

        has_group_level = group_score_col in df.columns and group_rank_col in df.columns
        # 位次数字越大代表排名越靠后（越差），最低分（min_score，数值最小）对应的
        # 恰好是最差的那个位次（数值最大）——所以位次这里要用 max，不是 min，
        # 两者必须成对匹配同一个"最弱录取"的专业，不能各自独立取极值。
        agg_cols = {"min_score": (major_score_col, "min"), "min_rank": (major_rank_col, "max")}
        if plan_col in df.columns:
            agg_cols["plan_sum"] = (plan_col, "sum")
        if has_group_level:
            agg_cols["group_min_score"] = (group_score_col, "min")
            agg_cols["group_min_rank"] = (group_rank_col, "min")

        cols_needed = ["院校代码", "专业组代码", major_score_col, major_rank_col]
        if plan_col in df.columns:
            cols_needed.append(plan_col)
        if has_group_level:
            cols_needed += [group_score_col, group_rank_col]

        sub = df[cols_needed].dropna(subset=[major_score_col, major_rank_col], how="all")
        if sub.empty:
            result.notes.append(f"{year} 年（后缀{suffix}）没有任何一行有最低分/位次数据，已跳过这一年")
            continue

        grouped = sub.groupby(["院校代码", "专业组代码"]).agg(**agg_cols).reset_index()
        # 这里的 grouped 只有数值列（院校代码/专业组代码是 int64，其余聚合列是
        # float64），下面按行 iterrows() 时 pandas 会把整行统一成一种 dtype——
        # 全数值的行会被统一提升成 float64，把整数代号"132"变成"132.0"，
        # 拼出来的 group_id 就对不上第一遍 upsert_group 时用的"132"，导致这里
        # upsert 的历史年份记录外键找不到对应的专业组。这里先把两个代号列转成
        # 字符串（列级别转换，不受行级别 dtype 统一影响），再按行取值就不会再被
        # 悄悄转成浮点数了。
        grouped["院校代码"] = grouped["院校代码"].astype(str)
        grouped["专业组代码"] = grouped["专业组代码"].astype(str)

        approximated_any = False
        for _, g in grouped.iterrows():
            school_code = str(g["院校代码"]).strip()
            group_code = str(g["专业组代码"]).strip()
            group_id = f"sc{school_code}-{group_code}"

            if has_group_level and pd.notna(g.get("group_min_score")):
                min_score = int(g["group_min_score"])
                min_rank = int(g["group_min_rank"]) if pd.notna(g.get("group_min_rank")) else None
            else:
                approximated_any = True
                min_score = int(g["min_score"]) if pd.notna(g.get("min_score")) else None
                min_rank = int(g["min_rank"]) if pd.notna(g.get("min_rank")) else None

            plan_count = None
            if "plan_sum" in g and pd.notna(g["plan_sum"]) and g["plan_sum"] > 0:
                plan_count = int(g["plan_sum"])

            upsert_record(db, group_id, {
                "year": year, "group_name": None,
                "plan_count": plan_count, "min_score": min_score, "min_rank": min_rank, "source_url": None,
            })
            result.records_upserted += 1

        if approximated_any:
            result.notes.append(
                f"{year} 年：文件没有专业组级别的最低分/最低位次字段，已用同一专业组下"
                f"所有专业最低分/位次的最小值近似（不是官方公布的投档线原文，建议核实关键数据）"
            )

    return result


def load_admission_master_excel(
    file_path: str, exam_province: str | None = None, header_row: int = _DEFAULT_HEADER_ROW,
) -> LoadResult:
    df = pd.read_excel(file_path, header=header_row)
    db = SessionLocal()
    try:
        result = load_admission_master_dataframe(db, df, exam_province)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return result


def _main() -> None:
    parser = argparse.ArgumentParser(description="导入'专家版'合并招生计划+历年录取数据 Excel 到数据库")
    parser.add_argument("--file", required=True, help="Excel 文件路径")
    parser.add_argument("--province", default=None, help="生源地/招生省份，不填则从文件的'生源地'列自动识别")
    parser.add_argument("--header-row", type=int, default=_DEFAULT_HEADER_ROW, help="表头所在行（0 起始），默认 2")
    args = parser.parse_args()

    result = load_admission_master_excel(args.file, args.province, header_row=args.header_row)
    print(
        f"共 {result.total_major_rows} 行专业数据，聚合出学校 {result.schools_upserted} 次 upsert、"
        f"专业组 {result.groups_upserted} 次 upsert、年度录取记录 {result.records_upserted} 次 upsert，"
        f"跳过 {result.skipped} 行。"
    )
    if result.notes:
        print("说明：")
        for n in result.notes:
            print(f"  - {n}")
    if result.errors:
        print("以下行被跳过，原因：")
        for e in result.errors[:50]:
            print(f"  {e}")
        if len(result.errors) > 50:
            print(f"  ……还有 {len(result.errors) - 50} 条，未全部打印")


if __name__ == "__main__":
    _main()
