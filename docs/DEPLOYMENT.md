# Deployment Plan — Conduit + Inner Council

**Status: plan, not yet executed.** Nothing is deployed; no cloud resources exist. This documents
the chosen shape and costs so the deploy itself is a mechanical step taken deliberately.

**Constraints that drove every choice:** minimal, infrequent users (a few trusted friends);
interview-impressive but sensible; **hard budget ≤ $5–10/month all-in**; the LLM spend cap (not
the infra) is the real financial risk, and it's already enforced in-gateway (hard 402 + fail-closed).

---

## Chosen shape: one small VM, two services, SQLite

```
                    ┌─────────────── Fly.io machine (shared-cpu-1x) ───────────────┐
  friends ── TLS ──▶│  Inner Council (uvicorn :8000)  ──▶  Conduit (uvicorn :8200) │──▶ Anthropic
                    │        │  SQLite + Chroma (volume)      │  SQLite ledger      │    / OpenAI
                    └────────┴────────────────────────────────┴─────────(volume)───┘
```

- **Fly.io** (or equivalent: Render/Railway): free TLS, `fly deploy` from a Dockerfile, volumes
  for persistence. Conduit listens only on the private/internal interface; Inner Council is the
  sole public surface. Conduit auth via per-app key regardless (defense in depth, decision #12).
- **SQLite everywhere** (decision D2): right-sized for a single instance with infrequent users.
  The ledger's DynamoDB path stays behind the same `storage.py` interface, unprovisioned.
- **Both services, one machine**: Conduit's fail-closed spend enforcement means IC can't
  accidentally bypass caps; co-locating removes a network failure mode and a second bill.

### Why not the spec's AWS-native shape (for THIS deployment)
The spec's Lambda + API Gateway + DynamoDB design scales to zero and idles at ~$0 — but it's for
Conduit *alone*. A public Inner Council needs an always-on process + persistent local files
(SQLite/Chroma), which Lambda doesn't fit. One VM serves both within budget. The AWS-native
Conduit deploy remains the documented alternative (`docs/spec.md` §3) and can be stood up
independently for the demo/benchmark story.

## Cost table (monthly)

| Item | Est. cost |
|---|---|
| Fly.io shared-cpu-1x, 256–512MB, always-on | ~$2–4 |
| Fly volume 1GB (SQLite + Chroma) | ~$0.15 |
| TLS, bandwidth at this scale | ~$0 |
| **Infra total** | **~$3–5/mo** ✅ under the $5–10 cap |
| LLM API spend (the real variable) | capped by `CONDUIT_GLOBAL_DAILY_CAP_USD` — e.g. $0.50/day ⇒ ≤ $15/mo worst case; set to taste |
| AWS Budgets alarm | n/a (no AWS in this shape); Fly spend alerts + the in-gateway cap are the controls |

## Preconditions before going live (from the integration decision log)

1. ✅ Durable spend ledger (D2 — done: SQLite, survives restarts; test-pinned).
2. ✅ Metadata-only telemetry in both services (decision #9 — done).
3. ⬜ IC per-user identity: invite code → `user_id` passed to Conduit (decision #4/#5).
4. ⬜ IC data encryption at rest (decision #11: app-level encrypt-on-write; friends told plainly
   that the operator can technically read content).
5. ⬜ Dockerfiles + `fly.toml` for both services; deploy + teardown each one command.
6. ⬜ Set real caps: `CONDUIT_GLOBAL_DAILY_CAP_USD`, `CONDUIT_USER_DAILY_CAP_USD`, per-app keys.

## Explicitly deferred

- AWS/CDK deployment of Conduit (kept as the spec-native demo path; ~$0 idle, deploy/teardown).
- Zero-knowledge encryption (incompatible with IC's safety gates reading content — documented).
- Multi-instance Conduit (would move limiter/cache/ledger to DynamoDB/Redis; interfaces ready).
