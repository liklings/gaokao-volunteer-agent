"""评测通用：对 Gemini 调用限速（免费档每分钟请求数有限）与按入参缓存。slots 与 judge 共用。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Awaitable, Callable


def rate_limited(fn: Callable[..., Awaitable], per_minute: int = 12, max_retries: int = 6):
    """每分钟最多发起 per_minute 次；429 按服务端 "retry in Ns" 等待重试；
    500/502/503/504（服务端过载，临时性）指数退避重试；其余错误直接抛出。
    放在缓存里面：命中缓存的请求不占配额。默认 12，给免费档 15 次/分钟留余量。"""
    interval = 60.0 / per_minute
    lock = asyncio.Lock()
    last = [0.0]

    async def wrapped(*args, **kwargs):
        for attempt in range(max_retries + 1):
            async with lock:  # 串行化发起时刻，保证间隔
                wait = last[0] + interval - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                last[0] = time.monotonic()
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:
                text = str(exc)
                transient = any(code in text for code in ("429", "500", "502", "503", "504"))
                if not transient or attempt == max_retries:
                    raise
                if "429" not in text:
                    await asyncio.sleep(min(10 * 2 ** attempt, 120))
                    continue
                m = re.search(r"retry in ([\d.]+)s", text) or re.search(r'"retryDelay":\s*"(\d+)s"', text)
                await asyncio.sleep(float(m.group(1)) + 2 if m else 30)

    return wrapped


def json_cached(fn: Callable[..., Awaitable], path: Path):
    """按全部入参缓存返回值（需可 JSON 序列化）。改了 prompt/模型后要删掉缓存文件。失败不写缓存。"""
    cache: dict = json.loads(path.read_text()) if path.exists() else {}

    async def wrapped(*args):
        key = hashlib.sha256(json.dumps(args, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if key not in cache:
            cache[key] = await fn(*args)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cache, ensure_ascii=False))
        return cache[key]

    return wrapped
