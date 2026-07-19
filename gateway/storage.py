"""Storage backends (spec §3: DynamoDB, deliberately the ONE data store).

Local/dev/test mode uses FakeTable -- an in-memory object implementing the tiny
slice of the boto3 DynamoDB Table API the gateway uses (put_item / get_item /
scan). Production mode (CONDUIT_USE_REAL_AWS=1) returns a real boto3 Table with
the identical interface, so the code path never branches on environment.

boto3 is imported lazily -- the local service and the whole test suite run with
zero AWS dependencies installed.

Three backends, one interface:
  - FakeTable    -- process-memory; tests only.
  - SqliteTable  -- durable single-file store; THE default for a running
    service. Senior call (decision D2): for a single-instance gateway with
    infrequent users, SQLite is the right production store, not a stopgap --
    the daily hard spend cap must survive restarts to be a real safety
    mechanism. DynamoDB remains the documented multi-instance/cloud path.
  - real DynamoDB (CONDUIT_USE_REAL_AWS=1) -- unchanged interface.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
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


class SqliteTable:
    """Durable single-file store with the same tiny Table interface. Items are
    stored as JSON (Decimal and friends serialized via str -- day_totals and
    callers coerce with float())."""

    def __init__(self, path: str, key_attr: str = "request_id") -> None:
        self._key = key_attr
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS items (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        self._conn.commit()
        self._lock = threading.Lock()

    def put_item(self, Item: dict) -> None:
        payload = json.dumps(Item, default=str)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO items (k, v) VALUES (?, ?)",
                (Item[self._key], payload))
            self._conn.commit()

    def get_item(self, Key: dict) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT v FROM items WHERE k = ?", (Key[self._key],)).fetchone()
        return {"Item": json.loads(row[0])} if row else {}

    def scan(self, **_: Any) -> dict:
        with self._lock:
            rows = self._conn.execute("SELECT v FROM items").fetchall()
        return {"Items": [json.loads(r[0]) for r in rows]}


def get_ledger_table(use_real_aws: bool = False, db_path: Optional[str] = None,
                     table_name: str = "conduit-ledger",
                     key_attr: str = "request_id") -> Any:
    if use_real_aws:
        import boto3  # lazy: only needed in real-AWS mode
        dynamodb = boto3.resource("dynamodb")
        from gateway.telemetry.ledger import LedgerStore
        return LedgerStore.create_table_if_missing(dynamodb, table_name)
    if db_path:
        return SqliteTable(db_path, key_attr=key_attr)
    return FakeTable(key_attr=key_attr)  # tests / explicit ephemeral mode
