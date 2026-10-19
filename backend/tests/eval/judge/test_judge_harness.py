"""评审流程自检——不调 Gemini，注入假评审员。"""
import asyncio
import json

from ..ratelimit import json_cached
from .calibration import GOLD, agreement
from .checks import check_answer
from .judge import JudgeItem, aggregate, build_prompt, judge_all, judge_item, parse_verdict
from .rubrics import PASS_SCORE, RUBRICS
from .targets import _summarize_tool_data, advisor_item_from_run


def _fake_reply(kind, score=5, na=(), overrides=None):
    rows = [{"key": c.key, "applicable": c.key not in na, "reason": "r", "score": (overrides or {}).get(c.key, score)}
            for c in RUBRICS[kind]]
    return json.dumps({"criteria": rows}, ensure_ascii=False)


def _item(kind="profile_reply"):
    return JudgeItem("x", kind, {"本轮用户输入": "我是江苏考生"}, "请问你选科是什么？")


def test_prompt_contains_facts_reply_rubric_and_anti_bias_rules():
    p = build_prompt(_item())
    assert "我是江苏考生" in p and "请问你选科是什么？" in p
    assert all(c.key in p for c in RUBRICS["profile_reply"])
    assert "不要因为回复更长" in p and "只能依据【背景事实】" in p


def test_parse_valid_and_invalid():
    it = _item()
    v = parse_verdict(it, _fake_reply("profile_reply", 4))
    assert not v.error and v.passed("ask_fit") is True
    assert parse_verdict(it, "not json").error
    assert "缺" in parse_verdict(it, json.dumps({"criteria": [{"key": "ask_fit", "applicable": True, "reason": "", "score": 5}]})).error
    bad = json.loads(_fake_reply("profile_reply"))
    bad["criteria"][0]["score"] = 9
    assert "越界" in parse_verdict(it, json.dumps(bad)).error
    extra = json.loads(_fake_reply("profile_reply"))
    extra["criteria"].append({"key": "zzz", "applicable": True, "reason": "", "score": 5})
    assert "多" in parse_verdict(it, json.dumps(extra)).error


def test_na_only_allowed_for_na_capable_criteria():
    it = JudgeItem("a", "advisor_answer", {}, "回复")
    v = parse_verdict(it, _fake_reply("advisor_answer", 5, na=("discovery_value", "grounded")))
    assert v.passed("discovery_value") is None      # 允许 N/A 的维度
    assert v.passed("grounded") is True             # 不允许 N/A 的维度被强制按适用处理


def test_judge_failure_is_recorded_not_dropped():
    async def boom(_):
        raise RuntimeError("503")
    v = asyncio.run(judge_item(_item(), boom))
    assert v.error.startswith("RuntimeError")
    agg = aggregate([v])
    assert agg["n_error"] == 1 and agg["by_criterion"] == {}


def test_aggregate_pass_rate_and_na_excluded():
    items = [JudgeItem(str(i), "advisor_answer", {}, "r") for i in range(3)]
    replies = iter([_fake_reply("advisor_answer", 5), _fake_reply("advisor_answer", 3, na=("discovery_value",)),
                    _fake_reply("advisor_answer", 4)])

    async def fn(_):
        return next(replies)
    agg = aggregate(asyncio.run(judge_all(items, fn, concurrency=1)))
    assert agg["by_criterion"]["grounded"]["n"] == 3
    assert agg["by_criterion"]["grounded"]["pass_rate"] == round(2 / 3, 3)
    assert agg["by_criterion"]["discovery_value"]["n"] == 2  # N/A 不计入


def test_calibration_gold_labels_are_valid_and_oracle_judge_is_reliable():
    ids = [g[0].id for g in GOLD]
    assert len(ids) == len(set(ids)) and len(GOLD) >= 15
    for item, labels in GOLD:
        assert set(labels) <= {c.key for c in RUBRICS[item.kind]}
    assert any(not v for _, lab in GOLD for v in lab.values()) and any(v for _, lab in GOLD for v in lab.values())

    by_prompt = {build_prompt(i): lab for i, lab in GOLD}

    async def oracle(prompt):  # 完美评审员：按人工标注给 5/1 分，没标注的维度给 5
        item = next(i for i, _ in GOLD if build_prompt(i) == prompt)
        lab = by_prompt[prompt]
        return _fake_reply(item.kind, 5, overrides={k: (5 if v else 1) for k, v in lab.items()})
    rep = agreement(asyncio.run(judge_all([g[0] for g in GOLD], oracle)))
    assert rep["agreement"] == 1.0 and rep["reliable"]


def test_calibration_detects_a_lenient_judge():
    async def lenient(prompt):  # 什么都给 5 分的"老好人"评审员
        kind = "advisor_answer" if "工具结果" in prompt else "profile_reply"
        return _fake_reply(kind, 5)
    rep = agreement(asyncio.run(judge_all([g[0] for g in GOLD], lenient)))
    assert rep["false_pass"] > 0 and not rep["reliable"]


def test_checks_probability_grounding_and_discovery():
    data = {"tiers": {"稳妥": {"total_in_category": 1, "candidates": [{"university": "苏州大学"}]}}, "discoveries": [{"university": "石河子大学"}], "candidates": []}
    ok = check_answer("稳妥：苏州大学。你可能没注意到：石河子大学。", data)
    assert ok["no_probability"] and ok["discoveries_mentioned"] and ok["grounded_schools"]
    bad = check_answer("苏州大学大概70%的把握；还有复旦大学。", data)
    assert not bad["no_probability"] and not bad["discoveries_mentioned"]
    assert bad["ungrounded"] == ["复旦大学"] and not bad["grounded_schools"]
    # 定性的「把握较高」不算违规；「录取概率」「极大概率」「七成把握」要抓
    assert check_answer("稳妥组录取把握较高。", data)["no_probability"]
    for phrase in ("录取概率相对稳妥", "极大概率被录取", "有七成把握"):
        assert not check_answer(phrase, data)["no_probability"], phrase


def test_advisor_item_summary_truncates_and_keeps_checks():
    # 真实结构：每档是 {"total_in_category", "candidates": [...]}（首次真实运行时因按列表处理而 KeyError）
    data = {"tiers": {"稳妥": {"total_in_category": 10, "candidates": [{"university": f"学校{i}大学", "rank_diff": i} for i in range(10)]}},
            "discoveries": [], "note": "n"}
    summ = _summarize_tool_data(data)
    assert len(summ["tiers"]["稳妥"]) == 6 and summ["各档候选总数"] == {"稳妥": 10}
    legacy = {"tiers": {"稳妥": [{"university": "苏州大学"}]}, "discoveries": []}   # 兼容旧的简化列表形态
    assert _summarize_tool_data(legacy)["tiers"]["稳妥"] == [{"university": "苏州大学"}]
    it = advisor_item_from_run("t", {"rank": 1}, "稳妥：学校1大学", data)
    assert it.extra["checks"]["no_probability"] and it.id == "adv-t"


def test_json_cached_skips_repeat_calls_and_does_not_cache_failures(tmp_path):
    calls = {"n": 0}

    async def fn(p):
        calls["n"] += 1
        if p == "bad":
            raise RuntimeError("x")
        return p.upper()
    c = json_cached(fn, tmp_path / "c.json")
    assert asyncio.run(c("a")) == "A" and asyncio.run(c("a")) == "A" and calls["n"] == 1
    for _ in range(2):
        try:
            asyncio.run(c("bad"))
        except RuntimeError:
            pass
    assert calls["n"] == 3   # 失败的调用不缓存，下次会重试


def test_cleanup_profile_removes_runs_steps_messages_and_profile(db):
    """回归：首次真实运行时，混用 db.delete(对象) 与批量删除，会因 autoflush=False 触发
    agent_runs_profile_id_fkey 外键冲突。这里用真实库按同样的数据形态验证清理顺序。"""
    from app.models import ChatMessage, Profile
    from app.models.agent_run import AgentRun
    from app.models.agent_step import AgentStep
    from .targets import cleanup_profile

    p = Profile(province="江苏", track="物理类")
    db.add(p)
    db.commit()
    db.refresh(p)
    db.add(ChatMessage(profile_id=p.id, role="user", content="hi"))
    run = AgentRun(profile_id=p.id, agent_name="retrieval", status="completed", total_steps=1)
    db.add(run)
    db.commit()
    db.refresh(run)
    db.add(AgentStep(run_id=run.id, step_index=0, action_type="final_answer", status="ok"))
    db.commit()

    pid, rid = p.id, run.id
    cleanup_profile(db, pid)

    assert db.query(Profile).filter(Profile.id == pid).count() == 0
    assert db.query(AgentRun).filter(AgentRun.profile_id == pid).count() == 0
    assert db.query(ChatMessage).filter(ChatMessage.profile_id == pid).count() == 0
    assert db.query(AgentStep).filter(AgentStep.run_id == rid).count() == 0


def test_checks_tolerate_paren_variants_and_generic_phrases():
    data = {"tiers": {}, "discoveries": [{"university": "华北电力大学(保定)"}, {"university": "中国矿业大学(北京)"}], "candidates": []}
    ok = check_answer("信息差：华北电力大学（保定）和中国矿业大学（北京）都是好学校，属于985工程大学、双一流高水平大学之列。", data)
    assert ok["discoveries_mentioned"] and ok["grounded_schools"] and not ok["ungrounded"]
    miss = check_answer("信息差：华北电力大学（保定）。", data)
    assert miss["discoveries_missing"] == ["中国矿业大学(北京)"]


def test_stale_cache_warning(tmp_path):
    import os
    import time
    from .run_judge import stale_cache_warning
    cache = tmp_path / "c.json"
    assert stale_cache_warning(cache) is None  # 缓存不存在：不警告
    cache.write_text("{}")
    os.utime(cache, (time.time() - 10 ** 7, time.time() - 10 ** 7))  # 远早于提示词文件
    assert "旧" in stale_cache_warning(cache)
    os.utime(cache, (time.time() + 100, time.time() + 100))
    assert stale_cache_warning(cache) is None
