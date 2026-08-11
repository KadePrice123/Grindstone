"""Market-data SQLite store — a SEPARATE file from app.db.

app.db holds credentials and user state: small, synced, journal_mode=DELETE so
the cloud copy is never torn (db.py). market.db holds the ticker universe,
news, and recorded data: bigger, write-heavy, and every byte is re-fetchable —
so WAL is the right tradeoff here, and a torn cloud copy costs nothing.
"""
from __future__ import annotations

import datetime as dt
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
-- Chart bars kept after a live fetch, so a chart still draws offline and a
-- second look does not re-hit the provider. DELIBERATELY NOT rec_bars: that
-- table is the user's own recording, it is shown as such on the Data page,
-- Recorder.prune keeps it forever when no job owns it, and btdata promotes it
-- into the BACKTEST store. Chart bars quietly becoming backtest inputs is the
-- worst outcome available here.
CREATE TABLE IF NOT EXISTS bar_cache (
    symbol    TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    ts        TEXT NOT NULL,
    o REAL, h REAL, l REAL, c REAL, v REAL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (symbol, timeframe, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS bar_cache_age ON bar_cache (fetched_at);

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
-- Chain CACHE. Distinct from rec_chain, which is the recorder's HISTORY: that
-- one keeps a snapshot per timestamp on purpose, and serving live filtering out
-- of it would either replay stale prices or grow without bound. This holds one
-- current row per contract, replaced on every refresh.
CREATE TABLE IF NOT EXISTS chain_cache (
    underlying TEXT NOT NULL,
    occ_symbol TEXT NOT NULL,
    expiration TEXT NOT NULL,
    strike     REAL NOT NULL,
    right      TEXT NOT NULL CHECK (right IN ('C','P')),
    bid        REAL, ask REAL, last REAL,
    iv         REAL, delta REAL, gamma REAL, theta REAL, vega REAL, rho REAL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (underlying, occ_symbol)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS chain_cache_win
    ON chain_cache (underlying, expiration, strike);
-- WHICH WINDOWS WE HAVE ACTUALLY ASKED FOR, and when. Rows alone cannot answer
-- that: a window with genuinely no contracts is indistinguishable from one
-- never fetched, and a partially covered window would read as complete. A hit
-- is a cover row that CONTAINS the request and is still inside the user's
-- retention, which is what makes dragging a leg a few dollars free.
CREATE TABLE IF NOT EXISTS chain_cover (
    underlying TEXT NOT NULL,
    exp_from   TEXT NOT NULL,
    exp_to     TEXT NOT NULL,
    strike_lo  REAL NOT NULL,
    strike_hi  REAL NOT NULL,
    right      TEXT NOT NULL,          -- '' = both rights
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (underlying, exp_from, exp_to, strike_lo, strike_hi, right)
) WITHOUT ROWID;
-- COVERAGE (DS-15): what we have, what is genuinely not there, and what we
-- simply have not asked for yet. Without the have/absent distinction a
-- backfill either re-requests a market holiday forever or writes an API
-- outage down as "the market was closed".
--
--   have    a row exists for this period
--   absent  an AUTHORITATIVE provider positively says there is nothing here
--           (a weekend, a holiday, before listing) -- suppresses retries
--   failed  the request errored -- retryable
--   unknown never asked -- retryable
--
-- `period` is a plain ISO date for daily and intraday work; a coarser kind
-- can use any sortable string. Keyed by PROVIDER too, because "Alpaca has no
-- 2019 for this name" says nothing about OnclickMedia.
CREATE TABLE IF NOT EXISTS data_cover (
    provider   TEXT NOT NULL,
    kind       TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    timeframe  TEXT NOT NULL DEFAULT '',
    period     TEXT NOT NULL,
    state      TEXT NOT NULL CHECK (state IN ('have','absent','failed','unknown')),
    rows       INTEGER NOT NULL DEFAULT 0,
    attempts   INTEGER NOT NULL DEFAULT 0,
    checked_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    detail     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (provider, kind, symbol, timeframe, period)
);
CREATE INDEX IF NOT EXISTS ix_cover_gap
    ON data_cover (kind, symbol, timeframe, state, period);

CREATE TABLE IF NOT EXISTS backtest_runs (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL,
    preset_id    INTEGER,
    name         TEXT NOT NULL DEFAULT '',
    kind         TEXT NOT NULL CHECK (kind IN ('run','calibration')),
    spec         TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'running'
                 CHECK (status IN ('running','done','error','cancelled')),
    started_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    finished_at  TEXT NOT NULL DEFAULT '',
    error        TEXT NOT NULL DEFAULT '',
    summary      TEXT NOT NULL DEFAULT '{}',
    trades       TEXT NOT NULL DEFAULT '[]',
    daily        TEXT NOT NULL DEFAULT '[]',
    calib        TEXT NOT NULL DEFAULT 'null',
    report_files TEXT NOT NULL DEFAULT '[]'
);
"""


def cache_keep_days(pref: str | None) -> int | None:
    """The retention setting as a number of days. 0 = do not cache at all,
    None = keep forever. Anything unrecognised falls back to the default
    rather than to 'forever' or to 'off' — both of those are decisions the
    user did not make."""
    p = (pref or "30").strip().lower()
    if p == "off":
        return 0
    if p == "forever":
        return None
    try:
        return max(1, int(p))
    except ValueError:
        return 30


def bar_cache_store(con: sqlite3.Connection, symbol: str, timeframe: str,
                    bars: list[dict], keep_days: int | None) -> int:
    """Keep bars a live fetch just produced, and sweep old ones in the SAME
    transaction — one deleter, so this can never race the recorder's prune
    over rows neither of them fully owns."""
    if keep_days == 0 or not bars:
        return 0
    now = dt.datetime.now(dt.timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [(symbol, timeframe, b.get("ts"), b.get("open"), b.get("high"),
             b.get("low"), b.get("close"), b.get("volume"), stamp)
            for b in bars if b.get("ts")]
    if not rows:
        return 0
    with con:
        con.executemany(
            "INSERT OR REPLACE INTO bar_cache"
            " (symbol, timeframe, ts, o, h, l, c, v, fetched_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)", rows)
        if keep_days is not None:
            cutoff = (now - dt.timedelta(days=keep_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
            con.execute("DELETE FROM bar_cache WHERE fetched_at < ?", (cutoff,))
    return len(rows)


def bar_cache_read(con: sqlite3.Connection, symbol: str, timeframe: str,
                   limit: int) -> tuple[list[dict], str | None]:
    """Cached bars, newest `limit`, plus when they were last refreshed. The
    caller must show that age: a chart drawn from a week-old cache and one
    drawn live are the same picture and very different claims."""
    rows = con.execute(
        "SELECT ts, o, h, l, c, v, fetched_at FROM bar_cache"
        " WHERE symbol=? AND timeframe=? ORDER BY ts DESC LIMIT ?",
        (symbol, timeframe, limit)).fetchall()
    if not rows:
        return [], None
    bars = [{"ts": r["ts"], "open": r["o"], "high": r["h"], "low": r["l"],
             "close": r["c"], "volume": r["v"]} for r in reversed(rows)]
    return bars, max(r["fetched_at"] for r in rows)




def market_path() -> Path:
    return data_dir() / "market.db"


SCHEMA_VERSION = 6

# Additive migrations, applied in order for databases created before the
# current SCHEMA_VERSION. Keep them idempotent-safe: the guard is
# user_version, not try/except.
_MIGRATIONS: dict[int, tuple[str, ...]] = {
    2: ("ALTER TABLE news ADD COLUMN content TEXT NOT NULL DEFAULT ''",),
    # 4: bar_cache — a new table. Bumping the version is the whole point:
    # chain_cache and chain_cover were added to _SCHEMA at version 3 WITHOUT a
    # bump, so every database already stamped 3 skipped the executescript that
    # would have created them. This machine's market.db has never had them and
    # its log carries hundreds of 'no such table: chain_cover' fallbacks, which
    # means option-chain caching has silently never run. This bump delivers all
    # three tables to every existing install.
    # 3: backtest_runs — a new table, created by the _SCHEMA executescript
    # that runs on any version mismatch; no ALTERs needed.
    6: ("ALTER TABLE data_cover ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",),
    # 5: data_cover — likewise a new table, delivered by the executescript.
    # Listed here only so the version history reads as a history; the bump
    # itself is what makes an existing market.db receive it. (chain_cover was
    # added without a bump once and never reached a single install.)
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
