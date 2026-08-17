"""Deterministic mock provider (spec C1, C9).

Zero-spend, reproducible-from-clone. This is what the benchmark and the
fault-injection chaos suite run against by default -- so it needs to support
SCRIPTED FAILURE INJECTION, not just a happy path: the chaos scenarios in P1
("provider goes dark mid-load", "rate-limit storm") drive this provider through
a queue of behaviors to exercise retry/circuit-breaker/failover deterministically.
"""
from __future__ import annotations

import hashlib
import re
from collections import deque
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable

from gateway.providers.base import (CompletionRequest, CompletionResponse,
                                    Provider, ProviderRateLimited,
                                    ProviderTimeout, ProviderUnavailable,
                                    StreamChunk, Usage)

Behavior = Callable[[], None]  # raises to inject a fault, returns normally to succeed


def fail_rate_limited() -> None:
    raise ProviderRateLimited("mock: 429 rate limited")


def fail_unavailable() -> None:
    raise ProviderUnavailable("mock: 503 unavailable")


def fail_timeout() -> None:
    raise ProviderTimeout("mock: request timed out")


def succeed() -> None:
    return None


# See MockProvider._consume_script -- the over-HTTP chaos seam for live drills.
CHAOS_MARKER = "CHAOS_PROVIDER_DOWN"


@dataclass
class MockProvider:
    """Deterministic mock. `script` is an optional queue of Behaviors consumed
    one-per-call (chaos scenarios push failures onto it); once exhausted, calls
    succeed normally. This is the seam the fault-injection harness (P1/C9)
    scripts against."""

    name: str = "mock"
    script: deque[Behavior] = field(default_factory=deque)

    def queue_failure(self, behavior: Behavior, times: int = 1) -> None:
        for _ in range(times):
            self.script.append(behavior)

    def _consume_script(self, request: CompletionRequest) -> None:
        if self.script:
            self.script.popleft()()
        # Remote chaos seam: `queue_failure` only works in-process, but the
        # live drill talks to a DEPLOYED gateway over HTTP. A request whose
        # last user message carries the marker fails as if the provider were
        # down. Only the mock honors it (drill traffic never reaches a real
        # provider), and mock tokens cost $0 by construction.
        last_user = next((m.content for m in reversed(request.messages)
                          if m.role == "user"), "")
        if CHAOS_MARKER in last_user:
            raise ProviderUnavailable("mock: scripted outage (chaos marker)")

    def _deterministic_reply(self, request: CompletionRequest) -> str:
        last_user = next((m.content for m in reversed(request.messages)
                          if m.role == "user"), "")
        digest = hashlib.md5(last_user.encode()).hexdigest()[:8]
        if request.json_response:
            import json
            return json.dumps({"mock": True, "model": request.model, "ack": digest})
        return f"[mock:{request.model}] ack({digest}): {last_user[:80]}"

    def _usage(self, request: CompletionRequest, text: str) -> Usage:
        in_tok = sum(max(1, len(m.content) // 4) for m in request.messages)
        return Usage(input_tokens=in_tok, output_tokens=max(1, len(text) // 4))

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self._consume_script(request)
        text = self._deterministic_reply(request)
        return CompletionResponse(text=text, model=request.model, provider=self.name,
                                  usage=self._usage(request, text), stop_reason="end_turn")

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        self._consume_script(request)
        text = self._deterministic_reply(request)
        words = re.findall(r"\S+\s*", text)
        for w in words:
            yield StreamChunk(text=w, is_final=False)
        yield StreamChunk(text="", is_final=True, usage=self._usage(request, text))


def new_mock() -> Provider:
    return MockProvider()
