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
-- What Jarvis knows about the user (name, work, projects, preferences, people).
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    category TEXT NOT NULL,
    text TEXT NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0
);
-- The user's personal database: tasks, expenses, contacts, ideas… (free-form collections).
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    collection TEXT NOT NULL,
    text TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '',
    due_ts REAL,
    done INTEGER NOT NULL DEFAULT 0,
    deleted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    due_ts REAL NOT NULL,
    message TEXT NOT NULL,
    fired INTEGER NOT NULL DEFAULT 0
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

    def last_event_id(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]

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

    # --- memory ----------------------------------------------------------
    def remember(self, text: str, category: str = "general") -> int:
        with self._conn() as c:
            # Don't store the exact same fact twice.
            row = c.execute("SELECT id FROM memories WHERE deleted=0 AND lower(text)=lower(?)", (text,)).fetchone()
            if row:
                return row["id"]
            return c.execute("INSERT INTO memories (ts, category, text) VALUES (?,?,?)",
                             (time.time(), category, text)).lastrowid

    def memories(self, query: str = "", limit: int = 60) -> list[dict[str, Any]]:
        words = [w.lower() for w in query.split() if len(w) > 1]
        with self._conn() as c:
            rows = c.execute("SELECT id, ts, category, text FROM memories WHERE deleted=0 ORDER BY id DESC").fetchall()
        items = [dict(r) for r in rows]
        if words:
            items = [m for m in items if any(w in (m["text"] + " " + m["category"]).lower() for w in words)]
        return items[:limit]

    def forget(self, memory_id: int) -> bool:
        with self._conn() as c:  # soft delete: recoverable from the database file
            return c.execute("UPDATE memories SET deleted=1 WHERE id=? AND deleted=0", (memory_id,)).rowcount == 1

    # --- personal database ------------------------------------------------
    def add_record(self, collection: str, text: str, details: str = "", due_ts: float | None = None) -> int:
        with self._conn() as c:
            return c.execute("INSERT INTO records (ts, collection, text, details, due_ts) VALUES (?,?,?,?,?)",
                             (time.time(), collection.strip().lower(), text, details, due_ts)).lastrowid

    def find_records(self, collection: str = "", query: str = "", include_done: bool = False,
                     limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM records WHERE deleted=0"
        args: list[Any] = []
        if collection:
            sql += " AND collection=?"
            args.append(collection.strip().lower())
        if not include_done:
            sql += " AND done=0"
        with self._conn() as c:
            rows = [dict(r) for r in c.execute(sql + " ORDER BY id DESC", args).fetchall()]
        words = [w.lower() for w in query.split() if len(w) > 1]
        if words:
            rows = [r for r in rows if any(w in (r["text"] + " " + r["details"]).lower() for w in words)]
        for r in rows:
            r.pop("deleted", None)
        return rows[:limit]

    def collections(self) -> dict[str, int]:
        with self._conn() as c:
            rows = c.execute("SELECT collection, COUNT(*) n FROM records WHERE deleted=0 AND done=0 "
                             "GROUP BY collection ORDER BY n DESC").fetchall()
        return {r["collection"]: r["n"] for r in rows}

    def update_record(self, record_id: int, *, done: bool | None = None, text: str | None = None,
                      details: str | None = None, deleted: bool | None = None) -> bool:
        sets, args = [], []
        for col, val in (("done", done), ("text", text), ("details", details), ("deleted", deleted)):
            if val is not None:
                sets.append(f"{col}=?")
                args.append(int(val) if isinstance(val, bool) else val)
        if not sets:
            return False
        with self._conn() as c:
            return c.execute(f"UPDATE records SET {', '.join(sets)} WHERE id=?", (*args, record_id)).rowcount == 1

    # --- reminders (persistent, fired by the assistant's background check) ---
    def add_reminder(self, due_ts: float, message: str) -> int:
        with self._conn() as c:
            return c.execute("INSERT INTO reminders (due_ts, message) VALUES (?,?)", (due_ts, message)).lastrowid

    def take_due_reminders(self, now: float | None = None) -> list[dict[str, Any]]:
        now = time.time() if now is None else now
        with self._conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM reminders WHERE fired=0 AND due_ts<=? ORDER BY due_ts", (now,)).fetchall()]
            c.executemany("UPDATE reminders SET fired=1 WHERE id=?", [(r["id"],) for r in rows])
        return rows

    def upcoming_reminders(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM reminders WHERE fired=0 ORDER BY due_ts LIMIT ?", (limit,)).fetchall()]
