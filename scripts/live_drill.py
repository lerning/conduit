"""Live drill: trip the DEPLOYED gateway's protections over HTTP.

The in-process drill (gateway/load_drill.py) proves the mechanisms; this one
proves the deployment -- real network path, real auth, real durable ledger,
real deployed config. Everything runs on the $0 "drill" tier (mock-backed), so
no vendor is called and no money moves. Stdlib only, so it can run anywhere the
gateway is reachable -- including inside another Fly machine over 6PN:

    CONDUIT_URL=http://localhost:8200 CONDUIT_API_KEY=... python scripts/live_drill.py

Scenes:
  1. burst        one drill tenant floods -> 429s; a bystander drill tenant
                  stays served (the bulkhead, on the deployed limiter)
  2. outage       chaos marker makes the mock fail every call -> retries burn,
                  breaker opens -> 502s recorded
  3. recovery     after the breaker cooldown, a clean request succeeds again
                  (half-open probe closes the breaker)

The tenant-cap 402 needs a seeded spend row (mock costs $0), which requires
ledger access -- run scene "cap" separately after seeding, or skip it.

All traffic is under user ids prefixed drill_ so it is identifiable on the
dashboard, and every row it writes says provider=mock.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

URL = os.environ.get("CONDUIT_URL", "http://localhost:8200").rstrip("/")
KEY = os.environ.get("CONDUIT_API_KEY", "")
CHAOS_MARKER = "CHAOS_PROVIDER_DOWN"   # must match gateway/providers/mock.py


def post(user: str, text: str, timeout: float = 30.0) -> tuple[int, dict]:
    body = json.dumps({"tier": "drill", "user_id": user, "max_tokens": 64,
                       "messages": [{"role": "user", "content": text}]}).encode()
    req = urllib.request.Request(
        f"{URL}/v1/complete", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-API-Key": KEY})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read())
        except Exception:
            detail = {}
        return e.code, detail


def main() -> None:
    if not KEY:
        sys.exit("set CONDUIT_API_KEY (and CONDUIT_URL if not localhost:8200)")

    print(f"target: {URL}")
    tally: dict[str, int] = {}

    def note(label: str, code: int) -> None:
        tally[f"{label}:{code}"] = tally.get(f"{label}:{code}", 0) + 1

    print("\n--- 1/3 burst: drill_noisy floods, drill_quiet must stay served ---")
    for i in range(30):
        code, _ = post("drill_noisy", f"flood {i}")
        note("burst", code)
    quiet_code, _ = post("drill_quiet", "am I still served?")
    note("bystander", quiet_code)
    isolated = quiet_code == 200
    print(f"    noisy: {tally}")
    print(f"    bystander -> {quiet_code} ({'ISOLATED' if isolated else 'COLLATERAL DAMAGE'})")

    print("\n--- 2/3 outage: chaos marker downs the mock, breaker should open ---")
    saw_502 = False
    for i in range(8):
        code, d = post("drill_outage", f"{CHAOS_MARKER} attempt {i}")
        note("outage", code)
        saw_502 = saw_502 or code == 502
        time.sleep(1.2)                       # stay under the deployed rate limit
    print(f"    outage codes: { {k:v for k,v in tally.items() if k.startswith('outage')} }")

    print("\n--- 3/3 recovery: clean request after breaker cooldown (30s) ---")
    time.sleep(31)
    rec_code, d = post("drill_outage", "clean request, no marker")
    note("recovery", rec_code)
    print(f"    recovery -> {rec_code} ({d.get('routing_reason', d.get('detail', ''))})")

    ok = isolated and saw_502 and rec_code == 200
    print(f"\nresult: {'live gateway protections all fired and recovered' if ok else f'UNEXPECTED {tally}'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
