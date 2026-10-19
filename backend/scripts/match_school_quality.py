"""
把数据库里真实的 School.name（从你的招生 Excel 导入的院校名称），跟
app/data/school_quality_source.json（《全国高等院校信息汇总》+ U.S.News
世界大学排名整理出来的学校质量参考数据）按名字匹配一遍，看看能覆盖多少、
哪些对不上号——这是"好学校优先展示/差学校折叠"这个功能落地前的必要一步：
先确认数据能不能接得上，接不上的名单要么手动改名对齐，要么这些学校就先
没有质量标签（不代表数据错，只是这份参考表没收录到，比如非常小的独立
学院、中外合作办学机构的曾用名等）。

用法（在你自己电脑上，backend/ 目录下，激活你平时跑 uvicorn 那个 venv）：
    python -m scripts.match_school_quality

不修改数据库、不改任何业务代码，只读 School 表、只打印匹配报告到终端，
同时把完整的匹配结果（含每一所学校匹配到了什么/有没有匹配上）写到
scripts/school_quality_match_report.json，方便你逐条看有问题的名字。

匹配策略分两轮，避免"完全匹配不上就当没有"这种一刀切：
1. 精确匹配：School.name 跟参考表里的 name 字段完全相同（去除首尾空白）。
2. 规整匹配：精确匹配失败的，把两边名字都去掉括号里的内容（比如
   "苏州大学（文正学院）"里的"（文正学院）"、"南京大学(苏州校区)"里的
   "(苏州校区)"）、统一全半角括号和空格后再比一次。规整匹配成功的会在
   报告里单独标出来（is_fuzzy: true），需要你抽查几个确认没有张冠李戴
   （比如把独立学院错误地匹配成了它的主体大学，质量标签会失真）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from app.db import SessionLocal
from app.models import School

DATA_FILE = Path(__file__).resolve().parent.parent / "app" / "data" / "school_quality_source.json"
REPORT_FILE = Path(__file__).resolve().parent / "school_quality_match_report.json"

_BRACKET_RE = re.compile(r"[（(].*?[）)]")


def normalize_name(name: str) -> str:
    """去掉括号里的内容（校区/学院分支说明）+ 统一全半角括号 + 去空白，
    只用于"精确匹配失败后"的第二轮兜底匹配——不能作为第一优先级，
    因为有些学校的括号内容其实是区分不同学校的关键信息（比如中外合作
    办学机构常常就是靠括号后缀跟主体院校区分开的）。
    """
    s = name.strip()
    s = _BRACKET_RE.sub("", s)
    return s.strip()


def load_quality_data() -> list[dict]:
    with open(DATA_FILE, encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    quality_records = load_quality_data()
    exact_index: dict[str, dict] = {}
    fuzzy_index: dict[str, list[dict]] = {}
    for rec in quality_records:
        exact_index[rec["name"]] = rec
        fuzzy_index.setdefault(normalize_name(rec["name"]), []).append(rec)

    db = SessionLocal()
    try:
        db_schools = db.query(School).order_by(School.name).all()
    finally:
        db.close()

    print(f"参考数据：{len(quality_records)} 条（app/data/school_quality_source.json）")
    print(f"数据库院校：{len(db_schools)} 所（去重前，按 School 表主键计）")
    print()

    exact_hits = 0
    fuzzy_hits = 0
    misses: list[str] = []
    report: list[dict] = []

    for school in db_schools:
        name = school.name.strip()
        matched = exact_index.get(name)
        is_fuzzy = False
        if matched is None:
            candidates = fuzzy_index.get(normalize_name(name))
            if candidates and len(candidates) == 1:
                matched = candidates[0]
                is_fuzzy = True
            elif candidates and len(candidates) > 1:
                # 规整后撞上了不止一条参考数据（比如"XX大学"跟"XX大学(分校)"
                # 规整后变成同一个 key）——这种情况不敢自动选一个，算未匹配，
                # 交给人工确认该对应哪一条。
                matched = None

        if matched is not None:
            exact_hits += 0 if is_fuzzy else 1
            fuzzy_hits += 1 if is_fuzzy else 0
            report.append({
                "db_school_id": school.id,
                "db_name": name,
                "db_city": school.city,
                "db_province": school.province,
                "matched": True,
                "is_fuzzy": is_fuzzy,
                "quality_name": matched["name"],
                "is_985": bool(matched["is_985"]),
                "is_211": bool(matched["is_211"]),
                "is_shuangyiliu": bool(matched["is_shuangyiliu"]),
                "level": matched["level"],
                "overall_rank": matched["overall_rank"],
                "usnews_world_rank": matched["usnews_world_rank"],
            })
        else:
            misses.append(name)
            report.append({
                "db_school_id": school.id,
                "db_name": name,
                "db_city": school.city,
                "db_province": school.province,
                "matched": False,
            })

    total = len(db_schools)
    matched_total = exact_hits + fuzzy_hits
    print(f"精确匹配：{exact_hits} 所")
    print(f"规整匹配（去括号后缀匹配到，建议抽查）：{fuzzy_hits} 所")
    print(f"完全没匹配上：{len(misses)} 所")
    if total:
        print(f"总覆盖率：{matched_total}/{total} = {matched_total / total:.1%}")
    print()

    if misses:
        print("没匹配上的院校名单（这些学校暂时不会有质量标签，不影响它们照常出现在候选列表里）：")
        for name in misses:
            print(f"  - {name}")
        print()

    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"完整报告已写入：{REPORT_FILE}")


if __name__ == "__main__":
    main()
