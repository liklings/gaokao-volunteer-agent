"""
验证底线校验网关在 orchestrator 这一层是"物理上无法被绕过"的，不是只靠
提示词里那句"必须调用 finalize_recommendation"自觉遵守——用脚本化假模型
模拟"模型没有调用 finalize_recommendation、直接给出最终文字回复"这种
不听话的情况，确认 orchestrator._run_advisor_step 会拦截这个回复，
不会把它当成正式建议写进聊天记录、返回给用户。

用 monkeypatch 替换 orchestrator.py 里的 call_gemini_with_tools（模块级
名字），不发真实网络请求、不需要真实 Gemini Key。
"""
from app.models import Profile
from app.services.orchestrator import (
    _UNVALIDATED_RECOMMENDATION_FALLBACK,
    _advisor_recommendation_was_validated,
    _run_advisor_step,
)
from app.services.react_loop import ModelResponse


def scripted_model(responses: list[ModelResponse]):
    call_count = 0

    async def _caller(system_prompt, contents, tool_schemas):
        nonlocal call_count
        if call_count >= len(responses):
            raise AssertionError(
                f"脚本已经用完（预设了{len(responses)}次响应），但循环还在继续调用模型。"
            )
        r = responses[call_count]
        call_count += 1
        return r

    return _caller


def make_complete_profile(db) -> Profile:
    profile = Profile(
        province="江苏", track="物理类", subjects=["物理", "化学"],
        score=637, rank=5200, major_keyword="计算机",
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


async def test_bypassing_finalize_recommendation_is_intercepted(db, monkeypatch):
    """模型没有调用 finalize_recommendation，直接给出'我推荐东南大学'这种
    文字结论——即便 ReAct 循环本身'正常完成'（没超时、没超步数），
    orchestrator 也必须拦截，不能把这段话当成最终建议返回。
    """
    profile = make_complete_profile(db)
    model = scripted_model([
        ModelResponse(is_final_answer=True, text="我强烈推荐东南大学计算机类专业组，性价比很高。", tokens_used=50),
    ])
    monkeypatch.setattr("app.services.orchestrator.call_gemini_with_tools", model)

    text, trace = await _run_advisor_step(db, profile, "你更推荐哪个", run_id="test-run-1")

    assert text == _UNVALIDATED_RECOMMENDATION_FALLBACK, f"应该被拦截替换成兜底文案，实际: {text}"
    assert trace.status == "aborted"
    assert trace.abort_reason == "recommendation_not_validated"
    assert not _advisor_recommendation_was_validated(trace)


async def test_calling_finalize_recommendation_successfully_passes_through(db, monkeypatch):
    """模型按规矩先调用 finalize_recommendation、拿到通过结果后再给最终
    回复——这种情况下回复应该原样通过，不应该被网关拦截替换掉。
    """
    profile = make_complete_profile(db)
    model = scripted_model([
        ModelResponse(is_final_answer=False, tool_name="finalize_recommendation",
                      tool_args={"group_ids": ["seu-cs-04", "nuist-cs-03"]}, tokens_used=30),
        ModelResponse(is_final_answer=True, text="综合来看，推荐东南大学和南京邮电大学这两个候选。", tokens_used=50),
    ])
    monkeypatch.setattr("app.services.orchestrator.call_gemini_with_tools", model)

    text, trace = await _run_advisor_step(db, profile, "你更推荐哪个", run_id="test-run-2")

    assert text == "综合来看，推荐东南大学和南京邮电大学这两个候选。"
    assert trace.status == "completed"
    assert _advisor_recommendation_was_validated(trace)


async def test_calling_finalize_recommendation_with_invalid_ids_then_retrying_passes(db, monkeypatch):
    """模型第一次提交的名单没通过校验（比如编了一个不存在的 group_id），
    根据返回的失败原因调整后重新提交、这次通过了——最终应该正常放行。
    """
    profile = make_complete_profile(db)
    model = scripted_model([
        ModelResponse(is_final_answer=False, tool_name="finalize_recommendation",
                      tool_args={"group_ids": ["does-not-exist-xyz"]}, tokens_used=30),
        ModelResponse(is_final_answer=False, tool_name="finalize_recommendation",
                      tool_args={"group_ids": ["seu-cs-04"]}, tokens_used=30),
        ModelResponse(is_final_answer=True, text="推荐东南大学计算机类专业组。", tokens_used=40),
    ])
    monkeypatch.setattr("app.services.orchestrator.call_gemini_with_tools", model)

    text, trace = await _run_advisor_step(db, profile, "你更推荐哪个", run_id="test-run-3")

    assert text == "推荐东南大学计算机类专业组。"
    assert trace.status == "completed"
    first_call = trace.steps[0]
    assert first_call.status == "failed", "第一次提交不存在的 group_id 应该被记录为失败的工具调用"
    assert _advisor_recommendation_was_validated(trace), "第二次成功提交后，网关应该判定这一轮通过了校验"
