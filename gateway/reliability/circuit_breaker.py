"""Per-provider circuit breaker (spec C2).

Why it exists (README failure-mode analysis): without a breaker, every request
during a provider outage burns its full retry budget against a dead endpoint --
latency for every caller balloons to (attempts x timeout) and worker capacity
exhausts. The breaker converts a dead provider into an instant local decision
("skip to the next provider in the chain") until a half-open probe proves
recovery.

States: CLOSED (normal) -> OPEN after N consecutive failures (all calls skipped
for cooldown_s) -> HALF_OPEN (one probe allowed) -> CLOSED on success / OPEN on
failure.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    cooldown_s: float = 30.0
    clock: callable = time.monotonic

    _state: str = field(default=CLOSED, init=False)
    _consecutive_failures: int = field(default=0, init=False)
    _opened_at: float = field(default=0.0, init=False)
    _probe_in_flight: bool = field(default=False, init=False)

    @property
    def state(self) -> str:
        # OPEN lapses into HALF_OPEN once the cooldown expires.
        if self._state == OPEN and self.clock() - self._opened_at >= self.cooldown_s:
            self._state = HALF_OPEN
            self._probe_in_flight = False
        return self._state

    def allow(self) -> bool:
        s = self.state
        if s == CLOSED:
            return True
        if s == HALF_OPEN and not self._probe_in_flight:
            self._probe_in_flight = True  # exactly one probe at a time
            return True
        return False

    def record_success(self) -> None:
        self._state = CLOSED
        self._consecutive_failures = 0
        self._probe_in_flight = False

    def record_failure(self) -> None:
        if self.state == HALF_OPEN:
            self._trip()  # failed probe -> back to OPEN, restart cooldown
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._trip()

    def _trip(self) -> None:
        self._state = OPEN
        self._opened_at = self.clock()
        self._consecutive_failures = 0
        self._probe_in_flight = False


class BreakerRegistry:
    """One breaker per provider name."""

    def __init__(self, failure_threshold: int = 5, cooldown_s: float = 30.0) -> None:
        self._breakers: dict[str, CircuitBreaker] = {}
        self._threshold = failure_threshold
        self._cooldown = cooldown_s

    def for_provider(self, name: str) -> CircuitBreaker:
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(
                failure_threshold=self._threshold, cooldown_s=self._cooldown)
        return self._breakers[name]

    def states(self) -> dict[str, str]:
        return {name: b.state for name, b in self._breakers.items()}
