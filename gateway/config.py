"""Conduit configuration. Env-driven; every integration decision that is a
number or a switch lives here, named for the decision it implements
(see docs/DECISIONS.md).

Decision mapping:
  #2  tiers        -> TIER_MAP (clients ask for a named tier, never a model)
  #5  per-user cap -> user_daily_cap_usd (client app sets the value via env/deploy)
  #6  cache bypass -> cache_ttl_s + per-request cache_bypass flag (in the API)
  #8  read-only 1st-> cache_enabled defaults FALSE (observability before action)
  #12 exposed+keys -> api_keys ("app:key,app2:key2"); empty = local dev, auth off
  #13 hard stop    -> global_daily_cap_usd (hard 402, no graceful degrade in v1)
  #14 per-IP       -> ratelimit_* (token bucket keyed by client IP)
  #16 fail closed  -> enforcement is in the request path; a ledger read failure
                      REFUSES the request (503) rather than proceeding uncapped
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

# Named tier -> ordered failover chain of (provider, model). The chain order IS
# the failover policy (spec C4 axis b). Clients never send a model string.
DEFAULT_TIER_MAP: dict[str, list[tuple[str, str]]] = {
    "fast": [("anthropic", "claude-haiku-4-5-20251001"), ("openai", "gpt-4o-mini")],
    "quality": [("anthropic", "claude-sonnet-4-6"), ("openai", "gpt-4o")],
    "judge": [("anthropic", "claude-opus-4-8")],
}


def _parse_api_keys(raw: str) -> dict[str, str]:
    """"ic:secret1,evals:secret2" -> {"secret1": "ic", "secret2": "evals"}"""
    out: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        app, _, key = pair.partition(":")
        if app and key:
            out[key] = app
    return out


def _parse_tier_map(raw: str) -> dict[str, list[tuple[str, str]]]:
    data = json.loads(raw)
    return {tier: [(e["provider"], e["model"]) for e in chain]
            for tier, chain in data.items()}


@dataclass
class Config:
    # auth (decision #12)
    api_keys: dict[str, str] = field(default_factory=dict)  # key -> app name

    # tiers (decision #2)
    tier_map: dict[str, list[tuple[str, str]]] = field(
        default_factory=lambda: {k: list(v) for k, v in DEFAULT_TIER_MAP.items()})

    # spend caps (decisions #13 global hard stop, #5 per-user value)
    global_daily_cap_usd: float = 5.0
    user_daily_cap_usd: float | None = None  # None = no per-user cap

    # rate limiting (decision #14: per-IP token bucket)
    ratelimit_capacity: int = 20        # burst
    ratelimit_refill_per_s: float = 2.0  # sustained rps

    # cache (decisions #6, #8 -- ships OFF; flip on after a read-only baseline)
    cache_enabled: bool = False
    cache_ttl_s: int = 300

    # reliability (spec C2)
    retry_attempts: int = 3
    retry_base_delay_s: float = 0.2
    breaker_failure_threshold: int = 5
    breaker_cooldown_s: float = 30.0
    provider_timeout_s: float = 30.0

    # storage
    use_real_aws: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        c = cls()
        c.api_keys = _parse_api_keys(os.getenv("CONDUIT_API_KEYS", ""))
        if os.getenv("CONDUIT_TIER_MAP"):
            c.tier_map = _parse_tier_map(os.environ["CONDUIT_TIER_MAP"])
        c.global_daily_cap_usd = float(os.getenv("CONDUIT_GLOBAL_DAILY_CAP_USD", "5.0"))
        if os.getenv("CONDUIT_USER_DAILY_CAP_USD"):
            c.user_daily_cap_usd = float(os.environ["CONDUIT_USER_DAILY_CAP_USD"])
        c.ratelimit_capacity = int(os.getenv("CONDUIT_RATELIMIT_CAPACITY", "20"))
        c.ratelimit_refill_per_s = float(os.getenv("CONDUIT_RATELIMIT_REFILL_PER_S", "2.0"))
        c.cache_enabled = os.getenv("CONDUIT_CACHE_ENABLED", "0") == "1"
        c.cache_ttl_s = int(os.getenv("CONDUIT_CACHE_TTL_S", "300"))
        c.use_real_aws = os.getenv("CONDUIT_USE_REAL_AWS", "0") == "1"
        return c
