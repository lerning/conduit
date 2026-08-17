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


def test_live_route_auto_refreshes_and_is_never_cached():
    """A static ops page that looks identical at two seconds and two days old
    reads as a broken dashboard the first time traffic doesn't appear."""
    r = make_client().get("/v1/dashboard")
    assert 'http-equiv="refresh" content="30"' in r.text
    assert "refreshing every 30s" in r.text
    assert "no-store" in r.headers["cache-control"]


def test_auto_refresh_can_be_paused_and_resumed_from_the_page():
    client = make_client()
    live = client.get("/v1/dashboard").text
    assert 'href="?hours=24&amp;refresh=0"' in live      # pause link

    paused = client.get("/v1/dashboard?refresh=0")
    assert 'http-equiv="refresh"' not in paused.text     # timer really is off
    assert "auto-refresh off" in paused.text
    assert 'href="?hours=24&amp;refresh=30"' in paused.text  # resume link


def test_window_links_preserve_the_refresh_setting():
    page = make_client().get("/v1/dashboard?hours=1&refresh=0").text
    assert 'href="?hours=168&amp;refresh=0"' in page
    assert "Last 1h" in page


def test_file_snapshot_says_it_is_a_snapshot():
    page = _html(make_client())            # refresh_s defaults to 0
    assert "http-equiv=\"refresh\"" not in page
    assert "does not update" in page


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


# --- cost table: these numbers ARE the spend cap ---------------------------

def test_opus_is_priced_at_its_published_rate():
    """Regression: Opus was priced at $15/$75 per MTok when the real rate is
    $5/$25 -- a 3x overcharge that made the cap trip at a third of the budget
    and every reported cost figure wrong."""
    from gateway.telemetry.ledger import compute_cost
    # 1M in + 1M out at $5/$25
    assert compute_cost("claude-opus-4-8", 1_000_000, 1_000_000) == 30.0
    assert compute_cost("claude-opus-5", 1_000_000, 1_000_000) == 30.0
    assert compute_cost("claude-sonnet-4-6", 1_000_000, 1_000_000) == 18.0
    assert compute_cost("claude-haiku-4-5", 1_000_000, 1_000_000) == 6.0


def test_unpriced_model_is_charged_not_free():
    """An unpriced model used to cost $0 -- it consumed no budget, so the cap
    could never stop it. Fail-open in a fail-closed system."""
    from gateway.telemetry.ledger import compute_cost
    assert compute_cost("some-model-shipped-tomorrow", 1_000_000, 0) > 0


def test_an_unpriced_model_cannot_slip_past_the_cap():
    cfg = Config(global_daily_cap_usd=0.01,
                 tier_map={"fast": [("mock", "not-in-the-cost-table")]})
    client = TestClient(create_app(cfg))
    r = client.post("/v1/complete", json={
        "tier": "fast", "user_id": "u",
        "messages": [{"role": "user", "content": "x" * 4000}], "max_tokens": 1000})
    assert r.status_code == 200
    assert r.json()["cost_usd"] > 0          # charged, so the cap can see it


# --- cache panel: distinguishing "broken" from "nothing repeated" ------------

def test_cache_skip_reason_is_recorded():
    client = make_client(cache_enabled=True)
    _post(client, text="repeat me")                       # miss -> cached
    _post(client, text="repeat me")                       # hit
    r = client.post("/v1/complete", json={
        "tier": "quality", "user_id": "joshua", "cache_bypass": True,
        "messages": [{"role": "user", "content": "repeat me"}]})
    assert r.status_code == 200
    rows = client.app.state.ledger.scan_all()
    skips = sorted(str(x.get("cache_skip", "")) for x in rows)
    assert skips == ["", "bypassed", "missed"]            # hit, bypass, miss


def test_cache_panel_separates_bypass_from_miss():
    client = make_client(cache_enabled=True)
    _post(client, text="alpha")                                        # miss
    _post(client, text="alpha")                                        # hit
    client.post("/v1/complete", json={
        "tier": "quality", "user_id": "joshua", "cache_bypass": True,
        "messages": [{"role": "user", "content": "beta"}]})            # bypass
    page = _html(client)
    assert "1 hit" in page and "1 unique miss" in page
    assert "1 bypassed by caller" in page
    # hit rate is over ELIGIBLE requests (hit+miss), so the bypass can't
    # deflate it: 1 of 2 eligible = 50%, not 1 of 3 = 33%.
    assert "50%" in page


# --- chaos marker + drill tier: the seams the live drill stands on -----------

def test_chaos_marker_downs_the_mock_and_opens_the_breaker():
    from gateway.providers.mock import CHAOS_MARKER
    # default tier map (includes "drill"), not the module fixture's override
    client = TestClient(create_app(Config(breaker_failure_threshold=2,
                                          ratelimit_capacity=50)))
    codes = [client.post("/v1/complete", json={
        "tier": "drill", "user_id": "d",
        "messages": [{"role": "user", "content": f"{CHAOS_MARKER} {i}"}],
    }).status_code for i in range(3)]
    assert all(c == 502 for c in codes)
    assert client.app.state.breakers.states().get("mock") == "open"
    # and a clean request without the marker is refused only by the breaker,
    # not by the marker logic itself
    r = client.post("/v1/complete", json={
        "tier": "drill", "user_id": "d",
        "messages": [{"role": "user", "content": "clean"}]})
    assert r.status_code == 502          # circuit still open (cooldown not elapsed)


def test_drill_tier_exists_and_is_free():
    client = TestClient(create_app(Config()))   # default tier map has "drill"
    r = client.post("/v1/complete", json={
        "tier": "drill", "user_id": "d",
        "messages": [{"role": "user", "content": "ping"}]})
    assert r.status_code == 200
    assert r.json()["provider"] == "mock"
    assert r.json()["cost_usd"] == 0.0
