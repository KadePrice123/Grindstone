"""The app-owned backtest data store, fed from recorded market data.

The engine (backend/bt/) reads a deliberately tiny schema: an ``opt`` table
(one row per contract per trading day, strikes in tenths of a cent, cp 0/1)
and an optional ``bars`` table for intraday range. spy_options.db implements
it on this workspace's machines — but a user has no such file, and must never
need to make one. This module gives every install its own database at
data/backtest_data/<UNDERLYING>.db, created empty by the app, and fills it
from what the recorder captured (rec_chain / rec_bars in market.db).

The sync keeps one chain snapshot per trading day — the LAST one taken during
market hours (ET), which is as close to the engine's 16:00-snapshot
convention as recorded data gets. Rows synced here OUTLIVE recorder
retention pruning on purpose: this file is the long-term store the backtests
run on; the recorder's tables are its inbox. Everything here is re-buildable
and sqlite-only — no numpy, safe to import in the sidecar, though the heavy
lifting runs in the runner subprocess or a background thread.

A later data source (TastyTrade recording, a hosted data API) feeds the same
two tables through the same mapping — the engine never needs to know.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

from .db import data_dir

_SCHEMA = """
CREATE TABLE IF NOT EXISTS opt (
    d      INTEGER NOT NULL,   -- trading date, yyyymmdd (ET)
    exp    INTEGER NOT NULL,   -- expiration, yyyymmdd
    cp     INTEGER NOT NULL,   -- 0 = call, 1 = put
    strike INTEGER NOT NULL,   -- price * 1000 (the engine divides back)
    bid    REAL, ask REAL, mark REAL,
    vol    REAL, oi REAL,
    delta  REAL,               -- NULL = let the engine solve a model delta
    iv     REAL,
    src    TEXT,               -- provenance: 'recorded', 'onclickmedia',
    imported_at TEXT,          --   'upload:<file>', 'vault'. NULL = pre-v1.
    PRIMARY KEY (d, exp, cp, strike)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS bars (
    d INTEGER NOT NULL,        -- trading date, yyyymmdd (ET)
    t TEXT NOT NULL,           -- 'H:MM AM/PM' ET, the engine's parse format
    o REAL, h REAL, l REAL, c REAL,
    src TEXT,
    PRIMARY KEY (d, t)
);
CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

#: ONE statement, two writers. sync_from_recorded and import_chain must agree
#: on the column list exactly — they fill the same table and the engine cannot
#: tell which of them wrote a row. Two copies of this text is two chances for
#: them to drift.
_OPT_INSERT = ("INSERT OR REPLACE INTO opt (d, exp, cp, strike, bid, ask,"
               " mark, vol, oi, delta, iv, src, imported_at)"
               " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)")

SCHEMA_VERSION = 1

#: Additive migrations for databases built before SCHEMA_VERSION existed.
#: Guarded by user_version, exactly as marketdb does it — see connect_data.
_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        "ALTER TABLE opt ADD COLUMN src TEXT",
        "ALTER TABLE opt ADD COLUMN imported_at TEXT",
        "ALTER TABLE bars ADD COLUMN src TEXT",
    ),
}

#: Finest first — when several timeframes were recorded, sync the finest.
_TIMEFRAME_ORDER = ("1Min", "5Min", "15Min", "1Hour", "1Day")


def _et(ts_utc: dt.datetime) -> dt.datetime:
    """UTC -> US/Eastern. zoneinfo needs the tzdata package on Windows; the
    fixed EST fallback mislabels one hour around DST changes, which for a
    once-per-day close snapshot costs nothing."""
    try:
        from zoneinfo import ZoneInfo
        return ts_utc.astimezone(ZoneInfo("America/New_York"))
    except Exception:  # noqa: BLE001 — no tz database on this machine
        return ts_utc + dt.timedelta(hours=-5)


def _parse_iso(ts: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _in_market_hours(et: dt.datetime) -> bool:
    """Weekday, 09:30-16:15 ET. Snapshots taken overnight or on weekends are
    stale quotes with a misleading date — they must never become trading
    days, or the engine would happily 'trade' a Saturday."""
    if et.weekday() >= 5:
        return False
    minutes = et.hour * 60 + et.minute
    return 9 * 60 + 30 <= minutes <= 16 * 60 + 15


def _ymd(d: dt.date) -> int:
    return d.year * 10000 + d.month * 100 + d.day


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
def data_db_path(underlying: str) -> Path:
    d = data_dir() / "backtest_data"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{underlying.upper()}.db"


def connect_data(path: Path | str) -> sqlite3.Connection:
    """WAL like market.db (derived, re-buildable, a torn cloud copy costs
    nothing) and the same busy_timeout politeness.

    VERSIONED, because `executescript(_SCHEMA)` alone is a trap that has
    already cost this codebase real behaviour. `CREATE TABLE IF NOT EXISTS`
    delivers new TABLES to an existing database but can never deliver a new
    COLUMN — and there is no error, just a column that silently isn't there.
    market.db proves it: chain_cache and chain_cover were added to its schema
    without bumping its version, and this machine's market.db has never had
    them; the log carries 574 'no such table: chain_cover' fallbacks. So an
    ALTER needs a delivery path, and user_version is it.
    """
    con = sqlite3.connect(str(path), check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
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


def stats(con: sqlite3.Connection) -> dict:
    """What the store holds — the honesty numbers the page shows."""
    row = con.execute(
        "SELECT COUNT(DISTINCT d), MIN(d), MAX(d), COUNT(*) FROM opt").fetchone()
    days, first, last, contracts = row
    bar_days = con.execute("SELECT COUNT(DISTINCT d) FROM bars").fetchone()[0]
    def _fmt(n):
        return f"{n // 10000:04d}-{(n // 100) % 100:02d}-{n % 100:02d}" if n else None
    # WHERE IT CAME FROM. Without this the page can only say "recorded data",
    # which would be a plain lie about a CSV somebody uploaded — and the whole
    # point of the provenance column is that a backtest can be traced to the
    # data it ran on.
    sources = [
        {"src": r[0] or "unknown", "contracts": r[1], "days": r[2]}
        for r in con.execute(
            "SELECT src, COUNT(*), COUNT(DISTINCT d) FROM opt"
            " GROUP BY src ORDER BY COUNT(*) DESC")
    ]
    return {"days": days, "first": _fmt(first), "last": _fmt(last),
            "contracts": contracts, "bar_days": bar_days, "sources": sources}


# ---------------------------------------------------------------------------
class ImportRefused(Exception):
    """The import will not proceed, and why. Same all-or-nothing contract as
    the parser: nothing is written when this is raised."""


def import_chain(data_con: sqlite3.Connection,
                 underlying: str,
                 rows,
                 source: str,
                 progress=None) -> dict:
    """Write parsed chain rows into the engine's own table.

    Mirrors sync_from_recorded deliberately — same connection-first argument,
    same short-transaction-per-day discipline, same result dict — so callers
    can poll either one the same way.

    Two guards here that the parser cannot make, because they are facts about
    THIS table rather than about the file:

    **One underlying per file.** `opt` has no symbol column; the filename is
    the only boundary. A stray QQQ row inside SPY.db does not raise anywhere —
    it silently blends two chains and the engine prices a strike ladder that
    never existed.

    **The imported dates BECOME the trading calendar.** MarketData derives its
    sessions from `SELECT DISTINCT d FROM opt`, so a Saturday row does not get
    skipped: it becomes a day the engine will open positions on. There is no
    downstream check for this, which is why it is a refusal rather than a
    filter — silently dropping rows would break the all-or-nothing promise the
    user was given, and silently keeping them is worse.
    """
    u = underlying.upper()
    stamp = _now_iso()
    rows = list(rows)

    wrong = {r.symbol for r in rows if r.symbol != u}
    if wrong:
        raise ImportRefused(
            f"this file carries {', '.join(sorted(wrong))} but is being imported "
            f"as {u}. The engine's table has no symbol column — the file IS the "
            f"boundary — so mixing them blends two chains with no error anywhere")

    by_day: dict[str, list] = {}
    for r in rows:
        by_day.setdefault(r.date, []).append(r)

    for day in sorted(by_day):
        wd = dt.date.fromisoformat(day).weekday()
        if wd >= 5:
            raise ImportRefused(
                f"{day} is a {'Saturday' if wd == 5 else 'Sunday'}. Imported "
                f"dates become the engine's trading calendar, so this would "
                f"not be skipped — it would become a session the backtest "
                f"opens positions on")

    days_done = 0
    contracts = 0
    total = len(by_day)
    for i, day in enumerate(sorted(by_day)):
        d_i = _ymd(dt.date.fromisoformat(day))
        out = []
        for r in by_day[day]:
            # Prefer a quoted mark; fall back the way the recorder does. NULL
            # when there is nothing honest to say — the engine substitutes
            # 0.5*(bid+ask) itself when mark is missing (bt/data.py:227).
            mark = r.mark
            if mark is None:
                b, a = r.bid or 0.0, r.ask or 0.0
                if b > 0 and a >= b:
                    mark = 0.5 * (b + a)
                elif r.last and r.last > 0:
                    mark = float(r.last)
            out.append((
                d_i, _ymd(dt.date.fromisoformat(r.expiration)),
                0 if r.right == "C" else 1,
                # Must match sync_from_recorded EXACTLY. The engine keys
                # contracts by k/1000.0; any other rounding produces contracts
                # a holding can never find again, and that does not raise —
                # Holding.mark quietly falls back to intrinsic.
                int(round(float(r.strike) * 1000)),
                r.bid, r.ask, mark, r.volume, r.open_interest,
                r.delta, r.iv, source, stamp,
            ))
        with data_con:  # one short transaction per day
            data_con.executemany(
_OPT_INSERT, out)
        days_done += 1
        contracts += len(out)
        if progress:
            progress(i + 1, total)

    return {"days": days_done, "contracts": contracts, "source": source,
            "first": min(by_day) if by_day else None,
            "last": max(by_day) if by_day else None}


def import_bars(data_con: sqlite3.Connection,
                underlying: str,
                rows,
                source: str) -> dict:
    """Bars for intraday range (ATR / day_range). Same symbol boundary."""
    u = underlying.upper()
    rows = list(rows)
    wrong = {r.symbol for r in rows if r.symbol != u}
    if wrong:
        raise ImportRefused(
            f"this file carries {', '.join(sorted(wrong))} but is being "
            f"imported as {u}")
    out = []
    for r in rows:
        if len(r.ts) == 10:                      # date-only: a daily bar
            day = dt.date.fromisoformat(r.ts)
            label = "4:00 PM"
        else:
            parsed = _parse_iso(r.ts)
            if parsed is None:
                continue
            et = _et(parsed)
            day, label = et.date(), et.strftime("%I:%M %p").lstrip("0")
        out.append((_ymd(day), label, r.open, r.high, r.low, r.close, source))
    if out:
        with data_con:
            data_con.executemany(
                "INSERT OR REPLACE INTO bars (d, t, o, h, l, c, src)"
                " VALUES (?,?,?,?,?,?,?)", out)
    return {"bars": len(out), "source": source}


# ---------------------------------------------------------------------------
def sync_from_recorded(market_con: sqlite3.Connection,
                       data_con: sqlite3.Connection,
                       underlying: str,
                       progress=None) -> dict:
    """Incremental: copy every in-hours snapshot day newer than the last sync
    into ``opt`` (latest snapshot per day wins), and newer bars into ``bars``.
    Short transactions per day so the recorder is never starved."""
    u = underlying.upper()
    stamp = _now_iso()
    state = dict(data_con.execute("SELECT key, value FROM sync_state"))
    last_chain = state.get("chain_last_ts", "")

    # One snapshot per day: the last in-hours ts of each ET trading day.
    snap_ts = [r[0] for r in market_con.execute(
        "SELECT DISTINCT ts FROM rec_chain WHERE underlying=? AND ts>?"
        " ORDER BY ts", (u, last_chain))]
    by_day: dict[dt.date, str] = {}
    for ts in snap_ts:
        parsed = _parse_iso(ts)
        if parsed is None:
            continue
        et = _et(parsed)
        if not _in_market_hours(et):
            continue
        by_day[et.date()] = ts  # ascending order: the last one stands

    days_done = 0
    contracts = 0
    newest_ts = last_chain
    for i, (day, ts) in enumerate(sorted(by_day.items())):
        rows = market_con.execute(
            "SELECT expiration, strike, right, bid, ask, last, iv, delta,"
            " volume, open_interest FROM rec_chain WHERE underlying=? AND ts=?",
            (u, ts)).fetchall()
        out = []
        d_i = _ymd(day)
        for r in rows:
            exp = _parse_iso(r["expiration"] + "T00:00:00+00:00")
            if exp is None:
                continue
            # MISSING IS NOT ZERO. The arithmetic below needs numbers, so it
            # uses local zeros — but what gets STORED is what was quoted, and
            # an absent bid is stored as NULL.
            #
            # Be precise about why: for bid/ask/mark the engine coerces the
            # same way we would (bt/data.py:225-227), so this costs it nothing
            # today. It matters because this table is the app's record of what
            # a market showed, and a stored 0.0 is a claim that someone bid
            # zero — indistinguishable, forever, from nobody quoting at all.
            # DATA_IMPORT.md makes that a rule; the import path will honour it,
            # and the two writers must not disagree about the same column.
            b = r["bid"] or 0.0
            a = r["ask"] or 0.0
            if b > 0 and a >= b:
                mark = 0.5 * (b + a)
            elif r["last"] and r["last"] > 0:
                mark = float(r["last"])
            else:
                mark = None  # no quote and no trade: nothing to mark against
            out.append((
                d_i, _ymd(exp.date()), 0 if r["right"] == "C" else 1,
                int(round(float(r["strike"]) * 1000)),
                r["bid"], r["ask"], mark, r["volume"], r["open_interest"],
                r["delta"], r["iv"], "recorded", stamp,
            ))
        with data_con:  # one short transaction per day
            data_con.executemany(
_OPT_INSERT, out)
            data_con.execute(
                "INSERT OR REPLACE INTO sync_state VALUES ('chain_last_ts', ?)", (ts,))
        newest_ts = max(newest_ts, ts)
        days_done += 1
        contracts += len(out)
        if progress:
            progress(i + 1, len(by_day))

    # Bars: the finest recorded timeframe feeds intraday range (ATR/day_range).
    tfs = {r[0] for r in market_con.execute(
        "SELECT DISTINCT timeframe FROM rec_bars WHERE symbol=?", (u,))}
    bars_added = 0
    tf = next((t for t in _TIMEFRAME_ORDER if t in tfs), None)
    if tf is not None:
        last_bars = state.get("bars_last_ts", "") if state.get("bars_timeframe") == tf else ""
        rows = market_con.execute(
            "SELECT ts, open, high, low, close FROM rec_bars"
            " WHERE symbol=? AND timeframe=? AND ts>? ORDER BY ts",
            (u, tf, last_bars)).fetchall()
        out = []
        newest_bar = last_bars
        for r in rows:
            parsed = _parse_iso(r["ts"])
            if parsed is None:
                continue
            et = _et(parsed)
            label = ("4:00 PM" if tf == "1Day"
                     else et.strftime("%I:%M %p").lstrip("0"))
            out.append((_ymd(et.date()), label, r["open"], r["high"],
                        r["low"], r["close"], "recorded"))
            newest_bar = max(newest_bar, r["ts"])
        if out:
            with data_con:
                data_con.executemany(
                    "INSERT OR REPLACE INTO bars (d, t, o, h, l, c, src)"
                    " VALUES (?,?,?,?,?,?,?)", out)
                data_con.execute(
                    "INSERT OR REPLACE INTO sync_state VALUES ('bars_last_ts', ?)",
                    (newest_bar,))
                data_con.execute(
                    "INSERT OR REPLACE INTO sync_state VALUES ('bars_timeframe', ?)",
                    (tf,))
            bars_added = len(out)

    return {"days": days_done, "contracts": contracts, "bars": bars_added,
            "newest_ts": newest_ts or None}
