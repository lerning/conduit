# Inner Council × Conduit — Integration Decision Log

**Purpose of this file:** a complete handoff so a fresh Claude Code session can continue this
planning conversation without re-deriving context. Read this file plus the referenced code paths;
no other prior conversation history is needed.

**Status:** decisions phase, nothing built yet. This is the log of choices made together before any
design/implementation work starts.

---

## 1. What the two projects are

- **Inner Council** (this repo) — a local-first, multi-agent IFS ("Internal Family Systems")
  reflective tool. LangGraph pipeline, FastAPI + SSE backend, web UI. Already built, working,
  documented in `README.md` and `docs/ARCHITECTURE.md`. Currently single-user, no auth, local
  SQLite + Chroma storage, one LLM seam (`app/llm_client.py`) with two backends (real Anthropic +
  a deterministic mock).
- **Conduit** — a separate, from-scratch, content-blind LLM gateway (spec lives in a companion
  `conduit-spec.md`, not in this repo). Handles routing, failover, retry/circuit-breaking, exact
  caching, rate limiting, cost ledger, guardrails — all *mechanical*, none of it reads message
  content.
- **The goal being explored:** (a) make Inner Council safe to share with friends, and (b)
  optionally route Inner Council's real traffic through Conduit so Conduit's portfolio story is
  "meters and hardens real app traffic" instead of only synthetic load.

The original planning brief that kicked this off is `~/Downloads/inner-council-public-brief.md`
(outside this repo — Joshua has it). It proposed a content-blind/semantic layer boundary and two
workstreams: **A** = make IC safe to share (cost caps, privacy), **B** = optional Conduit
integration. This file supersedes/refines that brief with decisions actually made.

---

## 2. Verified facts about Inner Council's code (checked, not assumed)

Grounded findings from reading the actual repo — don't re-derive these, they're confirmed:

- **Single provider seam:** `anthropic.` appears in exactly one place, `app/llm_client.py:59`.
  Every model call goes through `get_client()` behind a `Backend` protocol that already has two
  implementations (`AnthropicBackend`, `MockBackend`). A third backend (e.g. `ConduitBackend`) or
  a `base_url` swap is cheap.
- **No mechanical plumbing exists in IC today** — grepped for retry/backoff/circuit/cache/
  rate_limit/budget/quota: essentially nothing. IC has zero retry, zero failover, zero circuit
  breaker, zero cache, zero rate limit, zero spend cap. Conduit would not duplicate IC's logic —
  it would supply what's entirely absent.
- **Two kinds of "routing" exist, don't conflate them:**
  - Semantic (`runtime/router.py` — which parts activate, who speaks next; safety classifiers in
    `safety/monitor.py`) — needs message content, stays in IC.
  - Model tiering — every call site names `config.MODEL_FAST` or `config.MODEL_QUALITY` literally
    (`safety/monitor.py` x4, `runtime/part_agent.py`, `runtime/orchestrator.py`,
    `runtime/post_session.py` x2, `intake/structured.py`). This is content-blind policy, a
    candidate to move into Conduit as tier resolution (see Decision 2 below).
- **No identity/auth at all.** `profile_id` is a real seam (threaded through every table, see
  `domain/models.py:3`) but it's just a request parameter — `get_profile(profile_id)` etc. take it
  from the caller with zero verification. CORS is `allow_origins=["*"]` (`app/main.py`).
- **Token metering already exists, cost computation does not.** `app/llm_client.py:~598` logs
  `log_event("llm_call", task=, model=, input_tokens=, output_tokens=)` on every call — real
  per-call ledger raw material already flowing to `logs/trace.jsonl`. No price table, no
  aggregation, no per-user attribution, no caps.
- **🚩 Known bug, independent of Conduit, not yet fixed:** `runtime/part_agent.py` logs
  `text=turn["text"]` into the trace log — i.e., IC's own telemetry already stores a second
  plaintext copy of every session's actual content. `app/observability.py:27` only redacts
  `{api_key, authorization, token}` — secrets, not message content. **Fix this regardless of
  anything else in this doc** — it's a one-line change (drop `text=` from that log call).
- **Storage is local files, not cloud:** `sqlite3.connect(path)` (`domain/store_sql.py`) +
  `chromadb.PersistentClient(path)` (`domain/store_vector.py`). No encryption at rest, no TLS
  (uvicorn serves plain HTTP), no IAM — there is no cloud deployment to configure yet. The
  original brief assumed DynamoDB/RDS with a KMS checkbox; that infra doesn't exist. Going public
  means *choosing and standing up* a real deployment, not flipping a flag.

---

## 3. The 18 decisions — status and resolution

Legend: ✅ RESOLVED (with the answer) · 🟡 OPEN (still needs a call)

### A. How Conduit and IC talk

1. **✅ RESOLVED — Service, not import.** Conduit runs as its own process, IC calls it over HTTP
   (even on localhost). Rationale: importing as a library gives each app its own independent copy
   of rate-limiter/cache/ledger state — not actually more modular, since true cross-app
   enforcement needs a shared external store regardless. A service is the only shape that makes
   Conduit "a general tool for other projects," which is the explicit goal.
2. **✅ RESOLVED — Named tiers, not literal models.** IC calls Conduit with `tier="fast"` /
   `"quality"` / `"judge"`. IC owns *when* to use which tier (semantic — needs to know the task).
   Conduit owns *which model currently satisfies that tier*, including fallback/version bumps.
   This turns the Opus-4.8 `temperature`-param deprecation (already hit once, see git history
   around llm_client.py) into a Conduit-side fix instead of an IC-side patch across 5 files.
3. **✅ RESOLVED — Service (see #1).**

### B. What moves into Conduit

4. **✅ RESOLVED — Split the noun from the enforcement.** IC owns *who this human is* (invite
   code → user; IC already has the `profile_id` seam to extend). Conduit enforces
   rate-limits/caps against whatever ID IC hands it on each call. Conduit is not becoming an
   identity provider.
5. **✅ RESOLVED — IC sets the limit value, Conduit implements/monitors/enforces it.** IC needs to
   pass the user identifier on every call so Conduit knows which bucket to decrement. Consistent
   with #4's split.
6. **✅ RESOLVED — Safety-classifier caching needs a bypass, not just a policy.** Caching
   `crisis_screen`/`flooding_screen`/etc. is legitimate (content-blind, and these are exactly the
   calls the eval harness repeats N=5 with identical input) — but a cached verdict silently
   defeats the eval harness's provider-drift run, whose entire point is "did the live model's
   judgment change." Required: a short TTL and/or an explicit cache-bypass flag the drift run
   always sets.

   **Implemented (cache-on, v1.5).** The bypass flag turned out to need to be *per task*, not one
   global switch. IC derives it from the `[[TASK:...]]` tag already in the system prompt
   (`_NEVER_CACHE_TASKS` in `app/llm_client.py`), and forces a live call for two disjoint reasons:
   *correctness* for the safety classifiers and eval judges — a cached verdict is a verdict nobody
   ran, and within the TTL two different people typing the same sentence would share one screening;
   *non-determinism* for the generative turns (`part_agent`, `orchestrator`, `post_session`), which
   run at temp 0.85 specifically so the room differs every time — an exact-cache hit there replays a
   previous session verbatim, which is a visible regression rather than a saving. What remains
   cacheable is low-temperature structured extraction (intake, routing, tagging, enrichment), where
   an identical request genuinely should give an identical answer. `IC_CONDUIT_CACHE_BYPASS=1`
   survives as the whole-pipeline override for drift runs.

### C. Rollout order

7. **✅ RESOLVED — Eval harness is the first tenant**, not friend traffic. It's real app traffic
   today, needs zero users, is repeatable, and already has a measured cost profile (a live run:
   60 calls, 69,705 input / 13,877 output tokens, ~3 min — see `eval/harness/README.md`).
8. **✅ RESOLVED — Read-only (observability/ledger) first**, then flip on caching/retry once
   there's a baseline number to measure "before" against. **Remember this decision when
   sequencing implementation** — Joshua flagged this explicitly as easy to forget.

   **Done (v1.5).** The read-only period ran through v1.1–v1.4; `CONDUIT_CACHE_ENABLED` is now
   `"1"` in `fly.toml`, with the per-task bypass policy from #6 deciding what is eligible.

### D. Privacy & security

9. **✅ RESOLVED — Fix the content-logging leak regardless of Conduit.** See §2 above; independent
   one-line bug fix, do it first, unrelated to any Conduit decision.
10. **✅ RESOLVED — Going public requires standing up a real deployed DB + compute + TLS.** Not a
    flag flip. Standard shape: a managed Postgres (RDS or a provider like Supabase/Neon/Railway)
    or similar with encryption-at-rest as a literal checkbox, plus small always-on compute
    (Fly.io/Render/a small VM) that gives TLS by default. Specific vendor not chosen yet — 🟡 open
    if/when this becomes near-term (not urgent now).
11. **✅ RESOLVED — Encryption at rest yes; true zero-knowledge explicitly skipped, with reasoning
    recorded.** Two different things: (a) encryption-at-rest (small lift — one symmetric key per
    user, encrypt on write, decrypt in-memory only when the graph runs) protects against DB
    theft, and is worth building. (b) true zero-knowledge (server never sees plaintext, ever) is
    incompatible with IC's actual safety architecture — the crisis gate, per-turn screen, and
    router all need to read message content to function, so "zero-knowledge" would mean the
    whole graph runs somewhere with no visibility, which is architecturally a different app, not
    a feature. **Decision: build (a), skip (b), and tell friends plainly that Joshua can
    technically see their content if he goes looking.**
12. **✅ RESOLVED — Expose Conduit (deploy it, don't keep it localhost-only), but gate it with
    per-calling-app API keys, not open/unauthenticated.** Rationale: localhost-only Conduit can
    only front apps on the same machine; the multi-project future needs it reachable, but
    "exposed" doesn't have to mean "public and unauthenticated."

### E. Cost containment

13. **✅ RESOLVED — Hard stop (not graceful degrade) for the global daily spend cutoff, GLOBAL not
    per-user, with a visible usage meter in IC's UI header** (e.g. "$X / $Y today", top of page).
    **Remember this decision** — flagged explicitly as easy to lose track of. Requires: a
    read endpoint (Conduit or IC) exposing current spend-today that the IC frontend can poll or
    receive over SSE. UI work not yet designed, just the requirement is locked.
14. **✅ RESOLVED, then REVISED in deployment — rate limiting is per `app:user`, not per-IP.**
    Per-IP was the original call and it was wrong once Conduit moved onto Fly's private network:
    every request arrives from the same source address, so all tenants shared a single token
    bucket and one noisy user could throttle everyone. That is a bulkhead in name only. The
    limiter is now keyed on the client id the caller already sends (`ic:u_a080f989a3`), which is
    the same key the per-user spend cap uses. Proven by
    `tests/test_dashboard.py::test_one_tenant_flooding_does_not_throttle_another` and visible on
    the dashboard's tenant panel.
15. **✅ RESOLVED, with a correction — cascade routing ("cheap-first, escalate on uncertainty") is
    NOT purely mechanical as originally proposed.** "How confident was the model" is a semantic
    judgment, so by the project's own content-blind test it can't live wholesale in Conduit. Split
    it: IC decides *when* to escalate (semantic), Conduit just executes whatever tier IC asks for
    next (mechanical, consistent with Decision 2's tier model).

### F. Failure modes & coupling

16. **✅ RESOLVED — Fail CLOSED specifically for the rate-limit/spend-cap enforcement path; other
    Conduit features (cache/retry/observability) may still fail open.** Reasoning confirmed by
    Joshua: if Conduit is the only place enforcing limits and IC falls back to direct Anthropic
    calls on a Conduit outage, that outage silently returns IC to today's "no limits at all"
    state — a bug or abuse could spam unbounded during the outage window. This is a **deliberate,
    scoped reversal** of the original brief's "Conduit must never be load-bearing" principle —
    intentional, not an oversight, and scoped only to the safety-critical path.
17. **✅ RESOLVED, clarified — not actually a hard problem given IC's existing architecture.** The
    concern was retrying a live SSE stream. But IC already generates one part's turn as one
    discrete LLM call (each LangGraph node = one call through the single `llm_client` seam), so
    retry just means re-running that one call, not resuming a half-streamed response or replaying
    a whole session. Falls out naturally once Decisions 1–3 are implemented; no extra design
    needed now.
18. **✅ RESOLVED — Independent repos, version-pinned client, no vendoring/monorepo.** Matches the
    original brief's "keep the two projects independently presentable" instruction directly.

---

## 4. Everything is resolved — what's actually still open

All 18 decisions have a recorded answer. Remaining work is **not more decisions, it's
implementation planning**:

- Pick the specific deployment platform for Decision 10 (DB provider, compute host) — deferred as
  not urgent.
- Design the Conduit HTTP API surface itself (tier resolution endpoint, ledger/usage-meter read
  endpoint, rate-limit config format) — not started.
- Design the IC-side `ConduitBackend` (or `base_url` swap) implementing the `Backend` protocol in
  `app/llm_client.py`.
- Design the invite-code → user → `profile_id` extension in IC (Decision 4).
- Fix the content-logging bug (Decision 9) — trivial, can be done immediately, unblocked by
  nothing.
- Design the UI spend meter (Decision 13) — depends on a usage-read endpoint existing first.

**Suggested next session opening move:** fix Decision 9 (the logging bug) immediately since it's
free and blocks nothing, then start scoping the Conduit HTTP API surface (tier resolution +
ledger) as the first real implementation piece, using the eval harness (Decision 7) as the first
tenant to validate against.

---

## 5. Preferences/working-style notes for continuity

- Joshua wants decisions made explicitly and numbered before building — don't jump to
  implementation without a recorded decision.
- He tracks token/context cost consciously; keep responses efficient, prefer writing durable repo
  docs over long chat explanations when something needs to persist across sessions.
- Versioning convention already in use on this repo: tagged commits `v1`, `v2`, `v3`, `v3.1` …
  each a coherent, tested, pushed increment — continue that pattern for Conduit-related work.
- Personal data (the actual profile/session content) never gets committed — `data/`, `logs/`,
  `samples/`, `.env` are gitignored; keep that discipline for any Conduit-side storage too.

---

## Addendum (added when Conduit v1 was built)

**Decision 15, refined by the actual spec:** the original resolution said escalate-on-uncertainty
is semantic and therefore can't live in Conduit. The Conduit spec (v0.3, §2 C4) constrains
escalation signals to **near-free, content-blind heuristics only** — the model's own expressed
uncertainty/refusal plus cheap shape heuristics, explicitly no classifier in v1. Under that
constraint, cascade escalation *can* legitimately live in Conduit (it reads response shape, not
user meaning). Both remain true: anything requiring semantic judgment of the *user's* content stays
in the calling app; the spec-constrained cascade is deferred to P3 either way.

**v1 implementation notes (what shipped vs. the decisions):**
- Decisions 1–8, 12–14, 16–18 are implemented and pinned by tests (`tests/test_app.py` maps each
  to observable HTTP behavior). 9–11 are Inner-Council-side or deployment-phase items.
- Per-user cap value (decision 5) is set via `CONDUIT_USER_DAILY_CAP_USD` at deploy/config time —
  the calling app owns the number, the gateway enforces it. A per-request or admin-API mechanism
  can come later without interface changes.
- The usage meter (decision 13) is `GET /v1/usage` — the Inner Council UI polls this for the
  header spend display.
