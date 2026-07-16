"""Anthropic provider (spec C1). Real network calls -- not exercised by the
default test suite (which runs against MockProvider); used for the one captured
real-provider benchmark run (spec §4) and once routing/failover (P3) needs a
second live provider to fail over between.
"""
from __future__ import annotations

import os
from typing import AsyncIterator

from gateway.providers.base import (CompletionRequest, CompletionResponse,
                                    ProviderRateLimited, ProviderTimeout,
                                    ProviderUnavailable, StreamChunk, Usage)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str | None = None, timeout: float = 30.0) -> None:
        import anthropic  # lazy import -- keeps MockProvider-only paths dependency-light
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key or os.environ["ANTHROPIC_API_KEY"], timeout=timeout)

    def _map_error(self, e: Exception) -> Exception:
        import anthropic
        if isinstance(e, anthropic.RateLimitError):
            return ProviderRateLimited(str(e))
        if isinstance(e, anthropic.APITimeoutError):
            return ProviderTimeout(str(e))
        if isinstance(e, anthropic.APIStatusError) and e.status_code >= 500:
            return ProviderUnavailable(str(e))
        return e

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        try:
            resp = await self._client.messages.create(
                model=request.model,
                messages=[{"role": m.role, "content": m.content} for m in request.messages
                         if m.role != "system"],
                system=next((m.content for m in request.messages if m.role == "system"), None) or "",
                max_tokens=request.max_tokens, temperature=request.temperature,
            )
        except Exception as e:
            raise self._map_error(e) from e
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        return CompletionResponse(
            text=text, model=request.model, provider=self.name,
            usage=Usage(input_tokens=resp.usage.input_tokens,
                       output_tokens=resp.usage.output_tokens),
            stop_reason=resp.stop_reason)

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        try:
            async with self._client.messages.stream(
                model=request.model,
                messages=[{"role": m.role, "content": m.content} for m in request.messages
                         if m.role != "system"],
                system=next((m.content for m in request.messages if m.role == "system"), None) or "",
                max_tokens=request.max_tokens, temperature=request.temperature,
            ) as stream:
                async for text in stream.text_stream:
                    yield StreamChunk(text=text, is_final=False)
                final = await stream.get_final_message()
                yield StreamChunk(text="", is_final=True, usage=Usage(
                    input_tokens=final.usage.input_tokens,
                    output_tokens=final.usage.output_tokens))
        except Exception as e:
            raise self._map_error(e) from e
