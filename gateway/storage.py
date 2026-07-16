"""Storage backends (spec §3: DynamoDB, deliberately the ONE data store).

Local/dev/test mode uses FakeTable -- an in-memory object implementing the tiny
slice of the boto3 DynamoDB Table API the gateway uses (put_item / get_item /
scan). Production mode (CONDUIT_USE_REAL_AWS=1) returns a real boto3 Table with
the identical interface, so the code path never branches on environment.

boto3 is imported lazily -- the local service and the whole test suite run with
zero AWS dependencies installed.

Known v1 limitation (documented, deliberate): FakeTable is process-memory, so
the daily-spend ledger resets on restart. Fine for local/dev; the real-AWS mode
is required before any public deployment where the hard spend cap matters.
"""
from __future__ import annotations

import threading
from typing import Any, Optional


class FakeTable:
    """In-memory stand-in for a boto3 DynamoDB Table (the used subset only)."""

    def __init__(self, key_attr: str = "request_id") -> None:
        self._key = key_attr
        self._items: dict[str, dict] = {}
        self._lock = threading.Lock()

    def put_item(self, Item: dict) -> None:
        with self._lock:
            self._items[Item[self._key]] = Item

    def get_item(self, Key: dict) -> dict:
        with self._lock:
            item = self._items.get(Key[self._key])
        return {"Item": item} if item is not None else {}

    def scan(self, **_: Any) -> dict:
        with self._lock:
            return {"Items": list(self._items.values())}


def get_ledger_table(use_real_aws: bool = False, table_name: str = "conduit-ledger",
                     key_attr: str = "request_id") -> Any:
    if not use_real_aws:
        return FakeTable(key_attr=key_attr)
    import boto3  # lazy: only needed in real-AWS mode
    dynamodb = boto3.resource("dynamodb")
    from gateway.telemetry.ledger import LedgerStore
    return LedgerStore.create_table_if_missing(dynamodb, table_name)
