"""Inbound rate limiting: token bucket per client IP (decision #14, spec C6).

v1 is in-process memory -- correct for a single gateway instance, which is the
deployment shape for now. The DynamoDB conditional-write variant (spec §3) is
the documented upgrade when the gateway goes multi-instance; the interface here
doesn't change.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class TokenBucketLimiter:
    def __init__(self, capacity: int = 20, refill_per_s: float = 2.0,
                 clock=time.monotonic) -> None:
        self.capacity = float(capacity)
        self.refill_per_s = refill_per_s
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def consume(self, key: str, cost: float = 1.0) -> bool:
        """True if the request is allowed; False -> caller returns 429."""
        now = self._clock()
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                b = _Bucket(tokens=self.capacity, last_refill=now)
                self._buckets[key] = b
            # lazy refill
            b.tokens = min(self.capacity,
                           b.tokens + (now - b.last_refill) * self.refill_per_s)
            b.last_refill = now
            if b.tokens >= cost:
                b.tokens -= cost
                return True
            return False
