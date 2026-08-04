"""SQLite storage. One file, WAL mode, schema versioned by user_version.

Default location is <project>/data/app.db (gitignored, Drive-synced — safe
because every sensitive value is AES-GCM ciphertext; REQUIREMENTS.md 7.9).
Tests and the packaged app override with GRINDSTONE_DATA_DIR.
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    username    TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pw_hash     TEXT NOT NULL,
    kdf_salt    BLOB NOT NULL,
    wrapped_dek BLOB NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS accounts (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    broker     TEXT NOT NULL CHECK (broker IN ('alpaca','tastytrade','webull','fidelity')),
    kind       TEXT NOT NULL CHECK (kind IN ('live','paper','data')),
    nickname   TEXT NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
    settings   TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key     TEXT NOT NULL,
    value   TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);
CREATE TABLE IF NOT EXISTS secrets (
    id         INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    field      TEXT NOT NULL,
    blob       BLOB NOT NULL,
    hint       TEXT NOT NULL DEFAULT '',
    UNIQUE (account_id, field)
);
CREATE TABLE IF NOT EXISTS favorites (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL CHECK (kind IN ('symbol','page','web')),
    key        TEXT NOT NULL,
    label      TEXT NOT NULL,
    icon       TEXT NOT NULL DEFAULT '',
    pos        INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (user_id, kind, key)
);
CREATE TABLE IF NOT EXISTS backtest_presets (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    spec        TEXT NOT NULL,
    builtin     INTEGER NOT NULL DEFAULT 0,
    calibration INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (user_id, name)
);
"""


def data_dir() -> Path:
    override = os.environ.get("GRINDSTONE_DATA_DIR")
    if override:
        d = Path(override)
    else:
        d = Path(__file__).resolve().parent.parent.parent / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Open, commit-or-rollback, and CLOSE. Closing matters twice over: it
    releases the file handle (Drive holds directory handles on Windows) and it
    guarantees no journal is left beside a cloud-synced database.

    journal_mode is deliberately DELETE, not WAL: this DB lives in a synced
    folder by design, and a -wal sidecar means Drive can upload app.db
    without the newest committed rows — a torn backup missing exactly the
    credentials the user just enrolled. DELETE keeps every committed byte in
    the one file and removes the journal between transactions. Write volume
    here is a few rows per session, so WAL's concurrency buys us nothing.
    """
    con = sqlite3.connect(path or (data_dir() / "app.db"))
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("PRAGMA foreign_keys=ON")
        con.executescript(_SCHEMA)
        with con:
            yield con
    finally:
        con.close()
