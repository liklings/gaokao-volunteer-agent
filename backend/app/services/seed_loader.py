"""
把 seed_data/jiangsu_physics_seed.json 灌进数据库。

用法（在 backend/ 目录下，激活 venv 之后）：
    python -m app.services.seed_loader

建议每次更新种子数据文件后，先跑一遍 scripts/validate_seed_data.py 校验
一下再导入——这个脚本本身不做任何数据合法性检查（比如分数超出0~750的
合理范围、科目名称拼错、重复的year/group_id 都不会被这里拦下来，只会
被 upsert 逻辑悄悄覆盖或者原样写进数据库），校验和导入刻意分成两个独立
脚本，校验出问题不该在导入的时候才发现。

设计成幂等：重复运行不会插入重复数据——
学校/专业组按 id upsert（存在就更新字段，不存在就插入），
录取记录按 (group_id, year) upsert。这样以后你更新种子数据文件后，
直接重新跑一次这个脚本就行，不用先手动清库。
"""
import json
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import AdmissionGroup, AdmissionRecord, School

SEED_FILE = Path(__file__).resolve().parent.parent / "seed_data" / "jiangsu_physics_seed.json"


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def upsert_school(db: Session, data: dict) -> School:
    school = db.get(School, data["id"])
    if school is None:
        school = School(id=data["id"])
        db.add(school)
    school.name = data["name"]
    school.city = data["city"]
    school.province = data["province"]
    school.tags = data.get("tags", [])
    school.overall_rank = data.get("overall_rank")
    return school


def upsert_group(db: Session, school_id: str, data: dict) -> AdmissionGroup:
    group = db.get(AdmissionGroup, data["id"])
    if group is None:
        group = AdmissionGroup(id=data["id"], school_id=school_id)
        db.add(group)
    group.school_id = school_id
    group.code = data.get("code")
    group.track = data["track"]
    group.subjects_required = data.get("subjects_required", [])
    group.major_keywords = data.get("major_keywords", [])
    group.tuition = data.get("tuition")
    group.duration = data.get("duration")
    group.admission_category = data.get("admission_category")
    group.why_recommend = data.get("why_recommend") or ""
    group.caution = data.get("caution") or ""
    group.source_label = data.get("source_label") or ""
    return group


def upsert_record(db: Session, group_id: str, data: dict) -> AdmissionRecord:
    existing = db.execute(
        select(AdmissionRecord).where(
            AdmissionRecord.group_id == group_id, AdmissionRecord.year == data["year"]
        )
    ).scalar_one_or_none()
    record = existing or AdmissionRecord(group_id=group_id, year=data["year"])
    if existing is None:
        db.add(record)
    record.group_name = data.get("group_name")
    record.plan_count = data.get("plan_count")
    record.min_score = data.get("min_score")
    record.min_rank = data.get("min_rank")
    record.source_url = data.get("source_url")
    record.fetched_at = _parse_date(data.get("fetched_at"))
    return record


def run() -> dict:
    if not SEED_FILE.exists():
        raise FileNotFoundError(f"找不到种子数据文件：{SEED_FILE}")

    payload = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    schools_data = payload.get("schools", [])

    counts = {"schools": 0, "admission_groups": 0, "admission_records": 0}
    db = SessionLocal()
    try:
        for school_data in schools_data:
            upsert_school(db, school_data)
            counts["schools"] += 1
            for group_data in school_data.get("admission_groups", []):
                upsert_group(db, school_data["id"], group_data)
                counts["admission_groups"] += 1
                for record_data in group_data.get("history", []):
                    upsert_record(db, group_data["id"], record_data)
                    counts["admission_records"] += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    return counts


if __name__ == "__main__":
    result = run()
    print(f"导入完成：{result['schools']} 所学校，{result['admission_groups']} 个专业组，"
          f"{result['admission_records']} 条年度录取记录。")
