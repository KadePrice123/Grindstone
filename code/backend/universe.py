"""The searchable symbol universe.

Synced from Alpaca GET /v2/assets (equities + crypto) into market.db, plus a
hand-maintained supplement for what Alpaca doesn't list (index roots, futures
roots — the REQUIREMENTS.md 6.6 critique gap). Loaded into memory as parallel
lists for RapidFuzz batch scoring.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
from typing import Any

from rapidfuzz import fuzz, process, utils

# Not on Alpaca, but the platform trades or charts them via other routes.
SUPPLEMENT = [
    {"symbol": "SPX", "name": "S&P 500 Index", "exchange": "CBOE", "asset_class": "index", "tradable": False},
    {"symbol": "XSP", "name": "Mini-SPX Index (1/10 SPX)", "exchange": "CBOE", "asset_class": "index", "tradable": False},
    {"symbol": "VIX", "name": "CBOE Volatility Index", "exchange": "CBOE", "asset_class": "index", "tradable": False},
    {"symbol": "NDX", "name": "Nasdaq-100 Index", "exchange": "NASDAQ", "asset_class": "index", "tradable": False},
    {"symbol": "/ES", "name": "E-mini S&P 500 Futures", "exchange": "CME", "asset_class": "future", "tradable": False},
    {"symbol": "/NQ", "name": "E-mini Nasdaq-100 Futures", "exchange": "CME", "asset_class": "future", "tradable": False},
    {"symbol": "/CL", "name": "Crude Oil Futures", "exchange": "NYMEX", "asset_class": "future", "tradable": False},
    {"symbol": "/GC", "name": "Gold Futures", "exchange": "COMEX", "asset_class": "future", "tradable": False},
]

SYNC_STALE_HOURS = 24


class Universe:
    """In-memory search index over the assets table. Thread-safe swap on sync."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._symbols: list[str] = []
        self._names: list[str] = []
        self._haystack: list[str] = []       # "SYM Name" per row
        self._by_symbol: dict[str, dict[str, Any]] = {}

    def load(self, con: sqlite3.Connection) -> int:
        rows = con.execute(
            "SELECT symbol, name, exchange, asset_class, tradable FROM assets"
        ).fetchall()
        entries = [dict(r) for r in rows]
        known = {e["symbol"] for e in entries}
        entries += [s for s in SUPPLEMENT if s["symbol"] not in known]
        with self._lock:
            self._symbols = [e["symbol"] for e in entries]
            self._names = [e["name"] for e in entries]
            self._haystack = [f'{e["symbol"]} {e["name"]}' for e in entries]
            self._by_symbol = {e["symbol"].upper(): e for e in entries}
        return len(entries)

    @property
    def size(self) -> int:
        return len(self._symbols)

    def exact(self, token: str) -> dict[str, Any] | None:
        return self._by_symbol.get(token.upper())

    def prefix(self, q: str, limit: int = 8) -> list[dict[str, Any]]:
        """Symbol-prefix hits first, then company-name-prefix hits."""
        qq = q.upper()
        out: list[dict[str, Any]] = []
        with self._lock:
            for sym in self._symbols:
                if sym.upper().startswith(qq):
                    out.append(self._by_symbol[sym.upper()])
                    if len(out) >= limit:
                        return out
            for sym, name in zip(self._symbols, self._names):
                e = self._by_symbol[sym.upper()]
                if name.upper().startswith(qq) and e not in out:
                    out.append(e)
                    if len(out) >= limit:
                        break
        return out

    # Cutoff 62, not the textbook 70: a one-typo company name ("aple" for
    # Apple) scores ~67 under WRatio against "AAPL Apple Inc..." and a 70
    # cutoff silently drops the right answer while near-miss tickers pass.
    # RRF fusion + the exact/prefix pins keep the extra noise ranked low.
    def fuzzy(self, q: str, limit: int = 10, cutoff: float = 62.0) -> list[tuple[dict[str, Any], float]]:
        with self._lock:
            hay = self._haystack
            syms = self._symbols
        if not hay or len(q) < 2:
            return []
        hits = process.extract(
            q, hay, scorer=fuzz.WRatio, processor=utils.default_process,
            limit=limit, score_cutoff=cutoff,
        )
        return [(self._by_symbol[syms[idx].upper()], score) for (_, score, idx) in hits]


def sync_from_alpaca(con: sqlite3.Connection, data_client, trading_base: str) -> int:
    """Replace the assets table from Alpaca. Returns row count."""
    total = 0
    with con:
        con.execute("DELETE FROM assets")
        for batch in data_client.iter_assets(trading_base):
            con.executemany(
                "INSERT OR REPLACE INTO assets (symbol, name, exchange, asset_class, tradable)"
                " VALUES (:symbol, :name, :exchange, :asset_class, :tradable)",
                batch,
            )
            total += len(batch)
        con.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('universe_synced_at', ?)",
            (dt.datetime.now(dt.timezone.utc).isoformat(),),
        )
    return total


def sync_status(con: sqlite3.Connection) -> dict[str, Any]:
    row = con.execute("SELECT value FROM meta WHERE key='universe_synced_at'").fetchone()
    count = con.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    synced_at = row["value"] if row else None
    stale = True
    if synced_at:
        try:
            t = dt.datetime.fromisoformat(synced_at)
            stale = (dt.datetime.now(dt.timezone.utc) - t) > dt.timedelta(hours=SYNC_STALE_HOURS)
        except ValueError:
            pass
    return {"synced_at": synced_at, "count": count, "stale": stale}
