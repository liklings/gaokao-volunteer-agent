"""
分层测评（Layered Evaluation）的通用构造函数——不落库，直接构造 Python
对象喂给 rules_engine/candidate_curation 的纯函数入口。

沿用 tests/test_rules_engine.py 里 `_make_group`/`_profile` 已经验证过的
思路（evaluate_group() 只读 group.school/group.records 等属性，不需要真的
存进数据库），这里做成可复用的公共 helper，供 tests/eval/scenarios.py
批量构造场景用，避免 300~500 条 case 每条都重复写一遍 ORM 拼装代码。
"""
from __future__ import annotations

from app.models import AdmissionGroup, AdmissionRecord, School
from app.schemas.candidate import ProfileIn

DEFAULT_PROVINCE = "江苏"


def make_school(
    school_id: str,
    name: str | None = None,
    *,
    city: str = "南京",
    province: str = DEFAULT_PROVINCE,
    tags: list[str] | None = None,
    overall_rank: int | None = None,
) -> School:
    """tags 直接决定 school_quality.get_school_quality_for_school() 判出来的
    档次（985/211/双一流/本科/专科），不依赖学校名去匹配静态参考表——这样
    场景里可以随便起虚构校名（比如 "obscure-school"），不需要凑真实存在的
    985/211 校名才能测"信息差发现"这类依赖档次的逻辑。
    """
    return School(
        id=school_id, name=name or f"{school_id}大学", city=city, province=province,
        tags=tags or [], overall_rank=overall_rank,
    )


def make_record(
    group_id: str, year: int, *, min_rank: int | None, min_score: int | None = 600,
    plan_count: int | None = 10, group_name: str | None = None,
) -> AdmissionRecord:
    return AdmissionRecord(
        group_id=group_id, year=year, group_name=group_name or group_id,
        plan_count=plan_count, min_score=min_score, min_rank=min_rank,
        source_url=None, fetched_at=None,
    )


def make_group(
    group_id: str,
    school: School,
    *,
    track: str = "物理类",
    subjects_required: list[str] | None = None,
    major_keywords: list[str] | None = None,
    tuition: int | None = None,
    admission_category: str | None = None,
    code: str | None = None,
    records: list[AdmissionRecord] | None = None,
    min_rank: int | None = None,
    group_name: str | None = None,
) -> AdmissionGroup:
    """records 和 min_rank 二选一：大多数场景只关心"这条候选去年最低位次
    是多少"，min_rank 是给这种最常见情况的简写（等价于传一条 2025 年的
    AdmissionRecord）；需要构造多年历史数据（比如 Layer 5 历史回测场景）时
    直接传 records。
    """
    group = AdmissionGroup(
        id=group_id, school_id=school.id, code=code, track=track,
        subjects_required=subjects_required or [], major_keywords=major_keywords or [],
        tuition=tuition, duration=None, why_recommend="", caution="", source_label="",
        admission_category=admission_category,
    )
    group.school = school
    if records is not None:
        group.records = records
    elif min_rank is not None:
        group.records = [make_record(group_id, 2025, min_rank=min_rank, group_name=group_name)]
    else:
        group.records = []
    return group


def make_profile(**overrides) -> ProfileIn:
    """默认一个"正常"考生：江苏、物理类、物化组合、630分/5000名，没有任何
    额外偏好开关——场景只需要覆盖跟该场景相关的字段，其余用这份默认值，
    减少每条 case 里的样板代码，也让"这条 case 到底在测什么差异"更显眼。
    """
    base = dict(
        province=DEFAULT_PROVINCE, track="物理类", subjects=["物理", "化学"],
        score=630, rank=5000,
    )
    base.update(overrides)
    return ProfileIn(**base)
