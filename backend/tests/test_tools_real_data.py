"""
工具适配层的真实数据测试——不用脚本化模拟，直接对真实数据库跑一遍，
确认 search_candidates / get_candidate_detail / compare_candidates 这几个
工具包装后返回的数据是对的。web_search 现在真实通过 MCP 客户端接到本仓库
自带的离线参考 server（app/mcp_servers/policy_search_server.py），
这里测的是真实的端到端 MCP 调用，不是脚本模拟——跟 MCP 相关的更完整的
连接/断开/多工具场景测试在 tests/test_mcp_client.py，这里只保留
"web_search 这个工具在真实场景下能不能用"这一条，跟其他几个工具的测试
放在一起。load_skill 的测试同理放在 tests/test_skills.py。

从 scripts/test_tools_real_data.py 迁移，断言逻辑原样保留（除 web_search
那一条——原来测的是占位实现，现在已经是真实实现，断言相应更新）。
"""
from app.services.tools import ToolContext, execute_tool

from .conftest import make_profile_in


def make_ctx(db) -> ToolContext:
    return ToolContext(db=db, profile=make_profile_in())


def test_search_candidates_default_profile(db):
    ctx = make_ctx(db)
    r = execute_tool("search_candidates", {}, ctx)
    assert r.ok, f"应该成功: {r.error}"
    assert r.data["count"] == 10, f"参考考生条件下应有10个候选，实际 {r.data['count']}"


def test_search_candidates_ignores_hard_filter_overrides(db):
    """province/track/subjects/score/rank/budget_max/home_province_only 这些
    定义硬性条件的字段，现在不接受模型在单次调用里临时覆盖——这是修复
    "放宽学费上限后候选反而变少"这类不可预测行为的关键：硬性条件只能通过
    profile 的确定性刷新流程（_refresh_optional_profile_fields）写入，
    不能靠模型每次调用时自己判断要不要传。这里传一个会让候选清零的
    province 覆盖值，验证它被忽略、结果跟不传时一致（默认 profile 应有
    10 个候选）。"""
    ctx = make_ctx(db)
    r = execute_tool("search_candidates", {"province": "上海"}, ctx)
    assert r.ok, f"应该成功: {r.error}"
    assert r.data["count"] == 10, "province 覆盖应该被忽略，实际生效的还是 profile 里的江苏"


def test_search_candidates_major_keyword_and_city_pref_still_overridable(db):
    """major_keyword / city_pref 是软匹配字段，允许模型临时覆盖来换个角度
    重新查一次——不应该受上面那条硬性字段限制的影响。"""
    ctx = make_ctx(db)
    r = execute_tool("search_candidates", {"major_keyword": "不存在的专业关键词xyz", "city_pref": "南京"}, ctx)
    assert r.ok, f"应该成功: {r.error}"
    assert r.data["count"] == 10, "major_keyword/city_pref 是软匹配，不会改变硬性条件下的候选数量"


def test_search_candidates_tiers_grouped_by_category_and_quality_sorted(db):
    """tiers 字段是这次为了解决"参考概率最高的候选经常是专科/职业本科，
    把真正该冲的本科/985/211挤出候选列表"这个真实问题新加的——按保底/
    稳妥/冲刺分档、档内按学校质量优先排序，模型回复时应该直接用这个结构，
    不需要自己再判断"这些候选层次是否整齐"。这里验证：tiers 里出现的
    档位 key 都是合法的中文档位标签，且每档candidates列表跟 curate()
    独立计算出来的结果一致（不是另外一套逻辑、容易跟候选卡片区的精选
    结果不一致）。"""
    from app.services.candidate_curation import curate
    from app.services.rules_engine import search_candidates as run_search_candidates
    from app.services.tools import _load_groups

    ctx = make_ctx(db)
    r = execute_tool("search_candidates", {}, ctx)
    assert r.ok, f"应该成功: {r.error}"
    assert "tiers" in r.data

    groups = _load_groups(db)
    results, _audit = run_search_candidates(groups, ctx.profile)
    expected = curate(results)
    expected_labels = {g.category_label for g in expected.groups if g.candidates}

    assert set(r.data["tiers"].keys()) == expected_labels, (
        f"tiers 的档位 key 应该跟 curate() 算出来的一致，实际 {set(r.data['tiers'].keys())} "
        f"vs 期望 {expected_labels}"
    )
    for g in expected.groups:
        if not g.candidates:
            continue
        got_ids = [c["group_id"] for c in r.data["tiers"][g.category_label]["candidates"]]
        expected_ids = [c.group_id for c in g.candidates]
        assert got_ids == expected_ids, f"{g.category_label} 档内的候选顺序/内容应该跟 curate() 一致"


def test_get_candidate_detail_real_group_id(db):
    ctx = make_ctx(db)
    r = execute_tool("get_candidate_detail", {"group_id": "seu-cs-04"}, ctx)
    assert r.ok, f"应该成功: {r.error}"
    assert r.data["university"] == "东南大学"
    assert len(r.data["history"]) > 0


def test_get_candidate_detail_nonexistent_group_id(db):
    ctx = make_ctx(db)
    r = execute_tool("get_candidate_detail", {"group_id": "nonexistent-xyz"}, ctx)
    assert not r.ok, "不存在的group_id应该返回失败"


def test_compare_candidates_two_real_candidates(db):
    ctx = make_ctx(db)
    r = execute_tool("compare_candidates", {"group_ids": ["seu-cs-04", "nuist-cs-03"]}, ctx)
    assert r.ok, f"应该成功: {r.error}"
    assert len(r.data["comparison"]) == 2


def test_compare_candidates_single_id_fails(db):
    ctx = make_ctx(db)
    r = execute_tool("compare_candidates", {"group_ids": ["seu-cs-04"]}, ctx)
    assert not r.ok, "只传一个group_id应该报错"


def test_web_search_real_mcp_call_with_match(db):
    """真实端到端调用：通过 MCP 客户端连接到离线参考 server，用一个能命中
    数据集的关键词，验证返回的是真实结构化结果，不是占位错误。
    """
    ctx = make_ctx(db)
    r = execute_tool("web_search", {"query": "新专业"}, ctx)
    assert r.ok, f"应该成功: {r.error}"
    assert r.data["results"], "关键词应该能命中离线数据集里的内容"
    assert "source" in r.data and "离线" in r.data["source"], "返回结果应该标注数据来源，提醒这不是权威最新数据"


def test_web_search_real_mcp_call_no_match(db):
    """关键词查不到任何内容时，应该如实返回空结果+说明，而不是报错。"""
    ctx = make_ctx(db)
    r = execute_tool("web_search", {"query": "完全查不到的随机字符串xyz123"}, ctx)
    assert r.ok, f"查不到也应该是成功、只是结果为空: {r.error}"
    assert r.data["results"] == []


def test_web_search_missing_query_fails(db):
    ctx = make_ctx(db)
    r = execute_tool("web_search", {}, ctx)
    assert not r.ok
    assert "query" in r.error


def test_unknown_tool_name(db):
    ctx = make_ctx(db)
    r = execute_tool("delete_everything", {}, ctx)
    assert not r.ok


def test_internal_exception_is_caught_not_propagated(db):
    """工具内部真实抛出异常时，execute_tool 也要兜住不能让调用方崩溃。"""
    ctx = make_ctx(db)
    r = execute_tool("compare_candidates", {"group_ids": 12345}, ctx)  # 传错类型，len()会在内部真实抛TypeError
    assert not r.ok
    assert "TypeError" in r.error or "工具执行出错" in r.error
