# Conduit

A provider-agnostic **LLM gateway**, built from scratch: tier-based routing, provider failover,
retry with jitter, circuit breaking, exact caching, per-IP rate limiting, hard spend caps, and a
metadata-only cost ledger — behind one HTTP service any app can sit behind.

> **Why not LiteLLM / OpenRouter / Portkey / Helicone?** They exist and they're good. Conduit isn't
> competing with them — deploying a wrapper teaches you nothing about what's inside it. Every layer
> here is built from scratch specifically to be able to reason about these systems at the depth
> needed when the off-the-shelf tool breaks in production. (Naive-version failure modes: retry
> without jitter → thundering herd; no circuit breaker → every caller burns full timeout budgets
> against a dead provider; caching safety-classifier calls without a bypass → provider drift hides
> behind cache hits.)

**First tenant:** [Inner Council](https://github.com/lerning/inner_council)'s evaluation harness —
real application traffic (repeatable, cost-profiled) rather than synthetic load. The full
integration decision log (18 decisions, all resolved) is in
[`docs/DECISIONS.md`](docs/DECISIONS.md); the original architecture spec is
[`docs/spec.md`](docs/spec.md) — evidence the design preceded the code.

---

## Run it (zero secrets, zero spend)

```bash
pip install -r requirements.txt
python -m pytest -q                                  # 27 tests, all offline
uvicorn gateway.app:app --port 8200                  # start the gateway
```

Providers without an API key in the environment are transparently backed by a **deterministic
mock** (and the substitution is recorded in `routing_reason` — it can't masquerade as a real
provider in the ledger). Set `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` and those providers register
for real.

```bash
curl -s -X POST localhost:8200/v1/complete -H 'Content-Type: application/json' \
  -d '{"tier":"fast","user_id":"joshua","messages":[{"role":"user","content":"hello"}]}'

curl -s "localhost:8200/v1/usage?user_id=joshua"     # the spend meter
curl -s localhost:8200/health                        # providers, tiers, breaker states
```

## The API

| Endpoint | What |
|---|---|
| `POST /v1/complete` | `{tier, messages, max_tokens?, temperature?, stream?, user_id?, cache_bypass?}` → completion + cost + routing metadata. `stream: true` → SSE (`chunk`* → `final` with usage/cost/TTFT). |
| `GET /v1/usage` | Today's spend/requests vs. the global cap (+ per-user with `?user_id=`). Built for a client UI spend meter. |
| `GET /health` | Providers, tiers, circuit-breaker states, cache stats. |

**Clients request a named tier — never a model.** `fast` / `quality` / `judge` each map to an
ordered failover chain of `(provider, model)` (override via `CONDUIT_TIER_MAP` JSON). Which model
backs a tier, what happens when a provider deprecates a parameter, and where failover goes are
gateway concerns, invisible to the calling app.

## The pipeline (spec §3, as implemented)

```
request
  1. auth            per-app API key (CONDUIT_API_KEYS="ic:key1,evals:key2"; empty = dev mode)
  2. rate limit      token bucket per client IP
  4. spend caps      global daily hard stop + optional per-user cap — FAIL CLOSED:
                     if the ledger can't be read, requests are refused, never uncapped
  5. exact cache     sha256(tier+messages+params), TTL, per-request bypass flag
                     (ships DISABLED: observability first, then flip on with a baseline)
  6. router          tier → chain; skip open breakers; retry w/ backoff+full-jitter
                     within a provider; fail over across providers
  8. ledger          DynamoDB-shaped, metadata ONLY (tokens/cost/latency/cache-hit/model
                     — never message content), drives the caps and the usage meter
```

(Steps 3/7 — input/output guardrails — are the next phase; numbering kept from the spec.)

## Design decisions worth reading

- **Fail-closed enforcement** (`docs/DECISIONS.md` #16): the spend-cap path refuses requests when
  the ledger is unreachable. A gateway that fails open on exactly the day it's needed isn't a
  safety mechanism.
- **Cache bypass is a first-class request flag** (#6): callers running safety-classifier or
  provider-drift checks must be able to force a live call — a cached crisis verdict silently
  defeats a drift check.
- **Metadata-only telemetry** (privacy rule): if gateway logs became a second plaintext copy of an
  app's user content, they'd undermine whatever the app does to protect it.
- **Mock-always-present**: a fresh clone runs the full pipeline end-to-end with zero secrets, and
  the fault-injection seam (`MockProvider.queue_failure`) is how the chaos scenarios script
  provider outages deterministically.

## Status & roadmap (spec §8)

- ✅ **P0 skeleton** — service round-trips, ledger row lands, 27 tests, CI on push.
- ✅ **P1 reliability (core)** — retry + full jitter, per-provider circuit breaker with half-open
  probe, provider failover. *(Idempotency keys: next.)*
- ✅ **P2 streaming (core)** — SSE end-to-end with TTFT in the ledger. *(Buffer-vs-stream guardrail
  policy: next.)*
- ✅ **P3 (partial)** — tier routing + failover + rate limiting. *(Cost cascade / escalate-on-
  uncertainty: deliberately deferred — see DECISIONS.md #15.)*
- ✅ **P4 (partial)** — exact cache with TTL + bypass. *(Guardrails: next.)*
- ⬜ **P5** — benchmark + chaos harness with the before/after report and timeline artifacts.
- ⬜ Real-AWS mode (DynamoDB tables via IaC; `storage.py` interface already matches), deploy/teardown.

**v1.1:** durable **SQLite ledger** (the daily hard cap now survives restarts — see
`docs/DEPLOYMENT.md` for why SQLite is the right-sized production store here, with DynamoDB as the
documented multi-instance path) and a **structured-output passthrough** (`json_response: true` →
guaranteed JSON via forced tool-use / response_format; content-blind), which is what let the first
real tenant — Inner Council's `ConduitBackend` — move in.

**Known limits (deliberate, documented):** cache/limiter state is in-process (single-instance
shape); mid-stream failures end the stream rather than failing over; costs for mock-backed models
are simulated from the real price table so the meter demos honestly at $0 actual spend.
