"""
把真实的"历年录取位次 + 选科限制 + 2026招生计划" Excel 导入数据库，
替换/补充 seed_data/jiangsu_physics_seed.json 里的示例数据。

跟 seed_loader.py 用同一套底层 upsert 函数（School/AdmissionGroup/
AdmissionRecord 的 upsert_school/upsert_group/upsert_record 直接从那边
import 复用，不重新写一遍），保证"种子数据导入"和"真实数据导入"最终
落库的字段、幂等语义完全一致，不会出现两条导入路径行为对不上的情况。

【关于 school_id / group_id 怎么来】
现有数据模型（school.id / admission_group.id）用的是稳定的字符串短代号
（比如 'seu'、'seu-cs-04'），不是自增整数——这是为了让种子数据、以后的
真实数据都能用一个可读、跨次导入保持稳定的 id。真实招生计划表通常会带
官方的"院校代号"和"专业组代号"，这两个天然就适合拿来当 id 的基础
（见 _build_school_id / _build_group_id）；如果你的表里没有这两列，
会退化成用学校名称/专业组名称生成一个稳定的短哈希，能跑，但可读性差，
建议尽量提供官方代号列。

【关于 2026 招生计划这类"只有计划、没有分数线"的行】
最低分/最低位次两列允许留空（Excel 里就是空单元格）——upsert_record
落库时这两个字段会是 None，rules_engine.evaluate_group() 已经处理了
"latest.min_rank is None" 这种情况（不会报错，只是算不出冲稳保概率），
recommendation_guard.py 也已经把这种情况当成"数据不完整"的警告，不会
被误判成"通过校验的稳妥候选"，所以可以放心把没有分数线的计划数据导进来，
不需要等真正出分数线才能导入。

用法（在 backend/ 目录、激活 venv 之后）：
    python -m app.services.admission_data_loader \\
        --file ~/Downloads/江苏2026招生计划.xlsx \\
        --province 江苏 --year 2026
"""
from __future__ import annotations

import argparse
import hashlib
import re
from dataclasses import dataclass, field

import pandas as pd
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.services.seed_loader import upsert_group, upsert_record, upsert_school

# 逻辑字段 -> 常见 Excel 表头别名，匹配方式跟 score_rank_loader.py 完全一样
# （精确匹配优先，退化到"列名包含关键词"）——两个加载器故意保持同一套
# 匹配习惯，以后维护的人不用记两套规则。
_COLUMN_ALIASES: dict[str, list[str]] = {
    "school_name": ["学校名称", "院校名称", "学校"],
    "city": ["城市", "所在城市"],
    "school_code": ["院校代号", "院校代码", "学校代码"],
    "group_code": ["专业组代号", "专业组代码", "招生代码"],
    "group_name": ["专业组名称", "专业组"],
    "track": ["科类", "选考科目类别"],
    "subjects_required": ["选考科目要求", "选科要求", "选考要求"],
    "major_keywords": ["专业方向", "专业关键词", "包含专业"],
    "tuition": ["学费", "学费(元/年)", "学费（元/年）"],
    "duration": ["学制"],
    "plan_count": ["计划数", "招生计划", "招生计划数"],
    "min_score": ["最低分", "投档线", "录取最低分"],
    "min_rank": ["最低位次", "投档位次", "录取最低位次"],
    "source_url": ["数据来源", "来源链接", "来源"],
}
# 这几个字段必须有，其余允许缺（比如学费、来源链接现实中经常拿不到）。
_REQUIRED_FIELDS = ["school_name", "city", "track", "subjects_required", "plan_count"]

_SUBJECT_SPLIT_PATTERN = re.compile(r"[+、/，,\s]+")


@dataclass
class LoadResult:
    total_rows: int = 0
    schools_upserted: int = 0
    groups_upserted: int = 0
    records_upserted: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def _find_column(columns: list[str], aliases: list[str]) -> str | None:
    normalized = {str(c).strip(): c for c in columns}
    for alias in aliases:
        if alias in normalized:
            return normalized[alias]
    for alias in aliases:
        for col_str, original in normalized.items():
            if alias in col_str:
                return original
    return None


def _resolve_columns(df: pd.DataFrame) -> dict[str, str]:
    resolved: dict[str, str] = {}
    missing_required: list[str] = []
    for field_name, aliases in _COLUMN_ALIASES.items():
        col = _find_column(list(df.columns), aliases)
        if col is not None:
            resolved[field_name] = col
        elif field_name in _REQUIRED_FIELDS:
            missing_required.append(f"{field_name}（尝试过的别名：{aliases}）")
    if missing_required:
        raise ValueError(
            "以下必填字段在 Excel 表头里找不到匹配的列：\n" + "\n".join(f"- {m}" for m in missing_required) +
            f"\n\n表里实际的列名是：{list(df.columns)}\n"
            f"如果是列名措辞不一样，去 admission_data_loader.py 顶部 _COLUMN_ALIASES 里加一条别名即可。"
        )
    return resolved


def _short_hash(text: str, length: int = 8) -> str:
    return hashlib.md5(text.strip().encode("utf-8")).hexdigest()[:length]


def _build_school_id(school_name: str, school_code: str | None) -> str:
    if school_code:
        return f"sc{str(school_code).strip()}"
    return f"sc{_short_hash(school_name)}"


def _build_group_id(school_id: str, group_code: str | None, group_name: str | None) -> str:
    if group_code:
        return f"{school_id}-{str(group_code).strip()}"
    basis = group_name or "default"
    return f"{school_id}-{_short_hash(basis, 6)}"


def _parse_subjects(raw: str | float | None) -> list[str]:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return []
    text = str(raw).strip()
    if not text or text in ("不限", "无限制"):
        return []
    return [s for s in _SUBJECT_SPLIT_PATTERN.split(text) if s]


def _parse_optional_int(raw) -> int | None:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)) or str(raw).strip() == "":
        return None
    return int(raw)


def load_admission_dataframe(db: Session, df: pd.DataFrame, province: str, year: int) -> LoadResult:
    """核心导入逻辑，接受已读好的 DataFrame——拆出来是为了能用构造出来的
    pandas.DataFrame 直接写单元测试，不用每次都真的读一个 Excel 文件。
    """
    result = LoadResult(total_rows=len(df))
    columns = _resolve_columns(df)

    for idx, row in df.iterrows():
        try:
            school_name = str(row[columns["school_name"]]).strip()
            city = str(row[columns["city"]]).strip()
            track = str(row[columns["track"]]).strip()
            plan_count = _parse_optional_int(row[columns["plan_count"]])
            if not school_name or not city or not track:
                raise ValueError("学校名称/城市/科类不能为空")

            school_code = str(row[columns["school_code"]]).strip() if "school_code" in columns else None
            group_code = str(row[columns["group_code"]]).strip() if "group_code" in columns else None
            group_name = str(row[columns["group_name"]]).strip() if "group_name" in columns else None
            subjects_required = _parse_subjects(row[columns["subjects_required"]]) if "subjects_required" in columns else []
            major_keywords = _parse_subjects(row[columns["major_keywords"]]) if "major_keywords" in columns else []
            tuition = _parse_optional_int(row[columns["tuition"]]) if "tuition" in columns else None
            duration = str(row[columns["duration"]]).strip() if "duration" in columns and pd.notna(row[columns["duration"]]) else None
            min_score = _parse_optional_int(row[columns["min_score"]]) if "min_score" in columns else None
            min_rank = _parse_optional_int(row[columns["min_rank"]]) if "min_rank" in columns else None
            source_url = str(row[columns["source_url"]]).strip() if "source_url" in columns and pd.notna(row[columns["source_url"]]) else None
        except (ValueError, TypeError) as exc:
            result.skipped += 1
            result.errors.append(f"第 {idx + 2} 行（含表头计数）：{exc}，已跳过")
            continue

        school_id = _build_school_id(school_name, school_code)
        group_id = _build_group_id(school_id, group_code, group_name)

        upsert_school(db, {"id": school_id, "name": school_name, "city": city, "province": province, "tags": []})
        result.schools_upserted += 1

        upsert_group(db, school_id, {
            "id": group_id, "code": group_code, "track": track,
            "subjects_required": subjects_required, "major_keywords": major_keywords,
            "tuition": tuition, "duration": duration,
        })
        result.groups_upserted += 1

        upsert_record(db, group_id, {
            "year": year, "group_name": group_name, "plan_count": plan_count,
            "min_score": min_score, "min_rank": min_rank, "source_url": source_url,
        })
        result.records_upserted += 1

    return result


def load_admission_excel(file_path: str, province: str, year: int, header_row: int = 0) -> LoadResult:
    df = pd.read_excel(file_path, header=header_row)
    db = SessionLocal()
    try:
        result = load_admission_dataframe(db, df, province, year)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return result


def _main() -> None:
    parser = argparse.ArgumentParser(description="导入历年录取位次/选科限制/招生计划 Excel 到数据库")
    parser.add_argument("--file", required=True, help="Excel 文件路径")
    parser.add_argument("--province", required=True, help="省份，比如 江苏")
    parser.add_argument("--year", required=True, type=int, help="这份数据对应的年份，比如 2026")
    parser.add_argument("--header-row", type=int, default=0, help="表头所在行（从0开始），默认0")
    args = parser.parse_args()

    result = load_admission_excel(args.file, args.province, args.year, header_row=args.header_row)
    print(
        f"共 {result.total_rows} 行，学校 {result.schools_upserted} 次 upsert、"
        f"专业组 {result.groups_upserted} 次 upsert、录取记录 {result.records_upserted} 次 upsert，"
        f"跳过 {result.skipped} 行。"
    )
    if result.errors:
        print("以下行被跳过，原因：")
        for e in result.errors[:50]:
            print(f"  {e}")
        if len(result.errors) > 50:
            print(f"  ……还有 {len(result.errors) - 50} 条，未全部打印")


if __name__ == "__main__":
    _main()
