"""End-to-end tests through the HTTP service (mock provider, zero spend).

Each test pins one of the integration decisions to observable HTTP behavior:
auth (#12), per-IP rate limit (#14), global + per-user hard stops (#13, #5),
fail-closed enforcement (#16), tier-not-model (#2), cache + bypass (#6, #8),
metadata-only ledger (privacy rule), and the usage meter endpoint (#13).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import Config

TIERS = {"fast": [("mock", "claude-haiku-4-5-20251001")],
         "quality": [("mock", "claude-sonnet-4-6")]}


def make_client(**overrides) -> TestClient:
    cfg = Config(tier_map={k: list(v) for k, v in TIERS.items()}, **overrides)
    return TestClient(create_app(cfg))


BODY = {"tier": "quality", "user_id": "joshua",
        "messages": [{"role": "user", "content": "hello there"}]}


def test_complete_roundtrip_and_ledger_row_lands():
    client = make_client()
    r = client.post("/v1/complete", json=BODY)
    assert r.status_code == 200
    data = r.json()
    assert data["provider"] == "mock" and data["model"] == "claude-sonnet-4-6"
    assert data["cost_usd"] > 0 and not data["cache_hit"]
    # P0 acceptance: the ledger row landed -- and holds METADATA ONLY
    ledger = client.app.state.ledger
    row = ledger.get(data["request_id"])
    assert row is not None and row["client_id"] == "dev:joshua"
    assert "hello there" not in str(row)  # never message content


def test_auth_required_when_keys_configured():
    client = make_client(api_keys={"sekrit": "ic"})
    assert client.post("/v1/complete", json=BODY).status_code == 401
    r = client.post("/v1/complete", json=BODY, headers={"X-API-Key": "sekrit"})
    assert r.status_code == 200


def test_unknown_tier_is_400_and_no_model_param_exists():
    client = make_client()
    r = client.post("/v1/complete", json={**BODY, "tier": "gpt-4o"})
    assert r.status_code == 400  # decision #2: tiers only; a model name is not a tier


def test_per_ip_rate_limit_429():
    client = make_client(ratelimit_capacity=2, ratelimit_refill_per_s=0.0001)
    assert client.post("/v1/complete", json=BODY).status_code == 200
    assert client.post("/v1/complete", json=BODY).status_code == 200
    assert client.post("/v1/complete", json=BODY).status_code == 429


def test_global_daily_hard_stop_402():
    client = make_client(global_daily_cap_usd=0.000001)
    assert client.post("/v1/complete", json=BODY).status_code == 200  # first spend lands
    r = client.post("/v1/complete", json=BODY)
    assert r.status_code == 402
    assert "daily spend limit" in r.json()["detail"]


def test_per_user_cap_isolates_users():
    client = make_client(user_daily_cap_usd=0.000001, global_daily_cap_usd=100.0)
    assert client.post("/v1/complete", json=BODY).status_code == 200
    assert client.post("/v1/complete", json=BODY).status_code == 402  # joshua capped
    other = {**BODY, "user_id": "friend2"}
    assert client.post("/v1/complete", json=other).status_code == 200  # friend2 fine


def test_fail_closed_when_ledger_unreadable():
    client = make_client()
    def boom(*a, **k):
        raise RuntimeError("dynamo down")
    client.app.state.ledger.day_totals = boom  # decision #16
    r = client.post("/v1/complete", json=BODY)
    assert r.status_code == 503
    assert "fail-closed" in r.json()["detail"]


def test_cache_hit_and_bypass():
    client = make_client(cache_enabled=True)
    first = client.post("/v1/complete", json=BODY).json()
    assert first["cache_hit"] is False
    second = client.post("/v1/complete", json=BODY).json()
    assert second["cache_hit"] is True and second["cost_usd"] == 0.0
    third = client.post("/v1/complete", json={**BODY, "cache_bypass": True}).json()
    assert third["cache_hit"] is False  # decision #6: drift runs force live calls


def test_cache_disabled_by_default():
    client = make_client()  # decision #8
    client.post("/v1/complete", json=BODY)
    assert client.post("/v1/complete", json=BODY).json()["cache_hit"] is False


def test_usage_meter_endpoint():
    client = make_client(global_daily_cap_usd=5.0)
    client.post("/v1/complete", json=BODY)
    u = client.get("/v1/usage", params={"user_id": "joshua"}).json()
    assert u["requests_today"] == 1
    assert 0 < u["spend_today_usd"] < 5.0
    assert u["remaining_usd"] == pytest.approx(5.0 - u["spend_today_usd"], abs=1e-6)
    assert u["user"]["requests"] == 1


def test_streaming_sse_chunks_then_final_with_ledger():
    client = make_client()
    with client.stream("POST", "/v1/complete", json={**BODY, "stream": True}) as r:
        assert r.status_code == 200
        text = "".join(r.iter_text())
    assert "event: chunk" in text and "event: final" in text
    assert client.get("/v1/usage").json()["requests_today"] == 1


def test_health_reports_shape():
    client = make_client()
    h = client.get("/health").json()
    assert h["status"] == "ok"
    assert "mock" in h["providers"]
    assert set(h["tiers"]) == {"fast", "quality"}
