# Conduit — Production LLM Gateway
### Build Spec v0.3 (final pre-handoff draft for Claude Code)

> **One-line pitch:** A provider-agnostic LLM gateway built from scratch to demonstrate production judgment — streaming, failover, reliability, routing, guardrails, and cost/latency telemetry — with a replayable benchmark + fault-injection harness that proves the numbers without real users.

> **Portfolio role:** The *"I can make AI systems production-grade, cheap, and safe"* pillar. Deliberately does **not** overlap Inner Council (agents + evals). Together: the full Applied-AI / FDE profile.

---

## 0. The "why not LiteLLM?" answer (goes verbatim-ish in the README)

Prior art exists and is named up front: LiteLLM, OpenRouter, Portkey, Helicone. Conduit is not an attempt to compete with them. It exists because **deploying a wrapper teaches you nothing about what's inside it.** Every layer here — circuit breaking, failover, streaming guardrails, routing — is built from scratch specifically to be able to reason about these systems at the depth needed when the off-the-shelf tool breaks in production. The README includes a short failure-mode analysis of the naive versions of each layer (no jitter → thundering herd; no circuit breaker → thread-pool exhaustion; stream-through guardrails → un-retractable bad output) as proof the depth is real.

---

## 1. Design goals & non-goals

**Goals (priority order)**
1. Distributed-systems judgment applied to LLM infra — the under-supplied moat.
2. **Defensible numbers** with zero users: cost delta, p50/p95 latency, time-to-first-token, cache hit rate, routing distribution, failover behavior — all from a replayable benchmark.
3. Guardrails / AI-security mapped to OWASP LLM Top 10, with an explicit hot-path latency budget.
4. Legible AWS-native architecture — grokkable from the README diagram in 60 seconds.
5. **Visible restraint.** Two data stores, no gratuitous services. The cut list is documented.

**Non-goals (state these in the README — scope control is a senior signal)**
- Not training/fine-tuning. Not multi-tenant SaaS (API-key auth stub only). Not a LiteLLM competitor (see §0). Not framework-heavy — thin, explicit, readable code. No real-traffic claims — load is synthetic and honest about it.

---

## 2. Core capabilities (MVP — everything here ships or the project isn't done)

| # | Capability | Signal it carries |
|---|------------|-------------------|
| C1 | Provider abstraction: **Anthropic + OpenAI + deterministic mock** (shared interface, streaming-capable) | Enables failover, zero-spend testing, reproducibility |
| C2 | Reliability layer: idempotency keys, retry with **backoff-and-jitter**, **per-provider circuit breaker**, tight timeouts | The moat |
| C3 | **Streaming (SSE)** end-to-end, with **per-category guardrail policy**: stream-through (low stakes) vs buffer-then-emit (high stakes) | The production tell; TTFT becomes a first-class metric |
| C4 | **Two-axis routing**: (a) cost cascade — cheap model first, escalate on **near-free signals** (model's own expressed uncertainty/refusal + cheap heuristics — no classifier in v1); (b) **provider failover** — circuit open / rate-limited → alternate provider | Cost lever + reliability lever; escalation-signal design is a talking point |
| C5 | **Exact cache** (hash → DynamoDB w/ TTL). Semantic cache is *designed, documented, deferred* (see §6) | Cost + latency without the embedding-infra weight |
| C6 | **Rate limiting / backpressure**: token-bucket per client inbound; outbound respect for provider limits, integrated with retry + breaker | Conspicuous-by-absence systems flex |
| C7 | Guardrails on a **strict latency budget** (heuristics/small classifiers only — never an LLM call on the hot path): input injection scan; output Pydantic schema validation + self-correction loop; **tool-call allowlist + argument schema validation** | OWASP-mapped; "all LLM output is untrusted input to the next stage" |
| C8 | Telemetry: per-request trace with spans per stage; DynamoDB usage/cost ledger (tokens in/out, model, cache-hit, $, TTFT) | Feeds the benchmark; observability leg |
| C9 | **Benchmark + fault-injection harness** (see §4) | Replaces users with numbers |

---

## 3. Architecture (AWS-native, deliberately small)

```
client ⇄ (SSE)
  → API Gateway
  → Gateway service (Lambda MVP; Fargate noted as the steady-traffic swap)
      1. auth stub (API key)
      2. inbound rate limit          (token bucket, DynamoDB conditional writes)
      3. input guardrails            (injection scan — cheap, budgeted)
      4. exact-cache lookup          ── hit ─▶ return
      5. router                      (cost cascade + failover axis)
      6. provider call               (idempotency key, retry+jitter,
                                      circuit breaker, timeout, streaming)
      7. output guardrails           (schema validation;
                                      stream-through vs buffer per category)
      8. cache write + ledger write + trace emit
```

**Data stores — ONE, plus object storage (v0.3 change):**
- **DynamoDB** — idempotency keys, usage/cost ledger, routing + breaker state, **exact cache (TTL attribute)**, **rate-limit token buckets (conditional writes)**.
- *Why not Redis/ElastiCache:* ElastiCache forces Lambda into a VPC → NAT Gateway (~$32/mo idle) just to reach provider APIs, plus ElastiCache's own floor cost and VPC cold-start pain — infrastructure mis-sized for the workload. Token-bucket-in-DynamoDB is admittedly clunkier than Redis; that tradeoff is documented. This consolidation is itself a README story: right-sizing over resume-driven architecture.
- **S3** — benchmark request/response records (replay + report input).
- **CloudWatch** — dashboard + alarms (breaker trips, error-budget burn, TTFT p95).

**Documented cut list (in README):** Redis/ElastiCache (VPC + NAT Gateway cost trap — see above), Athena (ledger + benchmark report already tell the cost story), SQS/Fargate async worker path (no earned async work in MVP; returns with Batch API in stretch), third provider, PII detection (designed-for mention only), semantic cache (see §6). *Every cut has a one-line reason — restraint is part of the demo.*

---

## 4. Benchmark + fault-injection harness (first-class deliverable)

**Benchmark**
- Golden query set: ~100–200 prompts, difficulty-tiered, with duplicates to exercise the cache.
- **Baseline mode** (everything → expensive model, no cache, no routing) vs **Conduit mode**.
- Runs against the deterministic mock (zero spend, reproducible from a clone); one small captured real-provider run for credibility.
- Report: cost delta %, p50/p95/p99, **TTFT**, cache-hit rate, routing distribution, guardrail catches → markdown table + charts → README.

**Fault injection (the party trick)**
- Scripted chaos scenarios against the mock: provider goes dark mid-load → circuit opens → failover engages → half-open probe → recovery. Rate-limit storm → backpressure + jitter prevent thundering herd.
- Each scenario emits a timeline artifact (trace excerpt or chart) for the README.
- *Acceptance: one command runs the full chaos suite and produces the timeline.*

**Interview line:** "Against an all-to-expensive-model baseline, Conduit cut cost ~X% at Y ms p95 / Z ms TTFT — and here's the recording of it surviving a provider outage mid-benchmark."

---

## 5. Guardrails → OWASP LLM Top 10 map (README table)

- Injection scan → LLM01 Prompt Injection.
- Tool-call allowlist + arg schema (never execute LLM output as a command) → insecure tool use / confused-deputy.
- Output schema validation + buffer-before-emit for high-stakes categories → overreliance / harmful-output containment.
- Designed-for (documented, not built): PII detection, groundedness checking when retrieval context present.

Hot-path rule, stated explicitly: **guardrails get a latency budget; nothing on the request path may call an LLM.**

---

## 5.5 Repo professionalism & presentation layer (v0.3 additions — cheap, high-signal)

- **CI from Phase 0**: GitHub Actions — tests + lint on every push. The green check is the first thing an EM clicks.
- **Deploy posture**: nobody will hit a live endpoint. Ship **one-command deploy + one-command teardown** via IaC, with a documented monthly-cost table. Reproducible-from-clone *is* the demo; don't pay for idle infra.
- **Watch-it-work artifact**: 30–60s GIF/asciinema at the top of the README — benchmark running, provider killed, circuit opens, failover engages, recovery. Highest leverage-per-minute item in the project.
- **Benchmark honesty**: mock-provider latency numbers are labeled as *relative* (baseline vs Conduit, identical conditions); the single captured real-provider run carries the absolute numbers.
- **AI-assisted authorship stance (decided up front)**: implementation is Claude Code-assisted — stated plainly. The design is the author's: this spec lives in the repo as evidence the architecture preceded the code, and every mechanism is whiteboard-reproducible. Own it; don't hedge.

---

## 6. Stretch (build only if ahead of schedule — each is fully *designed* in the README regardless)

- **S1 — Semantic cache**: embedding + vector similarity behind the exact cache; includes the when-NOT-to-cache policy (per-category scoping, false-positive risk) and provenance-tracked invalidation (`source_doc_ids`). *The design writeup is interview-complete even unbuilt.*
- **S2 — Batch API path**: SQS + worker pool + DLQ/redrive, earned by real async work (batch embeds feed S1).
- **S3 — MCP server surface**: agent clients route through Conduit. Cheap once the gateway exists; currency signal.
- **S4 — Prompt-cache-aware assembly**: stable-prefix-first ordering + hit-rate metric.

---

## 7. Repo structure

```
conduit/
  infra/                 # IaC (§8 decision)
  gateway/
    providers/           # anthropic.py, openai.py, mock.py — streaming-capable shared interface
    reliability/         # retry.py, circuit_breaker.py, idempotency.py, timeouts.py
    ratelimit/           # token_bucket.py (inbound + outbound)
    cache/               # exact.py  (semantic/ arrives with S1)
    routing/             # cascade.py, failover.py, signals.py
    guardrails/          # input.py, output.py, tooluse.py, budget.py
    streaming/           # sse.py, buffer_policy.py
    telemetry/           # tracing.py, ledger.py
    handler.py           # the numbered pipeline
  benchmark/
    query_set.jsonl
    replay.py
    chaos.py             # fault-injection scenarios
    report.py
  tests/                 # unit + integration against mock provider
  .github/workflows/     # CI: test + lint on push
  dashboards/
  docs/spec.md           # THIS document — evidence design preceded code
  docs/demo.gif          # the watch-it-work recording
  README.md              # demo gif, diagram, §0 prior-art answer, OWASP map, cut list, cost table, benchmark results
```

---

## 8. Build sequence (each phase demoable + defensible alone; ship > ambition)

- **P0 — skeleton**: API GW + Lambda echo, DynamoDB, mock provider, test scaffold. *Accept: request round-trips, ledger row lands, tests run, **CI green on push**.*
- **P1 — reliability**: retry+jitter, circuit breaker, idempotency, timeouts. *Accept: chaos script kills mock mid-run → fail-fast → half-open recovery, captured.*
- **P2 — streaming**: SSE end-to-end, TTFT metric, buffer-vs-stream policy. *Accept: high-stakes category demonstrably buffers; TTFT in ledger.*
- **P3 — routing**: cost cascade (near-free signals) + provider failover + rate limiting. *Accept: easy prompts stay cheap; breaker-open reroutes cross-provider; ledger reconciles.*
- **P4 — cache + guardrails**: exact cache; injection scan; tool-call validation; schema validation. *Accept: duplicate served from cache; injection probe + malformed tool call caught and logged within latency budget.*
- **P5 — benchmark + README**: harness, chaos suite, report, diagram, OWASP map, §0, cut list. *Accept: one command → before/after table + chaos timelines; demo GIF recorded; deploy + teardown each verified as one command.*
- **P6 — stretch**: S1→S4 in order, only if P0–P5 are genuinely done.

**Deadline reality:** if interviews arrive early, **P0–P3 + README** is the minimum shippable, defensible cut. An unfinished ambitious project reads worse than a finished smaller one.

---

## 9. Open decisions (resolve before handoff)

1. **IaC**: CDK (faster, AWS-native) vs Terraform (resume-portable). Lean: CDK, with a README note on the tradeoff.
2. **Language**: all-Python (Pydantic-native, coherent). Lean: yes.
3. **Lambda streaming**: Lambda response streaming vs Fargate for the SSE path — verify Lambda's SSE ergonomics early in P2; Fargate is the documented fallback.
4. **Escalation signals**: locked — near-free only in v1 (model uncertainty/refusal + heuristics). Classifier is explicitly out of scope.
5. **Real-provider spend cap**: one captured sanity run, budget ~$5–10, results screenshotted into README; mock is primary everywhere else.

6. ~~Cache store~~ **Resolved v0.3**: DynamoDB-only (TTL for cache, conditional writes for token buckets). Redis cut — VPC/NAT trap.

*v0.3 — from v0.2: −Redis/ElastiCache (→DynamoDB-only, one store); +CI (Phase 0), +deploy/teardown posture + cost table, +demo GIF, +benchmark-honesty labeling, +AI-authorship stance, +spec-in-repo, +minimum-shippable-cut line (P0–P3).*
*v0.2 — reduced from v0.1: −Athena, −async worker path, −semantic cache (→S1), −PII (→designed-for), −groundedness tiers, −third provider; +streaming, +failover, +rate limiting, +fault injection, +tests, +§0 prior-art answer, +documented cut list.*
