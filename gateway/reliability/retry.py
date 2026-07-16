"""Retry with exponential backoff + FULL JITTER (spec C2).

Why jitter is not optional (the README failure-mode analysis, in code form):
when a provider blips, every client that retries on a fixed schedule comes back
at the same instant -- a thundering herd that re-kills the recovering provider.
Full jitter (delay drawn uniformly from [0, backoff]) decorrelates the herd.
"""
from __future__ import annotations

import asyncio
import random
from typing import Awaitable, Callable, TypeVar

from gateway.providers.base import (ProviderRateLimited, ProviderTimeout,
                                    ProviderUnavailable)

T = TypeVar("T")

RETRYABLE = (ProviderRateLimited, ProviderUnavailable, ProviderTimeout)


async def with_retry(fn: Callable[[], Awaitable[T]], *, attempts: int = 3,
                     base_delay_s: float = 0.2, max_delay_s: float = 4.0,
                     sleep=asyncio.sleep) -> T:
    """Run `fn` up to `attempts` times. Retries only on RETRYABLE provider
    errors -- a 4xx-style bad request is not retried (it will never succeed).
    `sleep` is injectable so tests run instantly."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return await fn()
        except RETRYABLE as e:
            last = e
            if attempt == attempts - 1:
                break
            backoff = min(max_delay_s, base_delay_s * (2 ** attempt))
            await sleep(random.uniform(0, backoff))  # full jitter
    assert last is not None
    raise last
