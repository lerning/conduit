"""Usage/cost ledger (spec C8, §3 data stores).

DynamoDB-backed in production; every test in this repo runs it against a
moto-mocked table, so P0's "ledger row lands" acceptance criterion is provable
with zero AWS credentials and zero cost. The table shape here is what
infra/ (CDK, built later) will provision for real.

Privacy: this is METADATA ONLY (tokens, cost, latency, cache-hit, model,
provider) -- never message content. See docs/spec.md and the Inner Council
integration decision log for why this boundary is load-bearing.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Optional

TABLE_NAME = "conduit-ledger"

# $ per 1K tokens (input, output) at published list rates. Used for the ledger,
# the spend caps, and the benchmark's cost-delta report (spec §4).
#
# These numbers ARE the spend cap. A wrong rate here doesn't show up as a bug --
# it shows up as a cap that trips at the wrong time and a dashboard that lies
# with confidence. Verified against Anthropic's published pricing 2026-08-15.
COST_TABLE: dict[str, tuple[float, float]] = {
    # Anthropic -- $5/$25 per MTok for the Opus tier, $3/$15 Sonnet, $1/$5 Haiku
    "claude-opus-5": (0.005, 0.025),
    "claude-opus-4-8": (0.005, 0.025),
    "claude-fable-5": (0.010, 0.050),
    "claude-sonnet-5": (0.003, 0.015),      # list rate; intro pricing is lower
    "claude-sonnet-4-6": (0.003, 0.015),
    "claude-haiku-4-5": (0.001, 0.005),
    "claude-haiku-4-5-20251001": (0.001, 0.005),
    # OpenAI
    "gpt-4o": (0.0025, 0.01),
    "gpt-4o-mini": (0.00015, 0.0006),
    "mock": (0.0, 0.0),
}

# An unpriced model used to cost $0, which meant it consumed no budget and the
# cap could never stop it -- a fail-OPEN hole in a system whose whole design
# premise is failing closed. Unknown models now price at the most expensive
# known rate: a new model over-counts against the cap until it's added here,
# which is the safe direction to be wrong in.
_FALLBACK_RATE = max(COST_TABLE.values())


def compute_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    if model in COST_TABLE:
        in_rate, out_rate = COST_TABLE[model]
    else:
        in_rate, out_rate = _FALLBACK_RATE
        _log_unpriced(model)
    return round(input_tokens / 1000 * in_rate + output_tokens / 1000 * out_rate, 8)


_warned_unpriced: set[str] = set()


def _log_unpriced(model: str) -> None:
    """Once per model per process -- loud enough to notice, quiet enough to run."""
    if model not in _warned_unpriced:
        _warned_unpriced.add(model)
        print(f"[conduit] WARNING: no price for model {model!r}; charging the "
              f"highest known rate {_FALLBACK_RATE}. Add it to COST_TABLE.",
              flush=True)


# Every way a request can end. Until these were recorded the ledger was a
# SUCCESS-ONLY log: throttles and cap-hits fired and left no trace, so "did we
# throttle anyone today?" was unanswerable and the protections were invisible.
OUTCOME_OK = "ok"
OUTCOME_RATE_LIMITED = "rate_limited"      # 429 -- tenant's own bucket empty
OUTCOME_CAP_GLOBAL = "cap_global"          # 402 -- global daily ceiling
OUTCOME_CAP_USER = "cap_user"              # 402 -- this tenant's ceiling
OUTCOME_FAIL_CLOSED = "fail_closed"        # 503 -- ledger unreadable, refused
OUTCOME_PROVIDER_FAILED = "provider_failed"  # 502 -- chain exhausted
OUTCOME_BAD_REQUEST = "bad_request"        # 400 -- unknown tier etc.

REJECTIONS = (OUTCOME_RATE_LIMITED, OUTCOME_CAP_GLOBAL, OUTCOME_CAP_USER,
              OUTCOME_FAIL_CLOSED, OUTCOME_PROVIDER_FAILED, OUTCOME_BAD_REQUEST)


@dataclass
class LedgerEntry:
    request_id: str = field(default_factory=lambda: f"req_{uuid.uuid4().hex[:12]}")
    ts: float = field(default_factory=time.time)
    outcome: str = OUTCOME_OK
    status_code: int = 200
    client_id: str = "anonymous"
    model: str = ""
    provider: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    cache_hit: bool = False
    ttft_ms: Optional[float] = None
    latency_ms: Optional[float] = None
    routing_reason: str = ""       # e.g. "cascade:cheap_first" | "failover:circuit_open"
    guardrail_flags: list[str] = field(default_factory=list)

    def to_item(self) -> dict:
        """DynamoDB item -- floats become Decimal, empty list stays a list."""
        d = asdict(self)
        d["cost_usd"] = Decimal(str(d["cost_usd"]))
        if d["ttft_ms"] is not None:
            d["ttft_ms"] = Decimal(str(d["ttft_ms"]))
        if d["latency_ms"] is not None:
            d["latency_ms"] = Decimal(str(d["latency_ms"]))
        return d


class LedgerStore:
    """Thin wrapper over a DynamoDB table resource. Pass a moto-mocked resource
    in tests; a real boto3 resource in prod -- identical code path either way."""

    def __init__(self, table) -> None:
        self._table = table

    @classmethod
    def create_table_if_missing(cls, dynamodb_resource, table_name: str = TABLE_NAME):
        existing = [t.name for t in dynamodb_resource.tables.all()]
        if table_name in existing:
            return dynamodb_resource.Table(table_name)
        table = dynamodb_resource.create_table(
            TableName=table_name,
            KeySchema=[{"AttributeName": "request_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "request_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table.wait_until_exists()
        return table

    def put(self, entry: LedgerEntry) -> None:
        self._table.put_item(Item=entry.to_item())

    def get(self, request_id: str) -> Optional[dict]:
        resp = self._table.get_item(Key={"request_id": request_id})
        return resp.get("Item")

    def scan_all(self) -> list[dict]:
        """Test/benchmark convenience -- not for production hot-path use."""
        return self._table.scan().get("Items", [])

    # --- daily aggregates (drive the hard spend cap + the IC usage meter) ----
    # v1: scan-and-sum. Fine at personal volume; a date-keyed aggregate item
    # (atomic ADD) is the documented upgrade before real multi-user traffic.

    @staticmethod
    def _day_start(now: Optional[float] = None) -> float:
        import datetime as _dt
        t = _dt.datetime.fromtimestamp(now or time.time(), tz=_dt.timezone.utc)
        return t.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def day_totals(self, client_prefix: Optional[str] = None) -> dict:
        """Spend/request totals since UTC midnight; optionally filtered by a
        client_id prefix (e.g. an app name, or 'app:user').

        `requests` counts SERVED requests only. Rejections carry cost 0 so they
        never move spend, but counting them here would make the usage meter
        claim work that was refused."""
        start = self._day_start()
        spend = 0.0
        requests = 0
        rejected = 0
        for item in self.scan_all():
            if float(item.get("ts", 0)) < start:
                continue
            cid = str(item.get("client_id", ""))
            if client_prefix and not cid.startswith(client_prefix):
                continue
            if str(item.get("outcome", OUTCOME_OK)) in REJECTIONS:
                rejected += 1
                continue
            requests += 1
            spend += float(item.get("cost_usd", 0))
        return {"spend_usd": round(spend, 6), "requests": requests,
                "rejected": rejected}

    def rows_since(self, since_ts: float) -> list[dict]:
        """All rows (served and rejected) newer than `since_ts`, oldest first."""
        rows = [r for r in self.scan_all() if float(r.get("ts", 0)) >= since_ts]
        return sorted(rows, key=lambda r: float(r.get("ts", 0)))
