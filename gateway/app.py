"""Conduit gateway service (decisions #1/#3: a standalone HTTP service).

The numbered pipeline from spec §3, as explicit steps in one handler:

  1. auth (per-app API key -- decision #12)
  2. inbound rate limit (per-IP token bucket -- decision #14)
  3. [input guardrails -- P4, not yet built]
  4. spend caps: global hard stop + optional per-user (decisions #13, #5).
     FAIL CLOSED (decision #16): if the ledger can't be read, REFUSE rather
     than proceed uncapped.
  5. exact-cache lookup (decisions #6 bypass flag, #8 ships disabled)
  6. router: tier -> provider chain with retry + circuit breaker (decision #2)
  7. [output guardrails -- P4, not yet built]
  8. cache write + ledger write (metadata only -- never message content)

Run locally:  uvicorn gateway.app:app --port 8200
"""
from __future__ import annotations

import json
import time
from typing import AsyncIterator, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from gateway.cache.exact import ExactCache, request_hash
from gateway.config import Config
from gateway.providers.base import CompletionRequest, Message
from gateway.providers.mock import MockProvider
from gateway.ratelimit.token_bucket import TokenBucketLimiter
from gateway.reliability.circuit_breaker import BreakerRegistry
from gateway.routing.tiers import AllProvidersFailed, Router, UnknownTier
from gateway.storage import get_ledger_table
from gateway.telemetry.ledger import (OUTCOME_BAD_REQUEST, OUTCOME_CAP_GLOBAL,
                                      OUTCOME_CAP_USER, OUTCOME_FAIL_CLOSED,
                                      OUTCOME_PROVIDER_FAILED, OUTCOME_RATE_LIMITED,
                                      LedgerEntry, LedgerStore, compute_cost)


# --- provider registry --------------------------------------------------------
def build_providers(config: Config) -> dict:
    """Mock is always present (zero-secret clone runs end-to-end). Real
    providers register only when their key is in the environment."""
    import os
    providers: dict = {"mock": MockProvider()}
    if os.getenv("ANTHROPIC_API_KEY"):
        from gateway.providers.anthropic_provider import AnthropicProvider
        providers["anthropic"] = AnthropicProvider(timeout=config.provider_timeout_s)
    if os.getenv("OPENAI_API_KEY"):
        from gateway.providers.openai_provider import OpenAIProvider
        providers["openai"] = OpenAIProvider(timeout=config.provider_timeout_s)
    return providers


# --- request/response shapes ---------------------------------------------------
class MessageBody(BaseModel):
    role: str
    content: str


class CompleteBody(BaseModel):
    tier: str = "quality"                # decision #2: named tier, never a model
    messages: list[MessageBody]
    max_tokens: int = Field(default=1024, ge=1, le=16000)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    stream: bool = False
    json_response: bool = False          # structured passthrough (guaranteed JSON)
    user_id: str = "anonymous"           # decisions #4/#5: caller owns identity
    cache_bypass: bool = False           # decision #6: drift runs force live calls


def create_app(config: Optional[Config] = None) -> FastAPI:
    config = config or Config.from_env()
    app = FastAPI(title="Conduit", version="0.1.0")

    providers = build_providers(config)
    breakers = BreakerRegistry(config.breaker_failure_threshold, config.breaker_cooldown_s)
    router = Router(providers, config.tier_map, breakers,
                    retry_attempts=config.retry_attempts,
                    retry_base_delay_s=config.retry_base_delay_s)
    limiter = TokenBucketLimiter(config.ratelimit_capacity, config.ratelimit_refill_per_s)
    cache = ExactCache(enabled=config.cache_enabled, ttl_s=config.cache_ttl_s)
    ledger = LedgerStore(get_ledger_table(use_real_aws=config.use_real_aws,
                                          db_path=config.db_path))

    app.state.config = config
    app.state.ledger = ledger
    app.state.cache = cache
    app.state.breakers = breakers
    app.state.providers = providers   # the router holds this same dict; the load
                                      # drill swaps an entry to simulate an outage

    # --- pipeline steps as dependencies/helpers -------------------------------
    def authed_app(request: Request) -> str:
        """Step 1 (decision #12). No keys configured = local dev, auth off."""
        if not config.api_keys:
            return "dev"
        key = request.headers.get("x-api-key", "")
        app_name = config.api_keys.get(key)
        if app_name is None:
            raise HTTPException(401, "missing or unknown X-API-Key")
        return app_name

    def reject(outcome: str, code: int, detail: str, client_id: str,
               tier: str = "") -> HTTPException:
        """Record a refused request, then raise it.

        Rejections are the whole point of having protections; a ledger that only
        logs successes cannot answer "did we throttle anyone?". Cost is 0, so
        these never move spend -- they are counted separately."""
        try:
            ledger.put(LedgerEntry(outcome=outcome, status_code=code,
                                   client_id=client_id, cost_usd=0.0,
                                   routing_reason=f"tier:{tier}" if tier else ""))
        except Exception:
            pass          # never let telemetry turn a 429 into a 500
        return HTTPException(code, detail)

    def rate_limited(client_id: str, tier: str = "") -> None:
        """Step 2 -- keyed on CLIENT, not IP (revises decision #14).

        Per-IP was a bulkhead in name only here: Conduit sits on a private
        network with a single calling app, so every request shares one source
        address and therefore one bucket -- one noisy tenant would throttle
        everyone. Keying on `app:user` is what actually isolates tenants."""
        if not limiter.consume(client_id):
            raise reject(OUTCOME_RATE_LIMITED, 429,
                         "rate limit exceeded for this client; slow down",
                         client_id, tier)

    def enforce_spend_caps(app_name: str, user_id: str) -> None:
        """Step 4 (decisions #13, #5, #16). Fail CLOSED on ledger failure."""
        try:
            global_totals = ledger.day_totals()
            user_totals = (ledger.day_totals(client_prefix=f"{app_name}:{user_id}")
                           if config.user_daily_cap_usd is not None else None)
        except Exception as e:  # ledger unreadable -> refuse, never proceed uncapped
            raise reject(OUTCOME_FAIL_CLOSED, 503,
                         f"spend ledger unavailable; refusing (fail-closed): {e}",
                         f"{app_name}:{user_id}")
        if global_totals["spend_usd"] >= config.global_daily_cap_usd:
            raise reject(OUTCOME_CAP_GLOBAL, 402,
                         "daily spend limit reached; service resumes tomorrow",
                         f"{app_name}:{user_id}")
        if user_totals is not None and config.user_daily_cap_usd is not None \
                and user_totals["spend_usd"] >= config.user_daily_cap_usd:
            raise reject(OUTCOME_CAP_USER, 402,
                         "your daily usage limit is reached; resumes tomorrow",
                         f"{app_name}:{user_id}")

    def write_ledger(entry: LedgerEntry) -> None:
        ledger.put(entry)  # metadata only -- never message content

    # --- endpoints -------------------------------------------------------------
    @app.post("/v1/complete")
    async def complete(body: CompleteBody, request: Request,
                       app_name: str = Depends(authed_app)):
        client_id = f"{app_name}:{body.user_id}"
        rate_limited(client_id, body.tier)
        enforce_spend_caps(app_name, body.user_id)

        messages = [Message(role=m.role, content=m.content) for m in body.messages]
        creq = CompletionRequest(model="", messages=messages,
                                 max_tokens=body.max_tokens, temperature=body.temperature,
                                 json_response=body.json_response)
        key = request_hash(body.tier, [m.model_dump() for m in body.messages],
                           body.max_tokens, body.temperature, body.json_response)

        if body.stream:
            return StreamingResponse(
                _stream(body, creq, client_id),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache"})

        # step 5: cache lookup
        cached = cache.get(key, bypass=body.cache_bypass)
        if cached is not None:
            entry = LedgerEntry(client_id=client_id, model=cached["model"],
                                provider=cached["provider"], cache_hit=True,
                                cost_usd=0.0, latency_ms=0.0,
                                routing_reason="cache:exact_hit")
            write_ledger(entry)
            # a hit costs nothing -- report 0, not the original call's cost
            return {**cached, "cost_usd": 0.0, "cache_hit": True,
                    "request_id": entry.request_id, "routing_reason": "cache:exact_hit"}

        # step 6: route
        t0 = time.monotonic()
        try:
            routed = await router.complete(body.tier, creq)
        except UnknownTier as e:
            raise reject(OUTCOME_BAD_REQUEST, 400, str(e), client_id, body.tier)
        except AllProvidersFailed as e:
            raise reject(OUTCOME_PROVIDER_FAILED, 502, str(e), client_id, body.tier)
        latency_ms = (time.monotonic() - t0) * 1000
        resp = routed.response
        cost = compute_cost(resp.model, resp.usage.input_tokens, resp.usage.output_tokens)

        # step 8: ledger + cache write
        # provider = the chain slot, matching the streaming path and the breaker
        # registry. resp.provider is the implementation's own name and can differ.
        entry = LedgerEntry(client_id=client_id, model=resp.model,
                            provider=routed.provider or resp.provider,
                            input_tokens=resp.usage.input_tokens,
                            output_tokens=resp.usage.output_tokens,
                            cost_usd=cost, cache_hit=False,
                            latency_ms=round(latency_ms, 2),
                            routing_reason=routed.routing_reason)
        write_ledger(entry)
        payload = {"text": resp.text, "model": resp.model, "provider": resp.provider,
                   "usage": {"input_tokens": resp.usage.input_tokens,
                             "output_tokens": resp.usage.output_tokens},
                   "cost_usd": cost, "routing_reason": routed.routing_reason}
        cache.put(key, payload)
        return {**payload, "cache_hit": False, "request_id": entry.request_id,
                "latency_ms": entry.latency_ms}

    async def _stream(body: CompleteBody, creq: CompletionRequest,
                      client_id: str) -> AsyncIterator[str]:
        """SSE stream (spec C3). v1: failover applies before the first chunk;
        a mid-stream failure ends the stream with an error event (documented).
        Streams skip the exact cache in v1."""
        t0 = time.monotonic()
        ttft_ms: Optional[float] = None
        chain = router.chain_for(body.tier)
        last_err = "no providers attempted"
        for provider_name, model, note in chain:
            breaker = breakers.for_provider(provider_name)
            if not breaker.allow():
                last_err = f"{provider_name}:circuit_open"
                continue
            provider = providers[provider_name]
            req = CompletionRequest(model=model, messages=creq.messages,
                                    max_tokens=creq.max_tokens, temperature=creq.temperature)
            usage_in = usage_out = 0
            started = False
            try:
                async for chunk in provider.stream(req):
                    if not started:
                        started = True
                        ttft_ms = round((time.monotonic() - t0) * 1000, 2)
                        breaker.record_success()
                    if chunk.is_final:
                        if chunk.usage:
                            usage_in, usage_out = chunk.usage.input_tokens, chunk.usage.output_tokens
                    elif chunk.text:
                        yield f"event: chunk\ndata: {json.dumps({'text': chunk.text})}\n\n"
                cost = compute_cost(model, usage_in, usage_out)
                entry = LedgerEntry(client_id=client_id, model=model, provider=provider_name,
                                    input_tokens=usage_in, output_tokens=usage_out,
                                    cost_usd=cost, ttft_ms=ttft_ms,
                                    latency_ms=round((time.monotonic() - t0) * 1000, 2),
                                    routing_reason=f"tier:{body.tier} provider:{provider_name} stream"
                                                   + (f" {note}" if note else ""))
                write_ledger(entry)
                yield ("event: final\ndata: " + json.dumps(
                    {"request_id": entry.request_id, "model": model,
                     "provider": provider_name, "cost_usd": cost, "ttft_ms": ttft_ms,
                     "usage": {"input_tokens": usage_in, "output_tokens": usage_out}}) + "\n\n")
                return
            except Exception as e:
                breaker.record_failure()
                last_err = f"{provider_name}:{type(e).__name__}"
                if started:  # mid-stream failure: can't cleanly fail over (v1)
                    yield f"event: error\ndata: {json.dumps({'message': last_err})}\n\n"
                    return
                continue
        yield f"event: error\ndata: {json.dumps({'message': f'all providers failed: {last_err}'})}\n\n"

    @app.get("/v1/usage")
    def usage(user_id: Optional[str] = None, app_name: str = Depends(authed_app)):
        """The IC UI spend meter reads this (decision #13)."""
        totals = ledger.day_totals()
        out = {"date_utc": time.strftime("%Y-%m-%d", time.gmtime()),
               "spend_today_usd": totals["spend_usd"],
               "requests_today": totals["requests"],
               "global_daily_cap_usd": config.global_daily_cap_usd,
               "remaining_usd": round(max(0.0, config.global_daily_cap_usd
                                          - totals["spend_usd"]), 6),
               "cache": cache.stats()}
        if user_id:
            u = ledger.day_totals(client_prefix=f"{app_name}:{user_id}")
            out["user"] = {"user_id": user_id, **u,
                           "user_daily_cap_usd": config.user_daily_cap_usd}
        return out

    if config.dashboard_enabled:
        @app.get("/v1/dashboard", response_class=HTMLResponse)
        def dashboard(hours: int = 24):
            """Operations view: spend, tenant concentration, throttles, breakers.

            Deliberately not behind X-API-Key: it is a browser page on a service
            with no public listener (reachable only over 6PN or `fly proxy`,
            which requires Fly account auth), and it renders the metadata-only
            ledger -- there is no message content in it to leak. Kill the route
            with CONDUIT_DASHBOARD_ENABLED=0."""
            from gateway.dashboard import build_html
            return HTMLResponse(build_html(ledger, config, cache, breakers,
                                           window_h=max(1, min(hours, 24 * 30))))

    @app.get("/health")
    def health():
        return {"status": "ok",
                "providers": sorted(providers.keys()),
                "tiers": sorted(config.tier_map.keys()),
                "breakers": breakers.states(),
                "cache_enabled": cache.enabled,
                "auth_enabled": bool(config.api_keys)}

    return app


app = create_app()
