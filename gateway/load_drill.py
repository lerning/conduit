"""Load drill: deliberately trip every protection, so the dashboard shows them
working instead of sitting at zero.

A rate limiter that has never rejected anything is a claim, not evidence. This
drives traffic through the real app -- real limiter, real cap enforcement, real
circuit breaker, real ledger -- against MOCK providers, so it calls no vendor and
costs $0. Everything it produces lands in the ledger and renders on the dashboard.

    python -m gateway.load_drill
    python -m gateway.load_drill --db data/drill.db --dashboard docs/dashboard.html

Four scenes:
  1. normal traffic   three tenants at different intensities -> concentration
  2. burst            one tenant floods -> 429s for them, and the assertion that
                      matters: a bystander tenant is still served (the bulkhead)
  3. tenant cap       one tenant passes its own daily cap -> 402
  4. outage           primary provider fails every call -> failover to the
                      secondary, then both breakers open -> 502

Tokens are priced at published rates for a real model name so the spend panel is
shaped like production, but no provider is contacted and no money moves. Snapshots
written by this drill carry a banner saying so.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from gateway.app import create_app  # noqa: E402
from gateway.config import Config  # noqa: E402
from gateway.providers.base import ProviderError  # noqa: E402
from gateway.providers.mock import MockProvider  # noqa: E402
from gateway.telemetry.ledger import REJECTIONS, LedgerEntry  # noqa: E402

TENANTS = ["u_a080f989a3", "u_47c1be0d22", "u_9f3e10b7c5", "u_2b6d84ff10"]
BANNER = ("Synthetic load drill — mock providers, no vendor calls, $0 spent. "
          "Tokens are priced at published rates so the shape is realistic.")


class OutageProvider:
    """Fails every call with a ProviderError -- which is what the router treats
    as failover-worthy, and what the breaker counts."""

    def complete(self, *a, **k):
        raise ProviderError("simulated provider outage")

    def stream(self, *a, **k):
        raise ProviderError("simulated provider outage")


def _post(client: TestClient, user: str, text: str, tier: str = "fast",
          stream: bool = False) -> int:
    r = client.post("/v1/complete", json={
        "tier": tier, "user_id": user, "max_tokens": 200, "stream": stream,
        "messages": [{"role": "user", "content": text}]})
    return r.status_code


def _drill_config(db_path: str | None) -> Config:
    cfg = Config()
    cfg.api_keys = {}                      # local drill: auth off, app name "dev"
    cfg.db_path = db_path
    cfg.user_daily_cap_usd = 0.05          # low enough that scene 3 can reach it
    cfg.global_daily_cap_usd = 100.0       # keep the global cap out of the way
    cfg.ratelimit_capacity = 8
    cfg.ratelimit_refill_per_s = 1.0
    cfg.breaker_failure_threshold = 3
    cfg.breaker_cooldown_s = 30.0
    cfg.dashboard_enabled = True
    # Two DISTINCT provider names so failover is observable: one chain, two
    # breakers. Both are mocks; the model name is real only for pricing.
    cfg.tier_map = {"fast": [("primary", "claude-haiku-4-5-20251001"),
                             ("secondary", "claude-haiku-4-5-20251001")],
                    "quality": [("primary", "claude-sonnet-4-6")]}
    return cfg


def run(db_path: str | None = None, verbose: bool = True) -> dict:
    app = create_app(_drill_config(db_path))
    app.state.providers["primary"] = MockProvider()
    app.state.providers["secondary"] = MockProvider()
    cfg = app.state.config
    client = TestClient(app)
    codes: dict[str, int] = {}

    def scene(name: str) -> None:
        if verbose:
            print(f"\n--- {name} ---")

    def tally(label: str, code: int) -> None:
        codes[f"{label}:{code}"] = codes.get(f"{label}:{code}", 0) + 1

    # 1. baseline, deliberately uneven -> the concentration story
    scene("1/4  normal traffic (3 tenants, uneven)")
    for i, (user, n) in enumerate(zip(TENANTS, (6, 3, 1))):
        for j in range(n):
            tally("normal", _post(client, user, f"reflection {i}-{j}",
                                  tier="quality" if j == 0 else "fast",
                                  stream=(j == 1)))
        time.sleep(0.05)

    # 2. one tenant bursts. The point is not that a 429 happened -- it is that
    #    the OTHER tenants were unaffected. Per-IP limiting would fail this.
    scene("2/4  burst from one tenant (expect 429s for them, 200 for a bystander)")
    noisy, bystander = TENANTS[0], TENANTS[1]
    for j in range(25):
        tally("burst", _post(client, noisy, f"flood {j}"))
    bystander_code = _post(client, bystander, "am I still served?")
    tally("bystander", bystander_code)
    isolated = bystander_code == 200
    if verbose:
        print(f"    bystander during the flood -> {bystander_code} "
              f"({'ISOLATED' if isolated else 'COLLATERAL DAMAGE'})")

    # 3. push a tenant past its own daily cap. Mock output is cheap, so seed the
    #    spend directly -- what is under test is the enforcement path.
    scene("3/4  tenant passes its own daily cap (expect 402)")
    capped = TENANTS[2]
    app.state.ledger.put(LedgerEntry(
        client_id=f"dev:{capped}", model="claude-sonnet-4-6", provider="primary",
        input_tokens=1200, output_tokens=900, cost_usd=cfg.user_daily_cap_usd,
        routing_reason="load drill: seeded spend"))
    time.sleep(1.1)                        # let the bucket refill: want a 402, not a 429
    tally("capped", _post(client, capped, "one more please"))

    # 4. provider outage -> failover, then both breakers open -> 502
    scene("4/4  primary provider outage (expect failover, then breakers open)")
    healthy = app.state.providers["primary"]
    app.state.providers["primary"] = OutageProvider()
    outage_user = TENANTS[3]
    try:
        for j in range(4):
            tally("outage", _post(client, outage_user, f"during outage {j}"))
            time.sleep(0.4)
        app.state.providers["secondary"] = OutageProvider()   # now nothing is left
        for j in range(4):
            tally("outage_total", _post(client, outage_user, f"total outage {j}"))
            time.sleep(0.4)
    finally:
        app.state.providers["primary"] = healthy
        app.state.providers["secondary"] = MockProvider()

    rows = app.state.ledger.rows_since(0)
    served = [r for r in rows if str(r.get("outcome", "ok")) not in REJECTIONS]
    refused = [r for r in rows if str(r.get("outcome", "ok")) in REJECTIONS]
    by_outcome: dict[str, int] = {}
    for r in refused:
        o = str(r.get("outcome"))
        by_outcome[o] = by_outcome.get(o, 0) + 1

    summary = {
        "requests": len(rows), "served": len(served), "refused": len(refused),
        "by_outcome": by_outcome,
        "tenant_isolated": isolated,
        "failovers": sum(1 for r in served
                         if "failover_from" in str(r.get("routing_reason", ""))),
        "breakers": app.state.breakers.states(),
        "codes": codes,
    }
    if verbose:
        print(f"    breakers -> {summary['breakers']}")
        print(f"\n{len(rows)} ledger rows: {len(served)} served, {len(refused)} refused, "
              f"{summary['failovers']} failed over")
        for o, n in sorted(by_outcome.items()):
            print(f"    {o:<18} {n}")
    return {"summary": summary, "app": app}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="SQLite path (default: in-memory)")
    ap.add_argument("--dashboard", default=None, help="write a dashboard snapshot")
    a = ap.parse_args()

    out = run(a.db)
    app, s = out["app"], out["summary"]
    if a.dashboard:
        from gateway.dashboard import build_html
        p = Path(a.dashboard)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(build_html(app.state.ledger, app.state.config, app.state.cache,
                                app.state.breakers, banner=BANNER), encoding="utf-8")
        print(f"\ndashboard -> {p.resolve()}")

    ok = (s["refused"] > 0 and s["tenant_isolated"] and s["failovers"] > 0
          and "open" in s["breakers"].values())
    print("\ndrill result: " + ("every protection fired and tenants stayed isolated"
                                if ok else f"UNEXPECTED {s}"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
