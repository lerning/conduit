"""Dashboard + load drill.

The dashboard's whole job is to be *believable*, so what is tested here is the
things a viewer would take at face value: that refused requests are counted as
refused rather than as work, that no message content can reach the page, and
that the tenant bulkhead really isolates -- one tenant flooding must not cost
another tenant a single request.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import Config
from gateway.dashboard import _short, build_html
from gateway.telemetry.ledger import (OUTCOME_RATE_LIMITED, REJECTIONS,
                                      LedgerEntry)

TIERS = {"fast": [("mock", "claude-haiku-4-5-20251001")],
         "quality": [("mock", "claude-sonnet-4-6")]}


def make_client(**overrides) -> TestClient:
    cfg = Config(tier_map={k: list(v) for k, v in TIERS.items()}, **overrides)
    return TestClient(create_app(cfg))


def _post(client, user="joshua", text="hello", tier="quality"):
    return client.post("/v1/complete", json={
        "tier": tier, "user_id": user, "messages": [{"role": "user", "content": text}]})


def _html(client, **kw) -> str:
    s = client.app.state
    return build_html(s.ledger, s.config, s.cache, s.breakers, **kw)


# --- rejections are recorded, and counted as refusals not as work -------------

def test_rate_limited_request_lands_in_ledger_as_a_rejection():
    client = make_client(ratelimit_capacity=1, ratelimit_refill_per_s=0.0)
    assert _post(client).status_code == 200
    assert _post(client).status_code == 429

    rows = client.app.state.ledger.scan_all()
    refused = [r for r in rows if str(r["outcome"]) in REJECTIONS]
    assert len(refused) == 1
    assert refused[0]["outcome"] == OUTCOME_RATE_LIMITED
    assert refused[0]["status_code"] == 429
    assert float(refused[0]["cost_usd"]) == 0.0     # a refusal never moves spend


def test_refusals_do_not_inflate_the_request_count():
    client = make_client(ratelimit_capacity=1, ratelimit_refill_per_s=0.0)
    _post(client)
    for _ in range(3):
        _post(client)
    totals = client.app.state.ledger.day_totals()
    assert totals["requests"] == 1        # served
    assert totals["rejected"] == 3


def test_cap_rejection_is_attributed_to_the_tenant_that_hit_it():
    client = make_client(global_daily_cap_usd=0.0001)
    _post(client, user="over_budget")
    assert _post(client, user="over_budget").status_code == 402
    refused = [r for r in client.app.state.ledger.scan_all()
               if str(r["outcome"]) in REJECTIONS]
    assert refused and refused[0]["client_id"] == "dev:over_budget"


# --- the bulkhead: the assertion the dashboard's tenant panel rests on --------

def test_one_tenant_flooding_does_not_throttle_another():
    """Per-IP limiting would fail this: on a private network every request
    arrives from the same address, so both tenants would share one bucket."""
    client = make_client(ratelimit_capacity=3, ratelimit_refill_per_s=0.0)
    codes = [_post(client, user="noisy").status_code for _ in range(6)]
    assert 429 in codes
    assert _post(client, user="quiet").status_code == 200


# --- privacy ------------------------------------------------------------------

def test_dashboard_cannot_render_message_content():
    client = make_client()
    secret = "my therapist said something confidential about Marcus"
    _post(client, text=secret)
    page = _html(client).lower()
    assert secret.lower() not in page
    for word in ("therapist", "confidential", "marcus"):
        assert word not in page


def test_tenant_ids_are_abbreviated_for_display():
    assert _short("ic:u_a080f989a3") == "ic:u_a080…"
    assert _short("ic:bob") == "ic:bob"          # too short to be worth cutting


# --- rendering ----------------------------------------------------------------

def test_dashboard_renders_on_an_empty_ledger():
    page = _html(make_client())
    assert page.startswith("<!DOCTYPE html>") and "charset" in page
    assert "no traffic" in page


def test_dashboard_route_is_served_and_can_be_disabled():
    client = make_client()
    r = client.get("/v1/dashboard")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "Gateway Operations" in r.text

    off = make_client(dashboard_enabled=False)
    assert off.get("/v1/dashboard").status_code == 404


def test_dashboard_separates_served_from_refused_in_the_header():
    client = make_client(ratelimit_capacity=2, ratelimit_refill_per_s=0.0)
    _post(client, user="a")
    _post(client, user="a")
    _post(client, user="a")                       # refused
    page = _html(client)
    assert "1 refused" in page or ">1<" in page   # header counts, not lumped in


def test_banner_is_escaped_and_shown():
    page = _html(make_client(), banner="drill <b>x</b>")
    assert "drill &lt;b&gt;x&lt;/b&gt;" in page


def test_provider_recorded_is_the_chain_slot_not_the_implementation_name():
    """Breakers, failover and the tier map are all keyed on the chain slot. If
    the ledger stored the provider object's self-reported name instead, one
    provider would split into two rows on the reliability panel."""
    cfg = Config(tier_map={"fast": [("primary", "claude-haiku-4-5-20251001")]})
    app = create_app(cfg)
    from gateway.providers.mock import MockProvider
    app.state.providers["primary"] = MockProvider()
    client = TestClient(app)
    r = client.post("/v1/complete", json={"tier": "fast", "user_id": "u",
                                          "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    row = app.state.ledger.get(r.json()["request_id"])
    assert row["provider"] == "primary"
    assert "primary" in app.state.breakers.states()


def test_latency_percentiles_ignore_cache_hits():
    client = make_client(cache_enabled=True)
    s = client.app.state
    s.ledger.put(LedgerEntry(client_id="dev:a", latency_ms=800.0, model="mock"))
    s.ledger.put(LedgerEntry(client_id="dev:a", latency_ms=0.0, cache_hit=True,
                             model="mock"))
    page = _html(client)
    assert "800 ms" in page      # the cached 0.0 must not drag p50 down


# --- the drill itself ---------------------------------------------------------

@pytest.mark.slow
def test_load_drill_trips_every_protection():
    from gateway.load_drill import run
    s = run(db_path=None, verbose=False)["summary"]
    assert s["tenant_isolated"], "a flooding tenant took down a bystander"
    assert s["by_outcome"].get("rate_limited", 0) > 0
    assert s["by_outcome"].get("cap_user", 0) == 1
    assert s["by_outcome"].get("provider_failed", 0) > 0
    assert s["failovers"] > 0
    assert "open" in s["breakers"].values()
