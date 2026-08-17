"""Onboard a new app onto the gateway: one command, no gateway code changes.

    python scripts/onboard_app.py spanish --cap 0.25

Prints everything the operator needs: the freshly minted key, the exact
`fly secrets set` command (run it yourself -- the key must not transit through
anything that logs), the per-app cap line, and the three config lines the new
app needs. The gateway itself needs NO code change and NO redeploy beyond the
secrets update: keys and caps are env-driven by design (decisions #12, #13).

Never prints or stores existing keys -- the CONDUIT_API_KEYS value shown is a
template with a placeholder where the current entries go.
"""
from __future__ import annotations

import argparse
import secrets


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("app_name", help="short app slug, e.g. 'spanish'")
    ap.add_argument("--cap", type=float, default=0.25,
                    help="per-app daily USD cap (default 0.25)")
    a = ap.parse_args()
    name = a.app_name.strip().lower()
    if not name.isidentifier():
        raise SystemExit(f"app name {name!r} should be a simple slug (letters/digits/_)")

    key = f"{name}-{secrets.token_hex(24)}"

    print(f"""
── onboarding '{name}' ────────────────────────────────────────────────

1. Add the key to the gateway (run yourself -- keep existing entries!):

   fly secrets set -a conduit-gateway \\
     CONDUIT_API_KEYS="<EXISTING_ENTRIES>,{name}:{key}"

   (fetch the current value's entries from wherever you keep them; secrets
    are write-only on Fly, so keep a copy in your password manager)

2. Give the app its own daily budget -- edit fly.toml [env]:

   CONDUIT_APP_DAILY_CAPS = "ic:0.75,{name}:{a.cap}"

   then: fly deploy -a conduit-gateway

3. Config for the new app (its .env / Fly secrets):

   CONDUIT_URL=http://conduit-gateway.internal:8200
   CONDUIT_API_KEY={key}

4. Smoke test from any machine in the Fly org:

   curl -s -X POST http://conduit-gateway.internal:8200/v1/complete \\
     -H 'Content-Type: application/json' -H 'X-API-Key: {key}' \\
     -d '{{"tier":"drill","user_id":"onboard_test","messages":[{{"role":"user","content":"ping"}}]}}'

   Expect provider=mock, cost_usd=0.0, and the request on the dashboard
   under tenant '{name}:onboar…'.

The key above was generated locally and printed ONCE. Store it now.
───────────────────────────────────────────────────────────────────────
""")


if __name__ == "__main__":
    main()
