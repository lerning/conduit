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

# $ per 1K tokens (input, output). Extend as models are added. Used for cost
# computation in the ledger and the benchmark's cost-delta report (spec §4).
COST_TABLE: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5-20251001": (0.001, 0.005),
    "claude-sonnet-4-6": (0.003, 0.015),
    "claude-opus-4-8": (0.015, 0.075),
    "gpt-4o": (0.0025, 0.01),
    "gpt-4o-mini": (0.00015, 0.0006),
    "mock": (0.0, 0.0),
}


def compute_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    in_rate, out_rate = COST_TABLE.get(model, (0.0, 0.0))
    return round(input_tokens / 1000 * in_rate + output_tokens / 1000 * out_rate, 8)


@dataclass
class LedgerEntry:
    request_id: str = field(default_factory=lambda: f"req_{uuid.uuid4().hex[:12]}")
    ts: float = field(default_factory=time.time)
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
        client_id prefix (e.g. an app name, or 'app:user')."""
        start = self._day_start()
        spend = 0.0
        requests = 0
        for item in self.scan_all():
            if float(item.get("ts", 0)) < start:
                continue
            cid = str(item.get("client_id", ""))
            if client_prefix and not cid.startswith(client_prefix):
                continue
            requests += 1
            spend += float(item.get("cost_usd", 0))
        return {"spend_usd": round(spend, 6), "requests": requests}
