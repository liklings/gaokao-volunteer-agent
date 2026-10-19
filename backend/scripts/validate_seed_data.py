"""
种子数据健全性校验。跟 app/services/seed_loader.py 分开成两个独立脚本——
校验不该悄悄埋在导入流程里，导入新一年数据前应该先单独跑一遍这个脚本，
看清楚"有几条结构错误必须先改、有几条业务警告建议人工核实"，而不是让
脏数据在导入的时候悄悄溜进数据库、等前端把它展示给一个真实考生看了才发现。

这份数据直接决定给考生的报考建议是否可信，比这个项目里大多数代码问题
后果都更实际，所以校验分两级、而不是一次性做成一道"过/不过"的关卡：

【结构层（ERROR，阻断）】必填字段是否齐全、类型对不对、数值是否在物理上
合理的范围内（比如高考分数不可能是负数或者超过750）、id 是否重复。任何
一条 ERROR 都不应该被导入——这不是"数据可能有问题"，是"数据本身就不合法"。

【业务合理性层（WARNING，不阻断）】比如同一个专业组跨年份的分数/学费
出现了不合常理的剧烈变化——可能是数据源本身出错，也可能是真的有调整，
但至少要有人看一眼、去官方数据源核实一下，不能假装没看见就直接导入。

用法（backend/ 目录下，激活 venv）：
    python scripts/validate_seed_data.py                          # 校验默认的种子文件
    python scripts/validate_seed_data.py path/to/other_seed.json   # 校验指定文件
退出码：有 ERROR 时返回 1（可以接进 CI/导入流程当作硬性关卡），
        只有 WARNING 或完全干净时返回 0。
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

DEFAULT_SEED_FILE = Path(__file__).resolve().parent.parent / "app" / "seed_data" / "jiangsu_physics_seed.json"

# 目前实际出现过的科目；新高考"3+1+2"模式下这是完整的可选科目集合，
# 超出这个集合大概率是录入时打错字了（比如"物理类"误填进了 subjects_required）。
KNOWN_SUBJECTS = {"物理", "历史", "化学", "生物", "政治", "地理", "技术"}
KNOWN_TRACKS = {"物理类", "历史类"}

MIN_PLAUSIBLE_YEAR = 2018  # 更早的数据参考价值很低，大概率是录入错误
MAX_SCORE = 750  # 现行新高考满分（含选考科目），超过这个数字必然是录入错误
MIN_SCORE = 0

# 业务警告的阈值——不是精确的统计学判断，是"值得一个人看一眼"的粗略基准。
SCORE_JUMP_WARN_PCT = 0.05   # 同一专业组相邻两年分数变化超过5%
TUITION_JUMP_WARN_PCT = 0.20  # 同一专业组相邻两年学费变化超过20%


@dataclass
class Issue:
    level: str  # "ERROR" | "WARNING"
    location: str  # 定位到具体是哪个学校/专业组/年份的哪条记录，方便去 JSON 里找
    message: str


@dataclass
class ValidationReport:
    schools: int = 0
    groups: int = 0
    records: int = 0
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "ERROR"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "WARNING"]

    def error(self, location: str, message: str) -> None:
        self.issues.append(Issue(level="ERROR", location=location, message=message))

    def warn(self, location: str, message: str) -> None:
        self.issues.append(Issue(level="WARNING", location=location, message=message))


def _require_str(report: ValidationReport, location: str, data: dict, field_name: str) -> None:
    value = data.get(field_name)
    if not isinstance(value, str) or not value.strip():
        report.error(location, f"字段 '{field_name}' 缺失或不是非空字符串（实际值：{value!r}）")


def _validate_school(report: ValidationReport, school: dict) -> None:
    school_id = school.get("id", "<无id>")
    location = f"school[id={school_id}]"
    for f in ("id", "name", "city", "province"):
        _require_str(report, location, school, f)


def _validate_group(report: ValidationReport, school_id: str, group: dict, seen_group_ids: set[str]) -> None:
    group_id = group.get("id", "<无id>")
    location = f"school[{school_id}] > group[id={group_id}]"

    _require_str(report, location, group, "id")
    if isinstance(group_id, str):
        if group_id in seen_group_ids:
            report.error(location, f"group_id 重复：'{group_id}' 在文件里出现了不止一次——"
                                    f"seed_loader.py 按 id upsert，重复的 id 会导致后一条覆盖前一条，"
                                    f"数据会静默丢失而不是报错。")
        seen_group_ids.add(group_id)

    track = group.get("track")
    if track not in KNOWN_TRACKS:
        report.error(location, f"track 不合法：{track!r}，应该是 {sorted(KNOWN_TRACKS)} 之一")

    subjects = group.get("subjects_required")
    if not isinstance(subjects, list) or not subjects:
        report.error(location, "subjects_required 缺失或为空列表——考生的选科匹配规则依赖这个字段")
    else:
        bad_subjects = [s for s in subjects if s not in KNOWN_SUBJECTS]
        if bad_subjects:
            report.error(location, f"subjects_required 里出现不认识的科目：{bad_subjects}，"
                                    f"可能是录入笔误（已知科目集合：{sorted(KNOWN_SUBJECTS)}）")

    tuition = group.get("tuition")
    if tuition is not None and (not isinstance(tuition, int) or tuition < 0):
        report.error(location, f"tuition 应该是非负整数，实际值：{tuition!r}")

    history = group.get("history")
    if not isinstance(history, list) or not history:
        report.error(location, "history 缺失或为空——没有任何一年的录取数据，这个专业组对考生没有参考价值")
        return

    seen_years: dict[int, dict] = {}
    for record in history:
        _validate_record(report, location, record, seen_years)

    _check_year_over_year(report, location, seen_years)


def _validate_record(report: ValidationReport, group_location: str, record: dict, seen_years: dict[int, dict]) -> None:
    year = record.get("year")
    location = f"{group_location} > year={year}"

    if not isinstance(year, int) or not (MIN_PLAUSIBLE_YEAR <= year <= date.today().year + 1):
        report.error(location, f"year 不合理：{year!r}（应该是 {MIN_PLAUSIBLE_YEAR}~{date.today().year + 1} 之间的整数）")
    elif year in seen_years:
        report.error(location, f"同一个专业组里 year={year} 出现了不止一条记录——"
                                f"seed_loader.py 按 (group_id, year) upsert，重复年份会互相覆盖")
    else:
        seen_years[year] = record

    min_score = record.get("min_score")
    if min_score is not None and (not isinstance(min_score, int) or not (MIN_SCORE <= min_score <= MAX_SCORE)):
        report.error(location, f"min_score 超出合理范围：{min_score!r}（应该是 {MIN_SCORE}~{MAX_SCORE} 之间的整数）")

    min_rank = record.get("min_rank")
    if min_rank is not None and (not isinstance(min_rank, int) or min_rank <= 0):
        report.error(location, f"min_rank 应该是正整数，实际值：{min_rank!r}")

    plan_count = record.get("plan_count")
    if plan_count is not None and (not isinstance(plan_count, int) or plan_count <= 0):
        report.error(location, f"plan_count 应该是正整数，实际值：{plan_count!r}")


def _check_year_over_year(report: ValidationReport, group_location: str, seen_years: dict[int, dict]) -> None:
    """业务警告：相邻两年（按年份排序后紧挨着的两条，不要求连续）分数/学费出现
    不合常理的剧烈跳变，标记出来人工核实——不阻断，因为确实可能是真实调整
    （比如某年扩招/缩招、专业组重新拆分），但至少要有人确认不是数据源本身出错。
    """
    years_sorted = sorted(seen_years.keys())
    for prev_year, curr_year in zip(years_sorted, years_sorted[1:]):
        prev, curr = seen_years[prev_year], seen_years[curr_year]

        prev_score, curr_score = prev.get("min_score"), curr.get("min_score")
        if isinstance(prev_score, int) and isinstance(curr_score, int) and prev_score > 0:
            change_pct = abs(curr_score - prev_score) / prev_score
            if change_pct > SCORE_JUMP_WARN_PCT:
                report.warn(
                    f"{group_location} > year={prev_year}~{curr_year}",
                    f"min_score 从 {prev_score} 变到 {curr_score}（变化 {change_pct:.1%}），"
                    f"超过 {SCORE_JUMP_WARN_PCT:.0%} 的提醒阈值，建议核实是否数据错误。",
                )

    # 学费理论上同一个专业组年年应该一致或者只小幅调整，这里单独拿最新两年比，
    # 不是跟 min_score 一样逐对比较——学费不像分数每年都变，只要最新的没有
    # 明显跳变就够了。
    if len(years_sorted) >= 2:
        prev, curr = seen_years[years_sorted[-2]], seen_years[years_sorted[-1]]
        prev_tuition, curr_tuition = prev.get("tuition"), curr.get("tuition")
        # tuition 是 group 级别字段不是 record 级别，这里这两个 get 大概率都是 None，
        # 留着这个分支是因为以后如果 tuition 改成按年记录，这里不用重写检查逻辑。
        if isinstance(prev_tuition, int) and isinstance(curr_tuition, int) and prev_tuition > 0:
            change_pct = abs(curr_tuition - prev_tuition) / prev_tuition
            if change_pct > TUITION_JUMP_WARN_PCT:
                report.warn(
                    f"{group_location} > year={years_sorted[-2]}~{years_sorted[-1]}",
                    f"学费从 {prev_tuition} 变到 {curr_tuition}（变化 {change_pct:.1%}），建议核实。",
                )


def validate(payload: dict) -> ValidationReport:
    report = ValidationReport()
    schools = payload.get("schools", [])
    seen_school_ids: set[str] = set()
    seen_group_ids: set[str] = set()

    for school in schools:
        report.schools += 1
        _validate_school(report, school)

        school_id = school.get("id")
        if isinstance(school_id, str):
            if school_id in seen_school_ids:
                report.error(f"school[id={school_id}]", f"school id 重复：'{school_id}'")
            seen_school_ids.add(school_id)

        for group in school.get("admission_groups", []):
            report.groups += 1
            _validate_group(report, school_id or "<无id>", group, seen_group_ids)
            report.records += len(group.get("history") or [])

    return report


def main() -> int:
    seed_file = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SEED_FILE
    if not seed_file.exists():
        print(f"找不到文件：{seed_file}")
        return 1

    payload = json.loads(seed_file.read_text(encoding="utf-8"))
    report = validate(payload)

    print(f"校验文件：{seed_file}")
    print(f"共 {report.schools} 所学校，{report.groups} 个专业组，{report.records} 条年度录取记录。\n")

    if report.errors:
        print(f"❌ {len(report.errors)} 条结构错误（必须先修，不能导入）：")
        for issue in report.errors:
            print(f"  [{issue.location}] {issue.message}")
        print()

    if report.warnings:
        print(f"⚠️  {len(report.warnings)} 条业务警告（建议核实，不阻断导入）：")
        for issue in report.warnings:
            print(f"  [{issue.location}] {issue.message}")
        print()

    if not report.errors and not report.warnings:
        print("✅ 没有发现问题。")
    elif not report.errors:
        print("✅ 没有结构错误，可以放心导入；上面的警告建议抽空核实一下。")
    else:
        print("🛑 存在结构错误，请先修正上面列出的问题，再执行 seed_loader 导入。")

    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
