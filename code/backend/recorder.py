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

from . import backfill, btdata, chainimport, coverage, newsstore, onclick
from .logs import LOG
from .brokers.alpaca_data import AlpacaData
from .brokers.base import BrokerError

TICK_SECONDS = 15
PRUNE_EVERY = dt.timedelta(hours=6)
# How often a backfill cycle runs. Deliberately unhurried: it competes with
# the user's own charts for the same 200 req/min free tier, and the history
# it chases is not going anywhere. Each cycle does a bounded slice and the
# next one resumes from coverage, so "slow" costs completeness nothing.
BACKFILL_EVERY = dt.timedelta(minutes=5)
# Chunks per symbol per cycle. Small on purpose — five symbols x 4 chunks x
# ~1.5s is under a minute of API time per cycle.
BACKFILL_CHUNKS_PER_CYCLE = 4
# Chain days per symbol per cycle. Much smaller than the bars cap: each day
# is its own request to a free, unauthenticated provider, paced at
# onclick.MIN_DELAY (5s) between calls.
CHAIN_DAYS_PER_CYCLE = 3

TIMEFRAMES = {"1Min": 60, "5Min": 300, "15Min": 900, "1Hour": 3600, "1Day": 86400}
CHAIN_INTERVALS = (60, 300, 900, 3600, 86400)
MIN_INTERVAL = 60

CredsProvider = Callable[[int], dict[str, str] | None]
#: user_id -> that user's settings. Injected, so this module never imports
#: the settings store or opens app.db itself.
SettingsProvider = Callable[[int], dict[str, Any]]


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


def _backfill_window(years: str, now: dt.datetime) -> tuple[dt.date, dt.date]:
    """How far back to try, and up to when.

    The end is YESTERDAY: today's bar is the live collector's job and is not
    final until the close, so a backfill claiming it would settle a day whose
    data is still moving. 'max' is 2016 because that is where Alpaca's free
    equity history begins — reaching further would manufacture years of
    `failed` periods that can never become `have`.
    """
    end = now.date() - dt.timedelta(days=1)
    if years == "max":
        return dt.date(2016, 1, 1), end
    try:
        n = max(1, int(years))
    except ValueError:
        n = 2
    return end - dt.timedelta(days=365 * n), end


class Recorder:
    def __init__(self, con: sqlite3.Connection, creds_provider: CredsProvider,
                 settings_provider: SettingsProvider | None = None) -> None:
        self._con = con                      # owned by the recorder thread
        self._creds = creds_provider
        # INJECTED like the credentials, for the same reason: settings live in
        # app.db and this class must not know that. The unattended process in
        # particular has to stay clear of anything vault-shaped.
        self._settings = settings_provider or (lambda _uid: {})
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_prune = _utcnow() - PRUNE_EVERY
        self._last_backfill = _utcnow() - BACKFILL_EVERY

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
                # BACKFILL AFTER the due jobs, always. Live collection is the
                # thing the user is waiting on; history is not, and a
                # backfill that delayed today's chain snapshot would be
                # spending the present to buy the past.
                if _utcnow() - self._last_backfill > BACKFILL_EVERY:
                    self.run_backfill()
                    self._last_backfill = _utcnow()
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
    def run_backfill(self, now: dt.datetime | None = None) -> dict[str, Any]:
        """Fill in the history the coverage table says is missing.

        Off unless the user turned it on. Bounded per cycle, paced between
        requests, and resumable for free — the plan is recomputed from
        coverage every time, so being interrupted costs nothing and the next
        cycle simply picks up what is still unsettled.

        Bars come from Alpaca. CHAINS come from OnclickMedia, because
        Alpaca sells no historical option snapshots at all — asking it would
        just manufacture `failed` rows for data that is not purchasable.
        OnclickMedia's window is a rolling ~180 days, so that is the honest
        reach for chain history and anything older is reported rather than
        retried.
        """
        now = now or _utcnow()
        # NO EARLY RETURN on an empty bars list. The chain pass below is a
        # separate provider with its own switch, and gating it on the
        # presence of a BARS job meant a user recording only chains got no
        # chain history at all — silently, because nothing errors.
        jobs = [dict(j) for j in self._con.execute(
            "SELECT * FROM record_jobs WHERE enabled=1 AND kind='bars'").fetchall()]

        # One settings read per cycle, keyed by the job's own user.
        by_user: dict[int, dict[str, Any]] = {}
        done = 0
        results: list[dict[str, Any]] = []
        for job in jobs:
            uid = int(job["user_id"])
            if uid not in by_user:
                try:
                    by_user[uid] = self._settings(uid) or {}
                except Exception:  # noqa: BLE001 — a settings read must not kill the loop
                    LOG.exception("backfill: settings unavailable for user %s", uid)
                    by_user[uid] = {}
            cfg = by_user[uid]
            if not cfg.get("backfill_enabled", False):
                continue
            creds = self._creds(uid)
            if creds is None:
                continue

            symbol, timeframe = job["symbol"], job["timeframe"]
            provider = "alpaca-iex"
            # TELL COVERAGE WHAT IS ALREADY HERE first. Coverage starts empty
            # while the live recorder has been collecting for weeks; without
            # this the first cycle would re-fetch every day already on disk.
            coverage.reconcile_bars(self._con, provider, symbol, timeframe)

            start, end = _backfill_window(str(cfg.get("backfill_years", "2")), now)
            chunks = backfill.plan(self._con, provider, "bars", symbol, timeframe,
                                   start, end,
                                   max_chunks=BACKFILL_CHUNKS_PER_CYCLE)
            if not chunks:
                continue
            client = AlpacaData(creds["key_id"], creds["secret_key"])

            def fetch(sym: str, tf: str, s_: dt.date, e_: dt.date) -> list[dict[str, Any]]:
                bars = client.stock_bars_range(
                    sym, tf, f"{s_.isoformat()}T00:00:00Z", f"{e_.isoformat()}T23:59:59Z")
                # STORE THEM, INSIDE THE FETCH, BEFORE COVERAGE IS WRITTEN.
                # run_one() marks a day `have` from what came back — and
                # `have` is a settled state that is never asked for again. So
                # rows that were fetched but not persisted would mark the day
                # permanently done while the database stayed empty, and no
                # later run could ever repair it. Raising here instead leaves
                # the days `failed`, which retries.
                if bars:
                    with self._con:
                        self._con.executemany(
                            "INSERT OR REPLACE INTO rec_bars (symbol, timeframe,"
                            " ts, open, high, low, close, volume)"
                            " VALUES (?,?,?,?,?,?,?,?)",
                            [(sym, tf, b["ts"], b["open"], b["high"], b["low"],
                              b["close"], b["volume"]) for b in bars])
                return bars

            r = backfill.run(self._con, chunks, fetch,
                             should_stop=lambda: self._stop.is_set())
            # The rows themselves still have to land, not just the coverage
            # claim — a backfill that recorded "have" without storing the bar
            # would poison the table permanently.
            for res in r["results"]:
                done += 1
                results.append(res)
            LOG.info("backfill %s %s: %d/%d chunks, %d day(s) settled",
                     symbol, timeframe, r["done"], r["planned"],
                     sum(x.get("have", 0) + x.get("absent", 0) for x in r["results"]))
            if self._stop.is_set():
                break

        # ---- CHAINS, from OnclickMedia -------------------------------------
        for job in [dict(j) for j in self._con.execute(
                "SELECT * FROM record_jobs WHERE enabled=1 AND kind='chain'"
        ).fetchall()]:
            if self._stop.is_set():
                break
            uid = int(job["user_id"])
            cfg = by_user.get(uid)
            if cfg is None:
                try:
                    cfg = by_user[uid] = self._settings(uid) or {}
                except Exception:  # noqa: BLE001
                    cfg = by_user[uid] = {}
            if not (cfg.get("backfill_enabled", False)
                    and cfg.get("onclick_chain_backfill", False)):
                continue
            done += self._backfill_chains(job["symbol"], now)
        return {"ran": done, "results": results}

    def _backfill_chains(self, symbol: str, now: dt.datetime) -> int:
        """One symbol's chain history, from OnclickMedia, a day at a time.

        Not routed through backfill.run_one: that maps a row list onto days
        by their `ts`, and a chain day is one CSV blob for a whole session,
        not a row per day. Pretending otherwise would be a worse fit than
        stating the difference.

        The window is recomputed HERE, at execution — it slides a day per
        day, so anything decided earlier is already stale.
        """
        provider = "onclick"
        lo, hi = onclick.window(now.date())
        gaps = coverage.gaps(self._con, provider, "chain", symbol, "", lo, hi,
                             limit=CHAIN_DAYS_PER_CYCLE)
        if not gaps:
            return 0
        con = None
        done = 0
        try:
            for iso in gaps:
                if self._stop.is_set():
                    break
                try:
                    body = onclick.fetch_day(symbol, iso)
                except PermissionError as e:
                    # The date fell outside the plan's range. The provider
                    # cannot answer — that is not a claim about the market,
                    # so it stays retryable rather than becoming `absent`.
                    coverage.mark(self._con, provider, "chain", symbol, "", iso,
                                  "unknown", detail=str(e)[:200])
                    done += 1
                    continue
                except Exception as e:  # noqa: BLE001 — transient; retry later
                    coverage.mark(self._con, provider, "chain", symbol, "", iso,
                                  "failed", detail=str(e)[:200])
                    done += 1
                    continue

                if body is onclick.MISMATCH or body == onclick.MISMATCH:
                    # An open session has no greeks. Absorbing those rows
                    # would put greek-less records into a history nothing can
                    # rebuild, so the day is left RETRYABLE and picked up once
                    # it has settled.
                    coverage.mark(self._con, provider, "chain", symbol, "", iso,
                                  "failed",
                                  detail="columns differ (open session, no greeks)")
                elif body:
                    parsed = chainimport.parse_text(
                        body, "option_chain", "csv", "onclickmedia")
                    if con is None:
                        con = btdata.connect_data(btdata.data_db_path(symbol))
                    res = btdata.import_chain(con, symbol, parsed.chain, "onclickmedia")
                    # STORED FIRST, then claimed — the same ordering the bars
                    # path needs and for the same reason: `have` is settled
                    # forever, so it must never outrun the data.
                    coverage.mark(self._con, provider, "chain", symbol, "", iso,
                                  "have", rows=int(res.get("contracts", 0)))
                else:
                    # Empty. A holiday, or a ticker it does not carry — the
                    # two are identical on the wire, so mark() decides using
                    # whether this provider has ever answered for THIS symbol
                    # (coverage.EARNS_AUTHORITY), and downgrades to retryable
                    # when it has not.
                    coverage.mark(self._con, provider, "chain", symbol, "", iso,
                                  "absent", detail="empty response")
                done += 1
                # PACED. The provider is free and unauthenticated; hammering
                # it is both rude and the fastest way to lose access.
                if not self._stop.wait(onclick.MIN_DELAY):
                    continue
                break
        finally:
            if con is not None:
                con.close()
        LOG.info("chain backfill %s: %d day(s) settled", symbol, done)
        return done

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
