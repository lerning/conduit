"""Shared provider interface (spec C1).

Every provider -- Anthropic, OpenAI, the deterministic mock -- satisfies this one
protocol. Streaming-capable from the start (spec C3 depends on this), so
reliability (retry/circuit-breaker), routing, and guardrails all operate against
one shape regardless of which provider is behind it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol


@dataclass
class Message:
    role: str  # "user" | "assistant" | "system"
    content: str


@dataclass
class CompletionRequest:
    model: str
    messages: list[Message]
    max_tokens: int = 1024
    temperature: float = 0.7
    stream: bool = False
    # Structured-output passthrough (content-blind: the gateway forwards the
    # constraint, it never reads the content). True -> the response text is
    # guaranteed to be a JSON object (forced tool-use on Anthropic,
    # response_format on OpenAI, deterministic JSON on the mock).
    json_response: bool = False
    idempotency_key: str | None = None


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class CompletionResponse:
    text: str
    model: str
    provider: str
    usage: Usage = field(default_factory=Usage)
    stop_reason: str | None = None


@dataclass
class StreamChunk:
    text: str
    is_final: bool = False
    usage: Usage | None = None  # populated on the final chunk


class ProviderError(Exception):
    """Base for provider failures. Reliability layer (P1) keys off these types."""


class ProviderRateLimited(ProviderError):
    """Provider returned 429 / rate-limit signal."""


class ProviderUnavailable(ProviderError):
    """Provider returned 5xx / timed out / connection failed."""


class ProviderTimeout(ProviderError):
    """Request exceeded the provider call's timeout budget."""


class Provider(Protocol):
    """One provider backend. Implementations: AnthropicProvider, OpenAIProvider,
    MockProvider. All are streaming-capable so the gateway never branches on
    "is this provider streaming or not" -- non-streaming providers wrap a single
    chunk."""

    name: str

    async def complete(self, request: CompletionRequest) -> CompletionResponse: ...

    def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]: ...
