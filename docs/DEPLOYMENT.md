# Deployment Plan — Conduit + Inner Council on Fly.io

**Status: prepped, not deployed.** Dockerfiles and `fly.toml`s exist and the wiring is verified
locally; no Fly account, no cloud resources, $0 spent. Deploying is a deliberate later step.

**Constraints driving every choice:** a few trusted, infrequent users; interview-impressive but
sensible; **infra budget ≤ $5–10/month**; LLM spend (not infra) is the real financial risk, and
it's already capped in-gateway (hard 402, fail-closed).

---

## Architecture: TWO Fly apps, not one machine

```
  friends ─── public HTTPS ───▶  inner-council  ──┐
                                   (Fly app)      │  Fly private network (6PN)
                                                  ├──▶  conduit-gateway  ──▶ Anthropic / OpenAI
  a future project ───────────────────────────────┘      (Fly app, NO public ports)
                                                          http://conduit-gateway.internal:8200
```

**Why two apps and not two processes on one machine.** An earlier draft of this plan co-located
them to save ~$2/mo. That was wrong for the stated goal: on Fly, one app is one *deployment unit*,
so a shared machine means deploying Inner Council restarts Conduit, ties Conduit's uptime to IC's
machine, and makes every future project's deploy bounce the others. A gateway that goes down
whenever one of its consumers ships isn't a shared service. Two apps cost a couple dollars more and
buy independent lifecycles.

**Adding a future project takes no Conduit changes:** deploy it in the same Fly org, point it at
`http://conduit-gateway.internal:8200`, and give it its own API key
(`CONDUIT_API_KEYS="ic:key1,newproject:key2"`). Its spend shows up in the ledger under its own
`client_id` prefix and counts against the same global cap.

**Conduit is private-only.** Its `fly.toml` deliberately has no `[http_service]` block — that's
what keeps it off the public internet. Per-app API keys are still enabled as defense in depth. IC
is the only internet-facing surface.

**Only Conduit holds provider keys.** In this deployment IC has no `ANTHROPIC_API_KEY` at all, so
it *cannot* bypass the spend cap even accidentally.

## Cost

| Item | Est. |
|---|---|
| `inner-council` — shared-cpu-1x, 512MB, always-on | ~$3.20/mo |
| `conduit-gateway` — shared-cpu-1x, 256MB, always-on | ~$1.95/mo |
| Volumes, 1GB each (SQLite + Chroma; SQLite ledger) | ~$0.30/mo |
| TLS + bandwidth at this scale | ~$0 |
| **Infra total** | **≈ $5.45/mo** ✅ |
| LLM spend | capped by `CONDUIT_GLOBAL_DAILY_CAP_USD` (set to `2.00` in `fly.toml` ⇒ ≤ ~$60/mo worst case — **lower it before inviting anyone**; $0.25–0.50/day is saner for a few friends) |

**Cost levers if it needs trimming:** set `auto_stop_machines = "suspend"` + `min_machines_running
= 0` on `inner-council` (costs a ~1s cold start on the first request; SSE streaming makes that a
slightly worse first impression, which is why it's off by default). Conduit stays always-on: Fly's
proxy wakes public apps, but a private 6PN app won't auto-start, so stopping it would break IC's
first call.

## Deploy sequence (when you're ready)

Both directories already contain a `Dockerfile` and `fly.toml`. Docker isn't needed locally — Fly
builds remotely.

```bash
# 0. one time — account creation needs a card, then:
fly auth login

# 1. Conduit FIRST (IC depends on its hostname existing)
cd conduit
fly launch --no-deploy --name conduit-gateway      # reads the existing fly.toml
fly volumes create conduit_data --size 1 --region sjc
fly secrets set ANTHROPIC_API_KEY=sk-ant-... \
                CONDUIT_API_KEYS="ic:$(openssl rand -hex 24)"   # save this key!
fly deploy

# 2. Inner Council
cd ../inner_council
fly launch --no-deploy --name inner-council
fly volumes create ic_data --size 1 --region sjc
fly secrets set IC_CONDUIT_API_KEY=<the key you just generated>
fly deploy

# verify
fly logs -a conduit-gateway
fly ssh console -a inner-council -C "curl -s http://conduit-gateway.internal:8200/health"
```

Teardown is `fly apps destroy inner-council conduit-gateway` (plus the volumes) — cost goes to $0.

## ⚠️ Dev-mode gotcha (verified, will waste your time otherwise)

**Do not use `IC_LLM_BACKEND=conduit` for offline development.** Conduit's deterministic mock
returns generic JSON (`{"mock": true, ...}`), while IC's *own* mock understands IC's task tags and
returns IC-shaped content (`{"message": "How have you been feeling..."}`). Routing IC through
Conduit's mock therefore yields structurally valid but semantically empty responses — intake
questions come back blank.

- **Offline dev / tests:** `IC_LLM_BACKEND=mock` (IC's semantic mock). ✅
- **Production (Fly):** `IC_LLM_BACKEND=conduit`, with real provider keys in Conduit. ✅
- **`IC → Conduit → mock`:** only useful for proving plumbing/metering, not behavior.

## Verified locally (no Docker required)

Both services were run with the exact container start commands and `fly.toml` env shapes:
`/health` on both ✅, IC's UI served ✅, IC data written to the volume path ✅, a real IC intake
flow driven over HTTP → routed through the private gateway → **metered in the ledger** ✅,
IC reporting `llm_backend: conduit` ✅.

**Not verified:** an actual `docker build` (Docker isn't installed on this machine). The images are
conventional `python:3.11-slim` builds; first `fly deploy` will be the real test, and the most
likely hiccup is a chromadb/numpy wheel needing a build dep — `build-essential` is already included
in IC's Dockerfile for that reason.

## Preconditions before inviting anyone

1. ✅ Durable spend ledger (survives restarts; test-pinned).
2. ✅ Metadata-only telemetry in both services.
3. ✅ Provider keys isolated to Conduit; IC can't bypass the cap.
4. ⬜ **Lower `CONDUIT_GLOBAL_DAILY_CAP_USD`** from the placeholder `2.00`.
5. ⬜ Per-user identity in IC (invite code → `user_id`), so per-user caps bind to a person.
6. ⬜ Encryption at rest for IC's stores — and tell friends plainly the operator can read content.

## Explicitly deferred

- The spec's AWS-native Conduit deploy (Lambda + API Gateway + DynamoDB, ~$0 idle) — kept as a
  portfolio artifact/demo path, not needed for this.
- Zero-knowledge encryption (incompatible with IC's safety gates, which must read content).
- Multi-instance Conduit (would move limiter/cache/ledger state to DynamoDB or Redis; the
  `storage.py` interface already anticipates it).
