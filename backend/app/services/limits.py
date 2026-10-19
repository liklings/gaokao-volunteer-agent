"""
成本控制的硬限制，集中放在这里维护——单轮 token 预算、单个工具调用超时、
整轮 ReAct 循环总耗时上限、按 profile_id 限流，这四条对应设计文档里
"防止一次异常请求把 Gemini 额度或响应时间拖爆"的要求。

之前 react_loop.py 只有 MAX_STEPS=6 和重复调用检测——这两条防的是"模型
在瞎绕圈子"，防不住另外两类真实会发生的情况：一步没绕圈子，但单单这一步
或者这一轮加起来消耗的 token 本身就很贵；或者某一步（尤其是以后接入真实
web_search 这种会发外部请求的工具）卡住迟迟不返回。这几条是补这两类。

放在独立文件而不是分散写进 react_loop.py/orchestrator.py 里，是因为这几个
阈值本身是同一类东西（"允许消耗多少资源"的策略配置），以后要调阈值只用
改这一个文件，不用去几个不同的服务文件里分别找。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ChatMessage

# 单轮 ReAct 循环（一次 run_react_loop）最多消耗多少 token——
# 达到这个上限即使还没到 MAX_STEPS，也强制中断、基于已有信息给出回复，
# 不能让模型在一轮对话里无限"深挖"下去，一轮就把大半天的额度花掉。
MAX_TOKENS_PER_RUN = 8000

# 单个工具调用最多等待多少秒。现在这几个工具（search_candidates 等）都是
# 本地数据库查询，正常几十毫秒就该返回；这个阈值主要是给以后接入真实
# web_search（会真正发外部网络请求）兜底——工具变慢了，最多拖慢这一步
# TOOL_CALL_TIMEOUT_SECONDS 秒，不会把整个循环拖死。
TOOL_CALL_TIMEOUT_SECONDS = 15

# 一次 run_react_loop 从开始到必须给出结果的总耗时上限。MAX_STEPS=6 兜底
# 的是"步数不能无限多"，但"步数不多、可每一步都很慢"依然可能让一次请求
# 耗时过长（尤其是 gemini_retry.py 失败会重试，单步最坏情况下可能真实
# 发生多次 60 秒级别的网络请求），这里再加一道基于真实耗时的保护。
RUN_TOTAL_TIMEOUT_SECONDS = 60

# 同一个 profile_id 在这个时间窗口内，最多允许发起这么多次会触发调用
# Gemini 的对话请求（不区分这一轮最终走 profile/retrieval/advisor 哪个
# 分支）——防止一个 profile 被脚本、误操作或者前端重复点击，在短时间内
# 高频调用把额度刷爆。
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_MAX_REQUESTS = 5


class RateLimitExceededError(RuntimeError):
    """同一个 profile_id 短时间内请求过于频繁，直接拒绝这次调用，不重试、不排队。"""


def check_rate_limit(db: Session, profile_id: int) -> None:
    """
    在真正调用任何 Agent（因而真正花掉 Gemini 额度）之前先检查，超限直接
    抛出异常，调用方（run_orchestrator）不需要往下走任何一步。

    用 ChatMessage 里 role='user' 的记录数量做判断，而不是另建一张限流专用
    表或者维护一份进程内存里的计数器——原因有两个：
    1. 每一轮对话（不管最终走 profile/retrieval/advisor 哪个分支）现在都会
       先落一条 role='user' 的 ChatMessage（orchestrator.py 里三个
       _run_xxx_step 函数都这么做），这个信号本身就已经是"这个 profile
       最近发起过几次请求"的准确记录，不需要另外维护一份。
    2. 进程内存里的计数器在多进程/多副本部署下各进程互相看不到，进程重启
       也会清零，起不到真正的保护作用；查数据库虽然多一次查询，但保证
       任何部署形态下限流判断都是准确的。
    """
    window_start = datetime.now(timezone.utc) - timedelta(seconds=RATE_LIMIT_WINDOW_SECONDS)
    count = db.scalar(
        select(func.count()).select_from(ChatMessage).where(
            ChatMessage.profile_id == profile_id,
            ChatMessage.role == "user",
            ChatMessage.created_at >= window_start,
        )
    ) or 0
    if count >= RATE_LIMIT_MAX_REQUESTS:
        raise RateLimitExceededError(
            f"profile_id={profile_id} 在最近 {RATE_LIMIT_WINDOW_SECONDS} 秒内已经发起了 "
            f"{count} 次请求，超过限流上限（{RATE_LIMIT_MAX_REQUESTS} 次/{RATE_LIMIT_WINDOW_SECONDS}秒），"
            f"请稍后再试。"
        )
