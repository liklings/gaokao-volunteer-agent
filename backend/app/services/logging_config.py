"""
统一的日志配置 + run_id 贯穿机制。

之前排查问题全靠测试脚本里的 print——能用是因为那时候是自己盯着终端跑
脚本、一次只跑一个场景。接了真实 /api/chat 接口之后，情况变成"某次真实
请求出了问题，事后要去翻日志找"，这时候没有统一的日志格式、也没有办法把
"同一次请求"触发的所有日志（可能横跨 orchestrator.py / react_loop.py /
gemini_tool_caller.py / agent_client.py 好几个文件）串起来看，日志再多
也没法用，等于没记。

run_id 用 contextvars 而不是在每个函数签名上加一个 run_id 参数、一路传下去——
后者要改遍好几个文件里一串函数的签名，改动面大、容易漏改一处，以后新加的
函数也得记得接着传。contextvars 是 asyncio 原生支持的"一条异步调用链共享
一份上下文"的机制：在请求入口（run_orchestrator 开始处）设一次，下游任何
地方只要调用标准的 logging 模块打日志，都会自动带上这个 run_id，不需要
显式传参、也不用担心漏传。

用法：
1. 应用启动时调用一次 configure_logging()（在 main.py 里）。
2. 每次执行一轮 Orchestrator（run_orchestrator 开头）调用一次 new_run_id()
   拿到本轮的 run_id 并设进上下文，后续这轮涉及的所有 logger.info/warning/
   error 调用都会自动带上它。
3. 想在日志里定位某一次真实请求，直接 grep "run_id=xxxx" 就能把这一次
   请求触发的完整过程摘出来（哪一步调用了什么工具、有没有重试、最终状态）。
"""
from __future__ import annotations

import logging
import sys
import uuid
from contextvars import ContextVar

_run_id_var: ContextVar[str] = ContextVar("run_id", default="-")


class _RunIdFilter(logging.Filter):
    """把当前上下文里的 run_id 注入每一条日志记录，配合下面的 Formatter 使用。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = _run_id_var.get()
        return True


def new_run_id() -> str:
    """生成一个新的 run_id 并设进当前上下文，返回值本身也可以存进数据库
    （AgentRun.run_id），这样日志里的 run_id 和数据库里的执行轨迹记录
    能直接对上，不用凭时间戳去猜是不是同一次。
    """
    run_id = uuid.uuid4().hex[:12]
    _run_id_var.set(run_id)
    return run_id


def get_run_id() -> str:
    return _run_id_var.get()


def configure_logging(level: int = logging.INFO) -> None:
    """应用启动时调用一次（app/main.py 里）。重复调用是安全的（幂等），
    FastAPI --reload 触发模块重新加载时不会叠加出重复的 handler。
    """
    root = logging.getLogger()
    root.setLevel(level)

    # 幂等：先清空已有 handler，避免 --reload 或者测试脚本重复调用时
    # 日志被打印好几遍。
    root.handlers = []

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_RunIdFilter())
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s [run_id=%(run_id)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(handler)
