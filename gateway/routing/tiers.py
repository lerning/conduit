"""Tier resolution + provider failover (decision #2; spec C4 axis b).

Clients request a NAMED TIER ("fast" / "quality" / "judge"), never a literal
model. The tier map is an ordered failover chain of (provider, model): the
router walks it, skipping providers whose circuit breaker is open, retrying
transient failures within a provider before moving on. Which model backs a tier
-- and what happens when a provider deprecates a parameter or goes down -- is a
gateway concern, invisible to the calling app.

The cost-cascade axis (spec C4 axis a, escalate-on-uncertainty) is deliberately
NOT in v1 -- see docs/DECISIONS.md #15 and spec §8 P3.

Providers not configured (no API key) are transparently backed by the
deterministic mock so a fresh clone runs end-to-end with zero secrets; the
substitution is recorded in the routing_reason so it can't masquerade as a real
provider in the ledger.
"""
from __future__ import annotations

from dataclasses import dataclass

from gateway.providers.base import (CompletionRequest, CompletionResponse,
                                    Provider, ProviderError)
from gateway.reliability.circuit_breaker import BreakerRegistry
from gateway.reliability.retry import with_retry


class UnknownTier(Exception):
    pass


class AllProvidersFailed(Exception):
    pass


@dataclass
class Routed:
    response: CompletionResponse
    routing_reason: str  # e.g. "tier:fast provider:anthropic attempt:1"


class Router:
    def __init__(self, providers: dict[str, Provider],
                 tier_map: dict[str, list[tuple[str, str]]],
                 breakers: BreakerRegistry,
                 retry_attempts: int = 3, retry_base_delay_s: float = 0.2) -> None:
        self._providers = providers
        self._tier_map = tier_map
        self._breakers = breakers
        self._retry_attempts = retry_attempts
        self._retry_base_delay_s = retry_base_delay_s

    def chain_for(self, tier: str) -> list[tuple[str, str, str]]:
        """[(effective_provider_name, model, note)] -- note marks mock substitution."""
        if tier not in self._tier_map:
            raise UnknownTier(f"unknown tier {tier!r}; known: {sorted(self._tier_map)}")
        chain = []
        for provider_name, model in self._tier_map[tier]:
            if provider_name in self._providers:
                chain.append((provider_name, model, ""))
            elif "mock" in self._providers:
                chain.append(("mock", model, f"mock_for:{provider_name}"))
        if not chain:
            raise AllProvidersFailed(f"no configured provider can serve tier {tier!r}")
        return chain

    async def complete(self, tier: str, request: CompletionRequest) -> Routed:
        errors: list[str] = []
        for provider_name, model, note in self.chain_for(tier):
            breaker = self._breakers.for_provider(provider_name)
            if not breaker.allow():
                errors.append(f"{provider_name}:circuit_open")
                continue
            provider = self._providers[provider_name]
            req = CompletionRequest(model=model, messages=request.messages,
                                    max_tokens=request.max_tokens,
                                    temperature=request.temperature,
                                    json_response=request.json_response,
                                    idempotency_key=request.idempotency_key)
            try:
                resp = await with_retry(lambda: provider.complete(req),
                                        attempts=self._retry_attempts,
                                        base_delay_s=self._retry_base_delay_s)
                breaker.record_success()
                reason = f"tier:{tier} provider:{provider_name}"
                if note:
                    reason += f" {note}"
                if errors:
                    reason += f" failover_from:[{','.join(errors)}]"
                return Routed(response=resp, routing_reason=reason)
            except ProviderError as e:
                breaker.record_failure()
                errors.append(f"{provider_name}:{type(e).__name__}")
                continue
        raise AllProvidersFailed(f"tier {tier!r} exhausted: {errors}")
