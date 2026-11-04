"""重试策略：指数退避（异步）。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from loguru import logger

T = TypeVar("T")

_SleepFn = Callable[[float], Awaitable[None]]


async def retry_with_backoff(
    func: Callable[[], Awaitable[T]],
    *,
    max_retries: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    sleep_fn: _SleepFn | None = None,
) -> T:
    """执行 ``func``，失败时按 1→2→4→8… 指数退避重试。

    成功返回结果；全部失败则抛出最后一次异常。
    """
    sleep = sleep_fn or asyncio.sleep
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            return await func()
        except Exception as exc:
            last_exc = exc
            if attempt == max_retries - 1:
                break
            delay = min(base_delay * (2**attempt), max_delay)
            logger.warning("第 {} 次失败，{}s 后重试：{}", attempt + 1, delay, exc)
            await sleep(delay)
    assert last_exc is not None
    raise last_exc
