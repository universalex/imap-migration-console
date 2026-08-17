"""Tiny SQLite layer. One process, one connection, guarded by a lock."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

from . import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT    NOT NULL,
    local_user      TEXT    NOT NULL UNIQUE,
    src_host        TEXT    NOT NULL,
    src_port        INTEGER NOT NULL,
    src_security    TEXT    NOT NULL,
    src_user        TEXT    NOT NULL,
    src_password    TEXT    NOT NULL,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    last_backup_at  TEXT,
    mailbox_bytes   INTEGER NOT NULL DEFAULT 0,
    mailbox_messages INTEGER NOT NULL DEFAULT 0,
    mailbox_folders INTEGER NOT NULL DEFAULT 0,
    scanned_at      TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    kind            TEXT    NOT NULL,
    status          TEXT    NOT NULL,
    created_at      TEXT    NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    exit_code       INTEGER,
    log_file        TEXT,
    options         TEXT    NOT NULL DEFAULT '{}',
    target_password TEXT,
    progress        TEXT    NOT NULL DEFAULT '{}',
    stats           TEXT    NOT NULL DEFAULT '{}',
    error           TEXT
);

CREATE INDEX IF NOT EXISTS jobs_account_idx ON jobs(account_id);
CREATE INDEX IF NOT EXISTS jobs_status_idx  ON jobs(status);
CREATE UNIQUE INDEX IF NOT EXISTS accounts_identity_idx
    ON accounts(email, src_host, src_user);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> None:
    global _conn
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA foreign_keys=ON")
    with _lock:
        _conn.executescript(SCHEMA)
        _conn.commit()


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


@contextmanager
def cursor() -> Iterator[sqlite3.Cursor]:
    assert _conn is not None, "db.connect() was not called"
    with _lock:
        cur = _conn.cursor()
        try:
            yield cur
            _conn.commit()
        finally:
            cur.close()


def query(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    with cursor() as cur:
        cur.execute(sql, tuple(params))
        return cur.fetchall()


def query_one(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    with cursor() as cur:
        cur.execute(sql, tuple(params))
        return cur.lastrowid or 0
