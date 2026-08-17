# Onboarding a new app onto Conduit

Adding a consumer to the gateway is one script run plus one secrets update —
**no gateway code changes**. Keys and caps are env-driven by design
(decisions #12, #13).

## The 15-minute path

```bash
# 1. Mint a key and get the exact commands to run
python scripts/onboard_app.py <app-slug> --cap 0.25
```

The script prints four things; do them in order:

1. **`fly secrets set ... CONDUIT_API_KEYS="..."`** — appends the new
   `app:key` pair. Fly secrets are write-only, so keep the full value in your
   password manager. Setting a secret restarts the machine (~5s blip).
2. **Per-app cap** — add `<app>:<usd>` to `CONDUIT_APP_DAILY_CAPS` in
   `fly.toml` and `fly deploy`. This is the middle layer of the bulkhead
   hierarchy (user → app → global): a runaway app exhausts its own budget,
   never the gateway's.
3. **App-side config** — two lines: `CONDUIT_URL` (the 6PN hostname) and its
   API key. The app must run in the same Fly org; Conduit has no public
   listener.
4. **Smoke test** — a `drill`-tier request ($0, mock-backed, auth enforced).
   The new tenant appears on the dashboard immediately.

## What the new app gets, with zero code in the gateway

- Named tiers (`fast` / `quality` / `judge` / `drill`) — never model strings
- Retry + circuit breaker + provider failover
- Rate limiting per `app:user`
- Spend enforcement: its users' caps, its own app cap, the global hard stop —
  all fail-closed
- Its traffic on `/v1/dashboard`, attributed by app prefix
- Exact cache with per-request `cache_bypass`
- `GET /v1/usage` for an in-app spend meter

## Client integration

Don't re-write the HTTP shim — crib `ConduitBackend` from Inner Council's
`app/llm_client.py` (~80 lines: POST `/v1/complete`, SSE streaming, the
`json_response` structured mode, and error mapping). Send an opaque
pseudonymous `user_id`; Conduit never sees message content in its ledger and
should never see real names in tenant ids either.

## Removing an app

Remove its entry from `CONDUIT_API_KEYS` (secrets update) and its cap line
from `fly.toml`. Its ledger history remains (metadata only) and ages out of
the dashboard windows naturally.
