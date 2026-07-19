"""OpenAI provider (spec C1). Second real provider -- needed for provider
failover (P3) to mean something. Not exercised by the default test suite.
"""
from __future__ import annotations

import os
from typing import AsyncIterator

from gateway.providers.base import (CompletionRequest, CompletionResponse,
                                    ProviderRateLimited, ProviderTimeout,
                                    ProviderUnavailable, StreamChunk, Usage)


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str | None = None, timeout: float = 30.0) -> None:
        import openai  # lazy import -- keeps MockProvider-only paths dependency-light
        self._client = openai.AsyncOpenAI(
            api_key=api_key or os.environ["OPENAI_API_KEY"], timeout=timeout)

    def _map_error(self, e: Exception) -> Exception:
        import openai
        if isinstance(e, openai.RateLimitError):
            return ProviderRateLimited(str(e))
        if isinstance(e, openai.APITimeoutError):
            return ProviderTimeout(str(e))
        if isinstance(e, openai.APIStatusError) and e.status_code >= 500:
            return ProviderUnavailable(str(e))
        return e

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        kwargs: dict = {}
        if request.json_response:
            # Requires the word "json" somewhere in the messages (OpenAI rule);
            # structured callers' prompts satisfy this by construction.
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = await self._client.chat.completions.create(
                model=request.model,
                messages=[{"role": m.role, "content": m.content} for m in request.messages],
                max_tokens=request.max_tokens, temperature=request.temperature, **kwargs,
            )
        except Exception as e:
            raise self._map_error(e) from e
        choice = resp.choices[0]
        return CompletionResponse(
            text=choice.message.content or "", model=request.model, provider=self.name,
            usage=Usage(input_tokens=resp.usage.prompt_tokens,
                       output_tokens=resp.usage.completion_tokens),
            stop_reason=choice.finish_reason)

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        try:
            stream = await self._client.chat.completions.create(
                model=request.model,
                messages=[{"role": m.role, "content": m.content} for m in request.messages],
                max_tokens=request.max_tokens, temperature=request.temperature,
                stream=True, stream_options={"include_usage": True},
            )
            async for chunk in stream:
                if chunk.usage:
                    yield StreamChunk(text="", is_final=True, usage=Usage(
                        input_tokens=chunk.usage.prompt_tokens,
                        output_tokens=chunk.usage.completion_tokens))
                elif chunk.choices and chunk.choices[0].delta.content:
                    yield StreamChunk(text=chunk.choices[0].delta.content, is_final=False)
        except Exception as e:
            raise self._map_error(e) from e
