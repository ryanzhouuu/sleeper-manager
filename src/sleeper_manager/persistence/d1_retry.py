"""Bound transient retries to D1 writes whose repeated application is safe."""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from functools import wraps
from random import uniform
from typing import Any

_TRANSIENT_ERRORS = (
    "Network connection lost",
    "storage caused object to be reset",
    "reset because its code was updated",
)


def retry_idempotent_d1_write[**P, T](
    write: Callable[P, Awaitable[T]],
) -> Callable[P, Coroutine[Any, Any, T]]:
    """Attempt a safe write at most three times; preserve the last failure on exhaustion."""

    @wraps(write)
    async def retry(*args: P.args, **kwargs: P.kwargs) -> T:
        attempt = 0
        while True:
            try:
                return await write(*args, **kwargs)
            except Exception as error:
                if attempt >= 2 or not any(text in str(error) for text in _TRANSIENT_ERRORS):
                    raise
                await asyncio.sleep(uniform(0.1, 0.2) * 2**attempt)
                attempt += 1

    return retry
