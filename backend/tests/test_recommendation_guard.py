"""
底线策略校验网关（app/services/recommendation_guard.py）的单元测试——
直接对真实数据库跑，复用 rules_engine.evaluate_group 已经验证过的判断，
这里只验证"把校验结果组织成 violations/warnings"这一层逻辑本身是对的。
"""
from app.services.recommendation_guard import validate_recommendation
from app.services.tools import ToolContext, execute_tool

from .conftest import make_profile_in


def test_empty_list_fails():
    result = validate_recommendation([], make_profile_in(), db=None)
    assert not result.passed
    assert result.violations


def test_unknown_group_id_fails(db):
    result = validate_recommendation(["does-not-exist-xyz"], make_profile_in(), db)
    assert not result.passed
    assert any("does-not-exist-xyz" in v.group_id for v in result.violations)


def test_duplicate_group_id_fails(db):
    result = validate_recommendation(["seu-cs-04", "seu-cs-04"], make_profile_in(), db)
    assert not result.passed
    assert any("重复" in v.reason for v in result.violations)


def test_real_matching_candidates_pass(db):
    """跟考生条件（省份/科类/选科/预算）都匹配的真实候选，应该通过校验。"""
    result = validate_recommendation(["seu-cs-04", "nuist-cs-03"], make_profile_in(), db)
    assert result.passed, f"应该通过: {[ (v.group_id, v.reason) for v in result.violations ]}"
    assert len(result.candidates) == 2


def test_province_mismatch_fails(db):
    """考生目标省份跟候选实际招生省份不一致——硬性规则不通过，必须判定校验失败。"""
    profile = make_profile_in(province="上海")
    result = validate_recommendation(["seu-cs-04"], profile, db)
    assert not result.passed
    assert any("seu-cs-04" == v.group_id for v in result.violations)


def test_subject_requirement_mismatch_fails(db):
    """考生选科不满足专业组要求的候选，必须判定校验失败——跟 rules_engine
    里"选科要求"这条硬性规则完全对应，不是这里额外发明的规则。
    """
    profile = make_profile_in(subjects=["历史", "政治"])  # 明显跟理工科专业组选科要求对不上
    result = validate_recommendation(["seu-cs-04"], profile, db)
    assert not result.passed


def test_budget_exceeded_fails(db):
    profile = make_profile_in(budget_max=1)  # 预算低到不可能有专业组满足
    result = validate_recommendation(["seu-cs-04"], profile, db)
    assert not result.passed


def test_max_list_size_exceeded_fails(db):
    ctx = ToolContext(db=db, profile=make_profile_in())
    all_candidates = execute_tool("search_candidates", {}, ctx)
    group_ids = [c["group_id"] for c in all_candidates.data["candidates"]]
    assert len(group_ids) >= 10, "种子数据应该至少有10个候选才能测'超出上限'这个场景"

    result = validate_recommendation(group_ids, make_profile_in(), db)
    assert not result.passed
    assert any("最多推荐" in v.reason for v in result.violations)


def test_structural_risk_warning_for_all_extreme_chong(db):
    """考生位次极差（rank 数字很大）时，同一批候选相对于考生的把握都会被
    打到'极冲'区间——名单里全是这种候选、没有任何稳妥/保底选项时，
    校验应该仍然通过（这不是硬性规则问题），但要带上结构性风险警告。
    """
    ctx = ToolContext(db=db, profile=make_profile_in(rank=999999))
    all_candidates = execute_tool("search_candidates", {}, ctx)
    group_ids = [c["group_id"] for c in all_candidates.data["candidates"][:3]]

    result = validate_recommendation(group_ids, make_profile_in(rank=999999), db)
    assert result.passed, f"结构性风险不该导致校验不通过: {result.violations}"
    assert any("冲刺" in w.reason or "保底" in w.reason for w in result.warnings), (
        f"应该有结构性风险警告，实际 warnings={result.warnings}"
    )


def test_tool_finalize_recommendation_matches_guard_result(db):
    """工具层包装（_tool_finalize_recommendation，通过 execute_tool 间接测）
    跟直接调用 validate_recommendation 的结果应该一致——工具层只是包了一层，
    不应该悄悄改变校验结论。
    """
    ctx = ToolContext(db=db, profile=make_profile_in())
    ok_result = execute_tool("finalize_recommendation", {"group_ids": ["seu-cs-04", "nuist-cs-03"]}, ctx)
    assert ok_result.ok
    assert set(ok_result.data["validated_group_ids"]) == {"seu-cs-04", "nuist-cs-03"}

    bad_result = execute_tool("finalize_recommendation", {"group_ids": ["does-not-exist-xyz"]}, ctx)
    assert not bad_result.ok
    assert "does-not-exist-xyz" in bad_result.error


def test_tool_finalize_recommendation_missing_group_ids(db):
    ctx = ToolContext(db=db, profile=make_profile_in())
    result = execute_tool("finalize_recommendation", {}, ctx)
    assert not result.ok
    assert "group_ids" in result.error
