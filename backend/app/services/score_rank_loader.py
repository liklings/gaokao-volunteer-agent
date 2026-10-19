"""
把省考试院发布的"一分一段表" Excel 导入 score_rank_rows 表。

官方一分一段表各省格式不完全一样，常见的是三列：分数 / 本段人数 / 累计人数
（累计人数就是这个分数对应的最好位次），但列名措辞各省不统一（"累计人数"
还是"累计人数(含并列)"、"位次"还是"累计位次"……）。这里不要求用户先手动
把表改成固定格式，而是维护一份"常见列名别名表"（_COLUMN_ALIASES），自动
按别名匹配——真的匹配不上时，报错信息会把"需要哪个字段"和"表里实际有
哪些列名"都打出来，让用户能直接照着改 Excel 表头或者在这份别名表里加一条，
而不是一个模糊的 KeyError。

用法（在 backend/ 目录、激活 venv 之后）：
    python -m app.services.score_rank_loader \\
        --file ~/Downloads/江苏2026一分一段表.xlsx \\
        --province 江苏 --year 2026 --track 物理类

设计成幂等：按 (province, year, track, score) upsert，重新导入同一份文件
不会产生重复行，覆盖式更新——跟 seed_loader.py 是同一个设计取舍。

跟 seed_loader.py 刻意分成两个独立文件（校验 + 导入不写成一步）不同，
这里的"校验"内嵌在导入过程里，逐行校验、错误行会被跳过并计入
skipped_rows、原因收集进 errors 列表返回给调用方——一分一段表通常有
几百到上千行，不值得为它单独维护一份预校验脚本，但错误不能默默吞掉，
所以选择"能跑的照常导入，跑不了的原因原样收集回传"这个折中。
"""
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import ScoreRankRow

# 每个逻辑字段对应的常见 Excel 表头别名——按顺序尝试精确匹配，
# 都不匹配再退化到"列名包含这个词"的模糊匹配。新省份的表头如果都不在
# 这里，往对应的列表里加一个字符串即可，不用改下面的匹配逻辑。
_COLUMN_ALIASES: dict[str, list[str]] = {
    "score": ["分数", "总分", "成绩"],
    "count_at_score": ["本段人数", "该分人数", "本分人数", "人数"],
    "cumulative_count": ["累计人数", "累计人数(含并列)", "累计人数（含并列）", "累计", "位次", "累计位次"],
}

# 可选列：表里有就读、没有就当 NULL，不影响导入。
_OPTIONAL_COLUMN_ALIASES: dict[str, list[str]] = {
    "batch": ["批次"],
}

# 分数的合理范围——江苏现行高考满分750，这里留一点余量而不是写死750，
# 避免以后接入其他省份（满分标准不完全一样）时要改代码；纯粹用来挡掉
# "表头行被误当成数据行"这类明显异常，不是精确的分数合法性校验。
_SCORE_MIN, _SCORE_MAX = 0, 900

# 官方一分一段表最高分那一档经常是合并区间，比如"683-750"（表示683到750分之间
# 所有考生都并列在这一档，因为顶尖分数段人数太少不逐分公布）。这种情况按区间
# 下限存一行——683分及以上的考生查分数都会命中"683分对应累计人数=126"这一行，
# 这正是这一档的准确含义：只有连续两次都不低于683分才会落到比它更高的档位，
# 跟原表定义一致，不是编造数据。
_SCORE_RANGE_PATTERN = re.compile(r"^\s*(\d+)\s*-\s*\d+\s*$")

# 有些省份的一分一段表会把物理类/历史类（或文科/理科）放在同一张表里，用一列
# 科类字段区分，而不是每个科类单独一个文件。如果表里能找到这样的列，就只导入
# 跟 --track 参数匹配的行，避免把另一个科类的数据错当成这个科类导入进去。
# 如果表里没有这一列（比如本来就是单科类文件），就不做任何过滤，保持原有行为。
_TRACK_COLUMN_ALIASES = ["科类", "选考科目类别", "类别"]


def _parse_score(raw) -> int:
    """兼容"683-750"这种合并区间写法，按区间下限取整数分数。"""
    if isinstance(raw, bool):
        raise ValueError(f"分数字段不能是布尔值：{raw!r}")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    text = str(raw).strip()
    match = _SCORE_RANGE_PATTERN.match(text)
    if match:
        return int(match.group(1))
    return int(text)


@dataclass
class LoadResult:
    total_rows: int = 0
    upserted: int = 0
    skipped: int = 0
    skipped_other_track: int = 0  # 表里混了别的科类、按 --track 过滤掉的行数（不算错误）
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
    missing: list[str] = []
    for field_name, aliases in _COLUMN_ALIASES.items():
        col = _find_column(list(df.columns), aliases)
        if col is None:
            missing.append(f"{field_name}（尝试过的别名：{aliases}）")
        else:
            resolved[field_name] = col
    if missing:
        raise ValueError(
            "以下字段在 Excel 表头里找不到匹配的列：\n" + "\n".join(f"- {m}" for m in missing) +
            f"\n\n表里实际的列名是：{list(df.columns)}\n"
            f"如果是列名措辞不一样，去 score_rank_loader.py 顶部 _COLUMN_ALIASES 里加一条别名即可。"
        )
    return resolved


def load_score_rank_dataframe(
    db: Session, df: pd.DataFrame, province: str, year: int, track: str,
) -> LoadResult:
    """核心导入逻辑，接受一个已经读好的 DataFrame——单独拆出来是为了能在
    不依赖真实 Excel 文件的情况下写单元测试（用 pandas.DataFrame 直接构造
    测试数据），also 方便以后如果数据来源换成别的格式（比如 CSV、数据库
    直连），只需要另外写一个"读成 DataFrame"的入口，这个函数不用变。
    """
    result = LoadResult(total_rows=len(df))
    columns = _resolve_columns(df)
    track_column = _find_column(list(df.columns), _TRACK_COLUMN_ALIASES)
    batch_column = _find_column(list(df.columns), _OPTIONAL_COLUMN_ALIASES["batch"])

    for idx, row in df.iterrows():
        if track_column is not None:
            row_track = str(row[track_column]).strip()
            if row_track and row_track != track:
                result.skipped_other_track += 1
                continue

        try:
            score = _parse_score(row[columns["score"]])
            count_at_score = int(row[columns["count_at_score"]])
            cumulative_count = int(row[columns["cumulative_count"]])
        except (ValueError, TypeError) as exc:
            result.skipped += 1
            result.errors.append(f"第 {idx + 2} 行（含表头计数）：字段不是合法数字，已跳过——{exc}")
            continue

        if not (_SCORE_MIN <= score <= _SCORE_MAX):
            result.skipped += 1
            result.errors.append(f"第 {idx + 2} 行：分数 {score} 超出合理范围 [{_SCORE_MIN}, {_SCORE_MAX}]，已跳过")
            continue
        if count_at_score < 0 or cumulative_count < 0:
            result.skipped += 1
            result.errors.append(f"第 {idx + 2} 行：人数出现负数（{count_at_score}/{cumulative_count}），已跳过")
            continue

        existing = db.scalar(
            select(ScoreRankRow).where(
                ScoreRankRow.province == province, ScoreRankRow.year == year,
                ScoreRankRow.track == track, ScoreRankRow.score == score,
            )
        )
        record = existing or ScoreRankRow(province=province, year=year, track=track, score=score)
        if existing is None:
            db.add(record)
        record.count_at_score = count_at_score
        record.cumulative_count = cumulative_count
        if batch_column is not None:
            raw_batch = row[batch_column]
            record.batch = None if pd.isna(raw_batch) else str(raw_batch).strip() or None
        result.upserted += 1

    return result


def load_score_rank_excel(
    file_path: str, province: str, year: int, track: str, header_row: int = 0,
) -> LoadResult:
    """从真实 Excel 文件导入——官方发布的表有时候第一行是标题（比如
    "2026年江苏省普通高考物理类一分一段表"）、第二行才是真正的表头，
    这种情况用 header_row=1 跳过标题行，默认假设表头就是第一行。
    """
    df = pd.read_excel(file_path, header=header_row)
    db = SessionLocal()
    try:
        result = load_score_rank_dataframe(db, df, province, year, track)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return result


def _main() -> None:
    parser = argparse.ArgumentParser(description="导入一分一段表 Excel 到数据库")
    parser.add_argument("--file", required=True, help="Excel 文件路径")
    parser.add_argument("--province", required=True, help="省份，比如 江苏")
    parser.add_argument("--year", required=True, type=int, help="年份，比如 2026")
    parser.add_argument("--track", required=True, choices=["物理类", "历史类"], help="科类")
    parser.add_argument("--header-row", type=int, default=0, help="表头所在行（从0开始），默认0")
    args = parser.parse_args()

    result = load_score_rank_excel(args.file, args.province, args.year, args.track, header_row=args.header_row)
    print(f"共 {result.total_rows} 行，成功导入/更新 {result.upserted} 行，跳过 {result.skipped} 行。")
    if result.skipped_other_track:
        print(f"另外有 {result.skipped_other_track} 行是表里混的其他科类，已按 --track={args.track} 自动过滤，不算错误。")
    if result.errors:
        print("以下行被跳过，原因：")
        for e in result.errors[:50]:
            print(f"  {e}")
        if len(result.errors) > 50:
            print(f"  ……还有 {len(result.errors) - 50} 条，未全部打印")


if __name__ == "__main__":
    _main()
