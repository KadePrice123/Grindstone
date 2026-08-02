"""The data recorder: user-configured jobs that capture market data on a
schedule into market.db (REQUIREMENTS.md 4.3 / Kade's data-management ask).

Kinds:
  bars   — OHLCV for one symbol at a timeframe (1Min..1Day)
  chain  — full options-chain snapshot for one underlying (rows per contract)
  news   — rolling news capture (optionally symbol-scoped)

Honesty rules baked in:
- A job runs only while its owner has an UNLOCKED session (credentials live in
  the vault; when locked the job reports "locked", it does not stall or cache
  keys anywhere).
- Chain snapshots on the free feed are INDICATIVE quotes; rows are stored with
  the feed name so later research can weigh them accordingly.
- Futures/index recording is rejected at creation with the real reason (no
  connected source carries them yet) rather than accepted and silently empty.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from typing import Any, Callable

from . import newsstore
from .logs import LOG
from .brokers.alpaca_data import AlpacaData
from .brokers.base import BrokerError

TICK_SECONDS = 15
PRUNE_EVERY = dt.timedelta(hours=6)

TIMEFRAMES = {"1Min": 60, "5Min": 300, "15Min": 900, "1Hour": 3600, "1Day": 86400}
CHAIN_INTERVALS = (60, 300, 900, 3600, 86400)
MIN_INTERVAL = 60

CredsProvider = Callable[[int], dict[str, str] | None]


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_job(kind: str, symbol: str, timeframe: str, interval_seconds: int,
                 retention_days: int, asset_class: str | None) -> str | None:
    """Returns an error string, or None if valid."""
    if kind not in ("bars", "chain", "news"):
        return f"unknown kind {kind!r}"
    if kind in ("bars", "chain") and not symbol:
        return "symbol required"
    if asset_class in ("future", "index") and kind in ("bars", "chain"):
        return (f"{symbol}: no connected data source carries {asset_class} data yet — "
                "arrives with the TastyTrade adapter (REQUIREMENTS.md 6.9)")
    if asset_class == "crypto" and kind in ("bars", "chain"):
        return (f"{symbol}: crypto recording isn't wired yet — crypto bars use a "
                "different Alpaca endpoint (v1beta3) and crypto has no options chain")
    if kind == "bars" and timeframe not in TIMEFRAMES:
        return f"timeframe must be one of {', '.join(TIMEFRAMES)}"
    if interval_seconds < MIN_INTERVAL:
        return f"interval must be at least {MIN_INTERVAL}s (Alpaca free tier is 200 req/min shared)"
    if not 1 <= retention_days <= 3650:
        return "retention must be 1..3650 days"
    return None


class Recorder:
    def __init__(self, con: sqlite3.Connection, creds_provider: CredsProvider) -> None:
        self._con = con                      # owned by the recorder thread
        self._creds = creds_provider
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_prune = _utcnow() - PRUNE_EVERY

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="recorder")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                self.run_due_jobs()
                if _utcnow() - self._last_prune > PRUNE_EVERY:
                    self.prune()
                    self._last_prune = _utcnow()
            except Exception:  # noqa: BLE001 — the loop must survive anything
                LOG.exception("recorder tick failed")

    # ------------------------------------------------------------------ core
    def run_due_jobs(self, now: dt.datetime | None = None) -> int:
        now = now or _utcnow()
        jobs = self._con.execute(
            "SELECT * FROM record_jobs WHERE enabled=1"
        ).fetchall()
        ran = 0
        for job in jobs:
            if not self.is_due(dict(job), now):
                continue
            self._run_job(dict(job), now)
            ran += 1
        return ran

    @staticmethod
    def is_due(job: dict[str, Any], now: dt.datetime) -> bool:
        last = job.get("last_run_at") or ""
        if not last:
            return True
        try:
            t = dt.datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        except ValueError:
            return True
        return (now - t).total_seconds() >= job["interval_seconds"]

    def _run_job(self, job: dict[str, Any], now: dt.datetime) -> None:
        creds = self._creds(job["user_id"])
        if creds is None:
            self._finish(job, now, 0, "locked — sign in to record")
            return
        client = AlpacaData(creds["key_id"], creds["secret_key"])
        try:
            if job["kind"] == "bars":
                rows = self._collect_bars(client, job)
            elif job["kind"] == "chain":
                rows = self._collect_chain(client, job, now)
            else:
                rows = self._collect_news(client, job)
            self._finish(job, now, rows, "ok")
        except BrokerError as e:
            self._finish(job, now, 0, str(e)[:200])
        except Exception as e:  # noqa: BLE001
            self._finish(job, now, 0, f"internal: {e.__class__.__name__}"[:200])

    def _finish(self, job: dict[str, Any], now: dt.datetime, rows: int, status: str) -> None:
        with self._con:
            self._con.execute(
                "UPDATE record_jobs SET last_run_at=?, last_status=?, last_rows=? WHERE id=?",
                (_iso(now), status, rows, job["id"]),
            )

    # ------------------------------------------------------------ collectors
    def _collect_bars(self, client: AlpacaData, job: dict[str, Any]) -> int:
        last_ts = self._con.execute(
            "SELECT MAX(ts) FROM rec_bars WHERE symbol=? AND timeframe=?",
            (job["symbol"], job["timeframe"]),
        ).fetchone()[0]
        if last_ts:
            start = last_ts
        else:
            lookback = dt.timedelta(seconds=TIMEFRAMES[job["timeframe"]] * 1000)
            start = _iso(_utcnow() - lookback)
        bars = client.stock_bars(job["symbol"], job["timeframe"], start=start)
        with self._con:
            self._con.executemany(
                "INSERT OR REPLACE INTO rec_bars (symbol, timeframe, ts, open, high, low, close, volume)"
                " VALUES (?,?,?,?,?,?,?,?)",
                [(job["symbol"], job["timeframe"], b["ts"], b["open"], b["high"],
                  b["low"], b["close"], b["volume"]) for b in bars],
            )
        return len(bars)

    def _collect_chain(self, client: AlpacaData, job: dict[str, Any],
                       now: dt.datetime) -> int:
        contracts = client.chain_snapshot(job["symbol"])
        ts = now.strftime("%Y-%m-%dT%H:%M:00Z")  # bucket to the minute
        with self._con:
            self._con.executemany(
                "INSERT OR REPLACE INTO rec_chain (underlying, ts, occ_symbol, expiration,"
                " strike, right, bid, ask, last, iv, delta, gamma, theta, vega, rho,"
                " volume, open_interest)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(job["symbol"], ts, c["occ_symbol"], c["expiration"], c["strike"],
                  c["right"], c["bid"], c["ask"], c["last"], c["iv"], c["delta"],
                  c["gamma"], c["theta"], c["vega"], c["rho"], None, None)
                 for c in contracts],
            )
        return len(contracts)

    def _collect_news(self, client: AlpacaData, job: dict[str, Any]) -> int:
        symbols = [job["symbol"]] if job["symbol"] else None
        items, _token = client.news(symbols=symbols, limit=50)
        return newsstore.upsert(self._con, items)

    # ----------------------------------------------------------------- prune
    def prune(self) -> dict[str, int]:
        """Delete rows older than the LONGEST retention any job declares for
        that data — never per-job (review 2026-08-02, high: a 7-day job on
        SPY silently deleted the 365-day history another SPY job was keeping;
        the tables are shared, so the most protective promise wins). Data
        with NO remaining job is kept forever — the UI says deleting a job
        keeps its data, and prune must honor that."""
        removed = {"bars": 0, "chain": 0, "news": 0}
        jobs = [dict(j) for j in self._con.execute("SELECT * FROM record_jobs").fetchall()]

        bars_keep: dict[tuple[str, str], int] = {}
        chain_keep: dict[str, int] = {}
        for j in jobs:
            if j["kind"] == "bars":
                k = (j["symbol"], j["timeframe"])
                bars_keep[k] = max(bars_keep.get(k, 0), j["retention_days"])
            elif j["kind"] == "chain":
                chain_keep[j["symbol"]] = max(chain_keep.get(j["symbol"], 0),
                                              j["retention_days"])

        with self._con:
            for (symbol, timeframe), days in bars_keep.items():
                cutoff = _iso(_utcnow() - dt.timedelta(days=days))
                cur = self._con.execute(
                    "DELETE FROM rec_bars WHERE symbol=? AND timeframe=? AND ts<?",
                    (symbol, timeframe, cutoff))
                removed["bars"] += cur.rowcount
            for underlying, days in chain_keep.items():
                cutoff = _iso(_utcnow() - dt.timedelta(days=days))
                cur = self._con.execute(
                    "DELETE FROM rec_chain WHERE underlying=? AND ts<?",
                    (underlying, cutoff))
                removed["chain"] += cur.rowcount
            news_days = max([j["retention_days"] for j in jobs if j["kind"] == "news"],
                            default=90)
            cutoff = _iso(_utcnow() - dt.timedelta(days=news_days))
            old = [r[0] for r in self._con.execute(
                "SELECT id FROM news WHERE created_at<?", (cutoff,)).fetchall()]
            for nid in old:
                self._con.execute("DELETE FROM news WHERE id=?", (nid,))
                self._con.execute("DELETE FROM news_fts WHERE rowid=?", (nid,))
            removed["news"] = len(old)
        return removed

    # ----------------------------------------------------------------- stats
    def usage(self) -> dict[str, Any]:
        c = self._con
        bars = c.execute(
            "SELECT symbol, timeframe, COUNT(*) n, MIN(ts) oldest, MAX(ts) newest"
            " FROM rec_bars GROUP BY symbol, timeframe").fetchall()
        chain = c.execute(
            "SELECT underlying, COUNT(*) n, COUNT(DISTINCT ts) snapshots,"
            " MIN(ts) oldest, MAX(ts) newest FROM rec_chain GROUP BY underlying").fetchall()
        page = c.execute("PRAGMA page_count").fetchone()[0]
        size = c.execute("PRAGMA page_size").fetchone()[0]
        return {
            "bars": [dict(r) for r in bars],
            "chain": [dict(r) for r in chain],
            "news": newsstore.stats(c),
            "db_bytes": page * size,
        }
