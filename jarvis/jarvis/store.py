"""SQLite-backed audit log and approval queue, shared by every process.

The bridge, the MCP server (spawned by Claude) and the dashboard are separate
processes, so they coordinate through this one file rather than in memory.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    stage TEXT NOT NULL,     -- ears | brain | policy | hands | human
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    action TEXT NOT NULL,
    args TEXT NOT NULL,
    status TEXT NOT NULL,    -- pending | approved | rejected | expired
    decided_ts REAL
);
"""


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # --- audit log -------------------------------------------------------
    def log(self, stage: str, kind: str, summary: str, **data: Any) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO events (ts, stage, kind, summary, data) VALUES (?,?,?,?,?)",
                (time.time(), stage, kind, summary, json.dumps(data, ensure_ascii=False, default=str)),
            )

    def events(self, after_id: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM events WHERE id > ? ORDER BY id DESC LIMIT ?", (after_id, limit)
            ).fetchall()
        return [{**dict(r), "data": json.loads(r["data"])} for r in reversed(rows)]

    # --- approvals -------------------------------------------------------
    def create_approval(self, action: str, args: dict[str, Any]) -> str:
        req_id = uuid.uuid4().hex[:8]
        with self._conn() as c:
            c.execute(
                "INSERT INTO approvals (id, ts, action, args, status) VALUES (?,?,?,?, 'pending')",
                (req_id, time.time(), action, json.dumps(args, ensure_ascii=False)),
            )
        return req_id

    def decide(self, req_id: str, approved: bool) -> bool:
        """Human decision. Returns False if the request is no longer pending."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE approvals SET status=?, decided_ts=? WHERE id=? AND status='pending'",
                ("approved" if approved else "rejected", time.time(), req_id),
            )
        return cur.rowcount == 1

    def expire(self, req_id: str) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE approvals SET status='expired', decided_ts=? WHERE id=? AND status='pending'",
                (time.time(), req_id),
            )
        return cur.rowcount == 1

    def expire_stale(self, max_age_s: float) -> int:
        """Expire pending requests nobody is waiting on any more (e.g. after a crash)."""
        now = time.time()
        with self._conn() as c:
            cur = c.execute(
                "UPDATE approvals SET status='expired', decided_ts=? WHERE status='pending' AND ts < ?",
                (now, now - max_age_s),
            )
        return cur.rowcount

    def approval_status(self, req_id: str) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT status FROM approvals WHERE id=?", (req_id,)).fetchone()
        return row["status"] if row else None

    def pending_approvals(self) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM approvals WHERE status='pending' ORDER BY ts").fetchall()
        return [{**dict(r), "args": json.loads(r["args"])} for r in rows]
