"""
把 react_loop.py 跑完一次循环后产生的内存态 RunTrace，落库到
agent_runs / agent_steps 两张表。这一步完全不需要调用 Gemini——
可以直接拿 test_react_loop_scripted.py 里那些脚本化场景跑出来的
RunTrace 来测，跟 API 额度没有任何关系。
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import AgentRun, AgentStep
from app.services.react_loop import RunTrace


def persist_trace(db: Session, trace: RunTrace, profile_id: int, run_id: str | None = None) -> AgentRun:
    """
    run_id：跟 app/services/logging_config.py 里 new_run_id() 生成、这次请求
    全程日志都带着的那个值是同一个——存进这一行，出问题时能直接拿日志里
    grep 到的 run_id 去数据库查这次执行的完整 trace。默认 None 是为了兼容
    还没传这个参数的旧调用方（比如脚本化测试），不强制所有调用方都改。
    """
    run = AgentRun(
        profile_id=profile_id,
        run_id=run_id,
        agent_name=trace.agent_name,
        status=trace.status,
        abort_reason=trace.abort_reason,
        total_steps=trace.total_steps,
        total_tokens=trace.total_tokens,
        finished_at=datetime.now(timezone.utc),  # 调用这个函数时run本身已经跑完了
    )
    db.add(run)
    db.flush()  # 拿到 run.id，下面的 AgentStep 要用

    for step in trace.steps:
        db.add(AgentStep(
            run_id=run.id,
            step_index=step.step_index,
            action_type=step.action_type,
            tool_name=step.tool_name,
            tool_args=step.tool_args,
            tool_result=step.tool_result,
            tokens_used=step.tokens_used,
            status=step.status,
        ))

    db.commit()
    db.refresh(run)
    return run
