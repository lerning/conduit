"""Exact cache (spec C5; decisions #6 and #8).

- Keyed on a canonical hash of (tier, messages, max_tokens, temperature) --
  exact match only. Semantic cache is designed/deferred (spec §6 S1).
- Ships DISABLED by default (decision #8: read-only observability first; flip
  `CONDUIT_CACHE_ENABLED=1` once there's a baseline to measure against).
- Honors a per-request `cache_bypass` flag (decision #6): safety-classifier
  traffic and the eval harness's provider-drift runs must be able to force a
  live call -- a cached crisis verdict silently defeats a drift check.
- TTL is deliberately short by default (300s) for the same reason.

v1 store is in-process memory; the DynamoDB TTL-attribute variant (spec §3) is
the documented upgrade, same interface.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any, Optional


def request_hash(tier: str, messages: list[dict], max_tokens: int,
                 temperature: float, json_response: bool = False) -> str:
    canonical = json.dumps(
        {"tier": tier, "messages": messages, "max_tokens": max_tokens,
         "temperature": temperature, "json": json_response},
        sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class ExactCache:
    def __init__(self, enabled: bool = False, ttl_s: int = 300,
                 clock=time.monotonic) -> None:
        self.enabled = enabled
        self.ttl_s = ttl_s
        self._clock = clock
        self._store: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str, bypass: bool = False) -> Optional[dict]:
        if not self.enabled or bypass:
            return None
        now = self._clock()
        with self._lock:
            entry = self._store.get(key)
            if entry is None or entry[0] < now:
                self._store.pop(key, None)
                self.misses += 1
                return None
            self.hits += 1
            return entry[1]

    def put(self, key: str, value: dict) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._store[key] = (self._clock() + self.ttl_s, value)

    def stats(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "entries": len(self._store),
                "hits": self.hits, "misses": self.misses}
