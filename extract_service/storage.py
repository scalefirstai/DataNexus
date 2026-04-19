"""SQLite-backed persistence for extract state, idempotency, entitlements, audit."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import get_settings
from .models import ExtractStatus, utcnow


SCHEMA = """
CREATE TABLE IF NOT EXISTS extracts (
    extract_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    app_id TEXT NOT NULL,
    domain TEXT NOT NULL,
    status TEXT NOT NULL,
    request_json TEXT NOT NULL,
    state_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    cancelled_at TEXT,
    expires_at TEXT,
    run_id TEXT,
    priority TEXT NOT NULL DEFAULT 'normal',
    expiring_event_sent INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_extracts_app ON extracts(app_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_extracts_status ON extracts(status);
CREATE INDEX IF NOT EXISTS idx_extracts_domain ON extracts(domain);
CREATE INDEX IF NOT EXISTS idx_extracts_expires ON extracts(expires_at);

CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT NOT NULL,
    app_id TEXT NOT NULL,
    extract_id TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (key, app_id)
);

CREATE TABLE IF NOT EXISTS entitlements (
    app_id TEXT NOT NULL,
    fund_id TEXT NOT NULL,
    PRIMARY KEY (app_id, fund_id)
);

CREATE TABLE IF NOT EXISTS applications (
    app_id TEXT PRIMARY KEY,
    token TEXT NOT NULL,
    webhook_secret TEXT,
    cert_fingerprint TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    app_id TEXT,
    action TEXT NOT NULL,
    resource TEXT,
    outcome TEXT NOT NULL,
    details TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(timestamp);

CREATE TABLE IF NOT EXISTS rate_limit_events (
    app_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rl ON rate_limit_events(app_id, kind, ts);
"""


_lock = threading.RLock()


class Storage:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with _lock:
            conn = self._connect()
            try:
                conn.executescript(SCHEMA)
                conn.commit()
            finally:
                conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with _lock:
            conn = self._connect()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    # ---------------- applications & entitlements ----------------

    def register_app(
        self,
        app_id: str,
        token: str,
        webhook_secret: str | None = None,
        cert_fingerprint: str | None = None,
        fund_ids: list[str] | None = None,
    ) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO applications(app_id, token, webhook_secret, cert_fingerprint) VALUES (?, ?, ?, ?)",
                (app_id, token, webhook_secret, cert_fingerprint),
            )
            if fund_ids:
                for f in fund_ids:
                    conn.execute(
                        "INSERT OR IGNORE INTO entitlements(app_id, fund_id) VALUES (?, ?)",
                        (app_id, f),
                    )

    def lookup_app_by_token(self, token: str) -> dict[str, Any] | None:
        with self.tx() as conn:
            row = conn.execute(
                "SELECT * FROM applications WHERE token=?", (token,)
            ).fetchone()
        return dict(row) if row else None

    def get_app(self, app_id: str) -> dict[str, Any] | None:
        with self.tx() as conn:
            row = conn.execute(
                "SELECT * FROM applications WHERE app_id=?", (app_id,)
            ).fetchone()
        return dict(row) if row else None

    def entitled_funds(self, app_id: str) -> set[str]:
        with self.tx() as conn:
            rows = conn.execute(
                "SELECT fund_id FROM entitlements WHERE app_id=?", (app_id,)
            ).fetchall()
        return {r["fund_id"] for r in rows}

    # ---------------- idempotency ----------------

    def lookup_idempotency(
        self, app_id: str, key: str
    ) -> dict[str, Any] | None:
        cutoff = (
            utcnow()
            - timedelta(days=get_settings().idempotency_key_ttl_days)
        ).isoformat()
        with self.tx() as conn:
            row = conn.execute(
                "SELECT * FROM idempotency WHERE app_id=? AND key=? AND created_at>=?",
                (app_id, key, cutoff),
            ).fetchone()
        return dict(row) if row else None

    def record_idempotency(
        self, app_id: str, key: str, extract_id: str, fingerprint: str
    ) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO idempotency(key, app_id, extract_id, request_fingerprint, created_at) VALUES (?, ?, ?, ?, ?)",
                (key, app_id, extract_id, fingerprint, utcnow().isoformat()),
            )

    # ---------------- extracts ----------------

    def create_extract(
        self,
        extract_id: str,
        idempotency_key: str,
        app_id: str,
        domain: str,
        priority: str,
        request_json: dict[str, Any],
        state_json: dict[str, Any],
    ) -> None:
        now = utcnow().isoformat()
        with self.tx() as conn:
            conn.execute(
                """INSERT INTO extracts(extract_id, idempotency_key, app_id, domain, status,
                                        request_json, state_json, created_at, updated_at, priority)
                   VALUES (?, ?, ?, ?, 'ACCEPTED', ?, ?, ?, ?, ?)""",
                (
                    extract_id,
                    idempotency_key,
                    app_id,
                    domain,
                    json.dumps(request_json),
                    json.dumps(state_json),
                    now,
                    now,
                    priority,
                ),
            )

    def get_extract(self, extract_id: str) -> dict[str, Any] | None:
        with self.tx() as conn:
            row = conn.execute(
                "SELECT * FROM extracts WHERE extract_id=?", (extract_id,)
            ).fetchone()
        if not row:
            return None
        r = dict(row)
        r["request"] = json.loads(r.pop("request_json"))
        r["state"] = json.loads(r.pop("state_json"))
        return r

    def update_extract(
        self,
        extract_id: str,
        *,
        status: ExtractStatus | None = None,
        state_json: dict[str, Any] | None = None,
        completed_at: datetime | None = None,
        cancelled_at: datetime | None = None,
        expires_at: datetime | None = None,
        run_id: str | None = None,
        expiring_event_sent: bool | None = None,
    ) -> None:
        sets: list[str] = []
        vals: list[Any] = []
        if status is not None:
            sets.append("status=?")
            vals.append(status.value)
        if state_json is not None:
            sets.append("state_json=?")
            vals.append(json.dumps(state_json))
        if completed_at is not None:
            sets.append("completed_at=?")
            vals.append(completed_at.isoformat())
        if cancelled_at is not None:
            sets.append("cancelled_at=?")
            vals.append(cancelled_at.isoformat())
        if expires_at is not None:
            sets.append("expires_at=?")
            vals.append(expires_at.isoformat())
        if run_id is not None:
            sets.append("run_id=?")
            vals.append(run_id)
        if expiring_event_sent is not None:
            sets.append("expiring_event_sent=?")
            vals.append(1 if expiring_event_sent else 0)
        sets.append("updated_at=?")
        vals.append(utcnow().isoformat())
        vals.append(extract_id)
        with self.tx() as conn:
            conn.execute(
                f"UPDATE extracts SET {', '.join(sets)} WHERE extract_id=?", vals
            )

    def list_extracts(
        self,
        app_id: str,
        *,
        domain: str | None = None,
        statuses: list[str] | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        fund_id: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["app_id=?"]
        params: list[Any] = [app_id]
        if domain:
            clauses.append("domain=?")
            params.append(domain)
        if statuses:
            placeholders = ",".join("?" * len(statuses))
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)
        if created_after:
            clauses.append("created_at>=?")
            params.append(created_after.isoformat())
        if created_before:
            clauses.append("created_at<?")
            params.append(created_before.isoformat())
        if cursor:
            clauses.append("created_at<?")
            params.append(cursor)
        limit_q = limit + 1
        sql = (
            f"SELECT * FROM extracts WHERE {' AND '.join(clauses)} "
            f"ORDER BY created_at DESC LIMIT ?"
        )
        params.append(limit_q)
        with self.tx() as conn:
            rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        if fund_id:
            filtered = []
            for r in rows:
                req = json.loads(r["request_json"])
                scope = req.get("fund_scope") or []
                if not scope or fund_id in scope:
                    filtered.append(r)
            rows = filtered
        return rows

    def find_expiring(
        self, warn_window: timedelta
    ) -> list[dict[str, Any]]:
        now = utcnow()
        warn_cutoff = (now + warn_window).isoformat()
        with self.tx() as conn:
            rows = conn.execute(
                """SELECT * FROM extracts
                   WHERE status='COMPLETED' AND expiring_event_sent=0
                     AND expires_at IS NOT NULL AND expires_at<=?""",
                (warn_cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]

    def find_expired(self) -> list[dict[str, Any]]:
        now = utcnow().isoformat()
        with self.tx() as conn:
            rows = conn.execute(
                """SELECT * FROM extracts
                   WHERE status IN ('COMPLETED','PARTIAL')
                     AND expires_at IS NOT NULL AND expires_at<=?""",
                (now,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- audit ----------------

    def audit(
        self,
        app_id: str | None,
        action: str,
        resource: str | None,
        outcome: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO audit_log(timestamp, app_id, action, resource, outcome, details) VALUES (?,?,?,?,?,?)",
                (
                    utcnow().isoformat(),
                    app_id,
                    action,
                    resource,
                    outcome,
                    json.dumps(details or {}),
                ),
            )

    # ---------------- rate limit events ----------------

    def record_rl_event(self, app_id: str, kind: str, ts: float) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO rate_limit_events(app_id, kind, ts) VALUES (?,?,?)",
                (app_id, kind, ts),
            )

    def count_rl_events(self, app_id: str, kind: str, since_ts: float) -> int:
        with self.tx() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM rate_limit_events WHERE app_id=? AND kind=? AND ts>=?",
                (app_id, kind, since_ts),
            ).fetchone()
        return int(row["c"])

    def prune_rl_events(self, before_ts: float) -> None:
        with self.tx() as conn:
            conn.execute(
                "DELETE FROM rate_limit_events WHERE ts<?", (before_ts,)
            )

    def count_active_extracts(self, app_id: str) -> int:
        with self.tx() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM extracts WHERE app_id=? AND status IN ('ACCEPTED','RUNNING')",
                (app_id,),
            ).fetchone()
        return int(row["c"])


_storage: Storage | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = Storage(get_settings().db_path)
    return _storage


def reset_storage_for_tests() -> None:
    global _storage
    _storage = None
