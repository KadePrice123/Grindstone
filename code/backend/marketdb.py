"""Market-data SQLite store — a SEPARATE file from app.db.

app.db holds credentials and user state: small, synced, journal_mode=DELETE so
the cloud copy is never torn (db.py). market.db holds the ticker universe,
news, and recorded data: bigger, write-heavy, and every byte is re-fetchable —
so WAL is the right tradeoff here, and a torn cloud copy costs nothing.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .db import data_dir

_SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    symbol      TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    exchange    TEXT NOT NULL DEFAULT '',
    asset_class TEXT NOT NULL DEFAULT 'us_equity',
    tradable    INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS news (
    id         INTEGER PRIMARY KEY,
    headline   TEXT NOT NULL,
    summary    TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT '',
    url        TEXT NOT NULL DEFAULT '',
    symbols    TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    content    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_news_created ON news(created_at DESC);
CREATE VIRTUAL TABLE IF NOT EXISTS news_fts USING fts5(
    headline, summary, tokenize='trigram'
);
CREATE TABLE IF NOT EXISTS record_jobs (
    id               INTEGER PRIMARY KEY,
    user_id          INTEGER NOT NULL,
    kind             TEXT NOT NULL CHECK (kind IN ('bars','chain','news')),
    symbol           TEXT NOT NULL DEFAULT '',
    timeframe        TEXT NOT NULL DEFAULT '',
    interval_seconds INTEGER NOT NULL,
    retention_days   INTEGER NOT NULL DEFAULT 90,
    enabled          INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    last_run_at      TEXT NOT NULL DEFAULT '',
    last_status      TEXT NOT NULL DEFAULT 'never ran',
    last_rows        INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS rec_bars (
    symbol    TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    ts        TEXT NOT NULL,
    open      REAL, high REAL, low REAL, close REAL,
    volume    REAL,
    PRIMARY KEY (symbol, timeframe, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS rec_chain (
    underlying TEXT NOT NULL,
    ts         TEXT NOT NULL,
    occ_symbol TEXT NOT NULL,
    expiration TEXT NOT NULL,
    strike     REAL NOT NULL,
    right      TEXT NOT NULL CHECK (right IN ('C','P')),
    bid        REAL, ask REAL, last REAL,
    iv         REAL, delta REAL, gamma REAL, theta REAL, vega REAL, rho REAL,
    volume     REAL, open_interest REAL,
    PRIMARY KEY (underlying, ts, occ_symbol)
) WITHOUT ROWID;
"""


def market_path() -> Path:
    return data_dir() / "market.db"


SCHEMA_VERSION = 2

# Additive migrations, applied in order for databases created before the
# current SCHEMA_VERSION. Keep them idempotent-safe: the guard is
# user_version, not try/except.
_MIGRATIONS: dict[int, tuple[str, ...]] = {
    2: ("ALTER TABLE news ADD COLUMN content TEXT NOT NULL DEFAULT ''",),
}


def connect_market(path: Path | None = None) -> sqlite3.Connection:
    """Long-lived connections are fine here (recorder thread owns one);
    request handlers should still open-use-close.

    busy_timeout is load-bearing: three writers share this file (recorder,
    login-time refresh, the search route's live-news upsert). SQLite's default
    timeout is ZERO — the first collision was an instant 'database is locked'
    500 in production (observed the moment a user searched during the initial
    universe sync). 5s of politeness fixes what no amount of WAL does,
    because WAL only de-conflicts readers from writers, not writers from
    writers."""
    con = sqlite3.connect(path or market_path(), check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    # DDL on every connect also takes write locks; run it once per schema
    # version instead of on every request.
    have = con.execute("PRAGMA user_version").fetchone()[0]
    if have != SCHEMA_VERSION:
        con.executescript(_SCHEMA)  # creates anything missing
        for version in sorted(_MIGRATIONS):
            if have < version:
                for stmt in _MIGRATIONS[version]:
                    try:
                        con.execute(stmt)
                    except sqlite3.OperationalError:
                        pass  # column already present on a fresh _SCHEMA build
        con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        con.commit()
    return con
