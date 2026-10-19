"""Slot F1 评分器/运行器的自检——不调 Gemini。"""
import asyncio

from .cases import CASES, Case
from .metrics import missing_required, score_case, summarize, values_match
from .runner import run_all, run_case


def test_cases_are_wellformed():
    assert len(CASES) >= 150
    for c in CASES:
        assert c.turns and all(t.strip() for t in c.turns)
        for i in c.followup_turns:
            assert 0 <= i < len(c.turns)


def test_values_match_is_lenient_but_bounded():
    assert values_match("province", "江苏", "江苏省")
    assert values_match("track", "物理类", "物理")
    assert values_match("subjects", ["物理", "化学", "生物"], ["生物", "物理", "化学"])
    assert values_match("subjects", ["历史", "政治", "地理"], ["历史", "思想政治", "地理"])
    assert not values_match("subjects", ["物理", "化学"], ["物理", "化学", "生物"])
    assert values_match("major_keyword", "计算机", "计算机类")
    assert not values_match("major_keyword", "计算机", "法学")
    assert not values_match("score", 610, 601)
    assert not values_match("accept_sino_foreign", True, False)


def test_score_case_counts_hallucination_and_miss():
    c = score_case({"score": 610, "rank": 8000}, {"score": 610, "rank": 9000, "city_pref": "上海"})
    assert (c["score"].tp, c["score"].fp, c["score"].fn) == (1, 0, 0)
    assert (c["rank"].tp, c["rank"].fp, c["rank"].fn) == (0, 1, 1)   # 值错 = 1 FP + 1 FN
    assert (c["city_pref"].tp, c["city_pref"].fp, c["city_pref"].fn) == (0, 1, 0)  # 幻觉 = FP
    c2 = score_case({"province": "江苏"}, {})
    assert c2["province"].fn == 1


def test_false_is_a_real_expected_value_for_tristate_slots():
    c = score_case({"accept_sino_foreign": False}, {"accept_sino_foreign": False})
    assert c["accept_sino_foreign"].tp == 1
    c = score_case({}, {"accept_sino_foreign": False})  # 没表态却填了 False = 幻觉
    assert c["accept_sino_foreign"].fp == 1


def test_missing_required():
    assert missing_required({"province": "江苏"}) == ["track", "subjects", "major_keyword", "score_or_rank"]
    full = {"province": "江苏", "track": "物理类", "subjects": ["物理"], "rank": 1, "major_keyword": "x"}
    assert missing_required(full) == []


def _scripted_agent(script):
    """按用户输入返回预设 profile 的假 Agent。"""
    async def agent(profile_json, history, msg):
        return script[msg]
    return agent


def test_run_case_merges_like_production_and_checks_followup():
    case = Case("t", "multi_turn", ["我是江苏考生", "分数610"], {"province": "江苏", "score": 610}, followup_turns=(0, 1))
    script = {
        "我是江苏考生": {"reply": "请问你的选科和分数？", "profile": {"province": "江苏", "score": None}},
        "分数610": {"reply": "收到。", "profile": {"province": None, "score": 610}},  # null 不能覆盖已有 province
    }
    r = asyncio.run(run_case(case, _scripted_agent(script)))
    assert r["state"] == {"province": "江苏", "score": 610}
    assert r["followup"] == [(0, True), (1, False)]  # 第 2 轮仍缺必填项却没追问


def test_run_all_records_agent_failure_instead_of_dropping_it():
    case = Case("boom", "partial", ["x"], {"province": "江苏"})

    async def broken(*a):
        raise RuntimeError("503")

    res = asyncio.run(run_all([case], broken))
    assert res[0]["error"].startswith("RuntimeError")
    rep = summarize(res)
    assert rep.overall.fn == 1 and rep.n_exact == 0   # 失败算漏检，不是被略过


def test_perfect_agent_gets_f1_one():
    async def oracle(profile_json, history, msg):
        for c in CASES:
            if msg in c.turns:
                return {"reply": "好的？", "profile": c.expected if msg == c.turns[-1] else {}}
        return {"reply": "", "profile": {}}

    from collections import Counter
    freq = Counter(t for c in CASES for t in c.turns)
    unique = [c for c in CASES if all(freq[t] == 1 for t in c.turns)]  # oracle 靠原话认用例，重复原话的跳过
    assert len(unique) > 100
    res = asyncio.run(run_all(unique, oracle))
    # 多轮用例里 oracle 只在最后一轮给出全部槽位，最终状态应与标准一致
    assert summarize(res).overall.f1() == 1.0


def test_rate_limited_retries_429_then_succeeds(monkeypatch):
    import asyncio as aio
    from .runner import rate_limited

    sleeps = []
    real_sleep = aio.sleep

    async def fake_sleep(t):
        sleeps.append(t)
        await real_sleep(0)

    monkeypatch.setattr("tests.eval.slots.runner.asyncio.sleep", fake_sleep)
    calls = {"n": 0}

    async def flaky(*a):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("Gemini API 返回 429: ... Please retry in 47.19s.")
        return {"reply": "ok", "profile": {}}

    res = asyncio.run(rate_limited(flaky, per_minute=600)("{}", [], "x"))
    assert res["reply"] == "ok" and calls["n"] == 3
    assert any(t >= 47 for t in sleeps)


def test_rate_limited_does_not_retry_other_errors():
    from .runner import rate_limited

    async def bad(*a):
        raise RuntimeError("Gemini API 返回 400")

    try:
        asyncio.run(rate_limited(bad, per_minute=600)("{}", [], "x"))
        assert False
    except RuntimeError:
        pass


def test_rate_limited_backs_off_on_503(monkeypatch):
    import asyncio as aio
    from .runner import rate_limited

    real_sleep = aio.sleep
    sleeps = []

    async def fake_sleep(t):
        sleeps.append(t)
        await real_sleep(0)

    monkeypatch.setattr("tests.eval.slots.runner.asyncio.sleep", fake_sleep)
    calls = {"n": 0}

    async def overloaded(*a):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError('Gemini API 返回 503：{"status":"UNAVAILABLE"}')
        return {"reply": "ok", "profile": {}}

    res = asyncio.run(rate_limited(overloaded, per_minute=600)("{}", [], "x"))
    assert res["reply"] == "ok" and calls["n"] == 3
    assert 10 in sleeps and 20 in sleeps


def test_home_province_only_false_equals_unset_but_true_does_not():
    assert score_case({}, {"home_province_only": False})["home_province_only"].fp == 0
    assert score_case({"home_province_only": False}, {})["home_province_only"].fn == 0
    assert score_case({}, {"home_province_only": True})["home_province_only"].fp == 1
    assert score_case({"home_province_only": True}, {"home_province_only": False})["home_province_only"].fn == 1
    # 真三态槽位仍然严格
    assert score_case({}, {"accept_sino_foreign": False})["accept_sino_foreign"].fp == 1


def test_has_question_accepts_imperative_asks_but_not_plain_statements():
    from .metrics import has_question
    assert has_question("请告知您的高考省份、选科类别")
    assert has_question("您是物理类还是历史类？")
    assert not has_question("收到，610分是一个不错的成绩。")
    # 新提示词让 Agent 改用陈述式追问（无问号、「请您」「还需要」），这些都是在要信息
    assert has_question("已记录选科信息。为了更精准地为您推荐，还需要了解一下您的预估高考分数。")
    assert has_question("好的，还需要请您提供所在省份、选科，以及您的预估分数或位次信息。")
    assert has_question("已收到您的选科信息。为了更精准地规划，请您告知目前的高考成绩。")


def test_generated_cases_are_deterministic_and_consistent():
    from . import cases as cmod
    from .cases import Case
    from .gen_cases import generate
    a, b = generate(Case), generate(Case)
    assert [(c.id, c.turns, c.expected) for c in a] == [(c.id, c.turns, c.expected) for c in b]
    for c in a:
        if c.tag in ("gen_full", "gen_infer_track", "gen_multi"):
            e = c.expected
            assert e["province"] and e["track"] in ("物理类", "历史类") and len(e["subjects"]) == 3
            assert ("score" in e) != ("rank" in e)
            first = e["subjects"][0]
            assert (first == "物理") == (e["track"] == "物理类")  # 类别与首选科目一致
        if c.tag == "gen_infer_track":
            assert "物理类" not in c.turns[0] and "历史类" not in c.turns[0]
        if c.tag == "gen_multi":
            assert 2 <= len(c.turns) <= 4 and c.followup_turns == tuple(range(len(c.turns) - 1))
    assert len({c.id for c in cmod.CASES}) == len(cmod.CASES)


def test_review_sample_covers_every_tag_and_at_least_20_percent():
    from .export_review import pick_sample
    picked = pick_sample(CASES)
    assert len(picked) / len(CASES) >= 0.2
    assert {c.tag for c in CASES if c.id in picked} == {c.tag for c in CASES}


def test_with_cache_refresh_ignores_old_entries_and_rewrites(tmp_path):
    import json as _json
    from .runner import with_cache
    path = tmp_path / "c.json"
    calls = {"n": 0}

    async def agent(profile_json, history, msg):
        calls["n"] += 1
        return {"reply": f"v{calls['n']}", "profile": {}}
    a = with_cache(agent, path)
    assert asyncio.run(a("{}", [], "x"))["reply"] == "v1"
    assert asyncio.run(with_cache(agent, path)("{}", [], "x"))["reply"] == "v1"           # 命中旧缓存
    assert asyncio.run(with_cache(agent, path, refresh=True)("{}", [], "x"))["reply"] == "v2"  # 刷新：重新调用
    assert asyncio.run(with_cache(agent, path)("{}", [], "x"))["reply"] == "v2"           # 文件已被新响应覆盖
    assert _json.loads(path.read_text())
