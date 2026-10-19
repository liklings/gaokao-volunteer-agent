"""
Gemini 调用的通用重试封装。只对 GeminiCallError（这次请求本身失败了，重试
大概率会好）重试，GeminiConfigError（没配Key，配置问题）不重试、直接抛出，
因为重试解决不了配置问题，白白浪费时间和额度。

这个设计直接来自两次真实撞到的偶发问题（都是在这个项目的调试过程中
真实发生的，不是提前假设的风险）：
1. Gemini 免费层小模型偶尔返回 503（"当前需求量大，请稍后再试"）
2. Gemini 偶尔生成畸形的超长数字导致 JSON 解析失败
这两种都靠人工手动点重试解决过，现在自动化掉，不用每次都靠人盯着。
"""
import asyncio
import logging
from typing import Awaitable, Callable, TypeVar

from app.services.gemini_errors import GeminiCallError, GeminiConfigError

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_MAX_RETRIES = 2        # 失败后最多再试2次，加上第一次总共最多尝试3次
DEFAULT_BACKOFF_SECONDS = 1.0  # 第1次重试等1秒，第2次等2秒——简单的线性退避，够用，不引入额外依赖


async def call_with_retry(
    func: Callable[[], Awaitable[T]],
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
) -> T:
    """
    func 是一个无参数的异步可调用对象（通常用 lambda 包一下真正要调用的函数），
    这样这个重试逻辑可以复用在任何"调用Gemini"的场景，不用为每个调用点各写一份。
    """
    last_error: GeminiCallError | None = None
    for attempt in range(max_retries + 1):
        try:
            return await func()
        except GeminiConfigError:
            raise  # 配置问题重试没用，直接原样抛出，不吞掉
        except GeminiCallError as exc:
            last_error = exc
            if attempt < max_retries:
                wait = backoff_seconds * (attempt + 1)
                logger.warning(
                    "Gemini 调用失败（第%d次尝试）：%s，%.0f秒后重试",
                    attempt + 1, exc, wait,
                )
                await asyncio.sleep(wait)
            else:
                logger.warning("Gemini 调用重试%d次后仍然失败：%s", max_retries, exc)

    assert last_error is not None  # 循环里 max_retries+1 次都失败才会走到这，last_error 一定被赋过值
    raise last_error
