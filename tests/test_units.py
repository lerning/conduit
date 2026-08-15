"""Unit tests: providers, retry, circuit breaker, token bucket, exact cache,
router failover, ledger. Everything runs against the deterministic mock --
zero network, zero AWS, zero spend."""
from __future__ import annotations

import asyncio

import pytest

from gateway.cache.exact import ExactCache, request_hash
from gateway.providers.base import CompletionRequest, Message, ProviderUnavailable
from gateway.providers.mock import MockProvider, fail_rate_limited, fail_unavailable
from gateway.ratelimit.token_bucket import TokenBucketLimiter
from gateway.reliability.circuit_breaker import CLOSED, HALF_OPEN, OPEN, BreakerRegistry, CircuitBreaker
from gateway.reliability.retry import with_retry
from gateway.routing.tiers import AllProvidersFailed, Router, UnknownTier
from gateway.storage import FakeTable
from gateway.telemetry.ledger import LedgerEntry, LedgerStore, compute_cost


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


REQ = CompletionRequest(model="m", messages=[Message(role="user", content="hello world")])


# --- mock provider -------------------------------------------------------------
def test_mock_is_deterministic():
    p = MockProvider()
    r1 = run(p.complete(REQ))
    r2 = run(p.complete(REQ))
    assert r1.text == r2.text and r1.usage.input_tokens == r2.usage.input_tokens


def test_mock_fault_script_consumed_in_order():
    p = MockProvider()
    p.queue_failure(fail_unavailable, times=2)
    with pytest.raises(ProviderUnavailable):
        run(p.complete(REQ))
    with pytest.raises(ProviderUnavailable):
        run(p.complete(REQ))
    assert run(p.complete(REQ)).text  # script exhausted -> success


def test_mock_streaming_reassembles_to_complete_text():
    p = MockProvider()
    async def collect():
        text, final_usage = "", None
        async for c in p.stream(REQ):
            text += c.text
            if c.is_final:
                final_usage = c.usage
        return text, final_usage
    text, usage = run(collect())
    assert text == run(p.complete(REQ)).text
    assert usage and usage.output_tokens > 0


# --- retry ----------------------------------------------------------------------
def test_retry_recovers_after_transient_failure():
    p = MockProvider()
    p.queue_failure(fail_rate_limited, times=1)
    async def noop_sleep(_): pass
    resp = run(with_retry(lambda: p.complete(REQ), attempts=3, sleep=noop_sleep))
    assert resp.text


def test_retry_exhausts_and_raises_last_error():
    p = MockProvider()
    p.queue_failure(fail_unavailable, times=5)
    async def noop_sleep(_): pass
    with pytest.raises(ProviderUnavailable):
        run(with_retry(lambda: p.complete(REQ), attempts=3, sleep=noop_sleep))


# --- circuit breaker -------------------------------------------------------------
def test_breaker_opens_cools_probes_and_recovers():
    t = [0.0]
    b = CircuitBreaker(failure_threshold=3, cooldown_s=10.0, clock=lambda: t[0])
    assert b.state == CLOSED
    for _ in range(3):
        b.record_failure()
    assert b.state == OPEN and not b.allow()
    t[0] = 11.0                       # cooldown elapses -> half-open
    assert b.state == HALF_OPEN
    assert b.allow()                  # exactly one probe
    assert not b.allow()              # second concurrent probe denied
    b.record_success()
    assert b.state == CLOSED and b.allow()


def test_breaker_failed_probe_reopens():
    t = [0.0]
    b = CircuitBreaker(failure_threshold=1, cooldown_s=5.0, clock=lambda: t[0])
    b.record_failure()
    assert b.state == OPEN
    t[0] = 6.0
    assert b.allow()                  # probe
    b.record_failure()                # probe fails
    assert b.state == OPEN and not b.allow()


# --- token bucket ----------------------------------------------------------------
def test_bucket_enforces_burst_then_refills():
    t = [0.0]
    lim = TokenBucketLimiter(capacity=2, refill_per_s=1.0, clock=lambda: t[0])
    assert lim.consume("ip1") and lim.consume("ip1")
    assert not lim.consume("ip1")     # burst exhausted
    assert lim.consume("ip2")         # other key unaffected
    t[0] = 1.5                        # refill 1.5 tokens
    assert lim.consume("ip1")
    assert not lim.consume("ip1")


# --- exact cache ------------------------------------------------------------------
def test_cache_hit_miss_ttl_and_bypass():
    t = [0.0]
    c = ExactCache(enabled=True, ttl_s=10, clock=lambda: t[0])
    k = request_hash("fast", [{"role": "user", "content": "hi"}], 100, 0.0)
    assert c.get(k) is None
    c.put(k, {"text": "cached"})
    assert c.get(k) == {"text": "cached"}
    assert c.get(k, bypass=True) is None          # decision #6: bypass forces live
    t[0] = 11.0
    assert c.get(k) is None                        # TTL expired
    disabled = ExactCache(enabled=False)
    disabled.put(k, {"text": "x"})
    assert disabled.get(k) is None                 # decision #8: ships off


# --- router / failover -------------------------------------------------------------
def _router(providers, tier_map):
    return Router(providers, tier_map, BreakerRegistry(failure_threshold=2, cooldown_s=60),
                  retry_attempts=1)


def test_router_unknown_tier():
    r = _router({"mock": MockProvider()}, {"fast": [("mock", "m1")]})
    with pytest.raises(UnknownTier):
        run(r.complete("nope", REQ))


def test_router_fails_over_to_next_provider():
    a, b = MockProvider(name="mockA"), MockProvider(name="mockB")
    a.queue_failure(fail_unavailable, times=10)
    r = _router({"mockA": a, "mockB": b},
                {"fast": [("mockA", "m1"), ("mockB", "m2")]})
    routed = run(r.complete("fast", REQ))
    assert routed.response.provider == "mockB"
    assert "failover_from" in routed.routing_reason


def test_router_substitutes_mock_for_unconfigured_provider():
    r = _router({"mock": MockProvider()}, {"fast": [("anthropic", "claude-x")]})
    routed = run(r.complete("fast", REQ))
    assert routed.response.provider == "mock"
    assert "mock_for:anthropic" in routed.routing_reason


def test_router_all_failed():
    a = MockProvider(name="mockA")
    a.queue_failure(fail_unavailable, times=10)
    r = _router({"mockA": a}, {"fast": [("mockA", "m1")]})
    with pytest.raises(AllProvidersFailed):
        run(r.complete("fast", REQ))


# --- ledger -----------------------------------------------------------------------
def test_ledger_roundtrip_and_day_totals():
    store = LedgerStore(FakeTable())
    e = LedgerEntry(client_id="ic:joshua", model="claude-sonnet-4-6",
                    provider="anthropic", input_tokens=1000, output_tokens=500,
                    cost_usd=compute_cost("claude-sonnet-4-6", 1000, 500))
    store.put(e)
    got = store.get(e.request_id)
    assert got and got["client_id"] == "ic:joshua"
    totals = store.day_totals()
    assert totals["requests"] == 1
    assert totals["spend_usd"] == pytest.approx(0.003 + 0.0075)
    assert store.day_totals(client_prefix="ic:joshua")["requests"] == 1
    assert store.day_totals(client_prefix="other:")["requests"] == 0


def test_cost_table_math():
    assert compute_cost("claude-haiku-4-5-20251001", 1000, 1000) == pytest.approx(0.006)
    # An unpriced model must NOT be free: cost 0 means it consumes no budget,
    # so the spend cap can never stop it. This assertion used to require 0.0 --
    # it was pinning a fail-open hole in place.
    assert compute_cost("unknown-model", 99999, 99999) > 0.0


# --- sqlite durable ledger (D2) ---------------------------------------------------
def test_sqlite_table_persists_across_reopen(tmp_path):
    from gateway.storage import SqliteTable
    db = str(tmp_path / "ledger.db")
    store1 = LedgerStore(SqliteTable(db))
    e = LedgerEntry(client_id="ic:joshua", model="claude-haiku-4-5-20251001",
                    provider="anthropic", input_tokens=100, output_tokens=50,
                    cost_usd=compute_cost("claude-haiku-4-5-20251001", 100, 50))
    store1.put(e)
    # simulate a process restart: brand-new connection to the same file
    store2 = LedgerStore(SqliteTable(db))
    row = store2.get(e.request_id)
    assert row is not None and row["client_id"] == "ic:joshua"
    totals = store2.day_totals()
    assert totals["requests"] == 1 and totals["spend_usd"] > 0  # cap survives restart


# --- structured json passthrough ---------------------------------------------------
def test_mock_json_response_is_valid_json():
    import json as _json
    p = MockProvider()
    req = CompletionRequest(model="m", json_response=True,
                            messages=[Message(role="user", content="give me json")])
    resp = run(p.complete(req))
    data = _json.loads(resp.text)
    assert data["mock"] is True and data["model"] == "m"
