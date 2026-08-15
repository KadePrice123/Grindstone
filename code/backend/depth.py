"""Automatic deep history: keep every symbol the user opens, as far back as
the sources go, and on a later visit fetch only what is missing.

Kade's ask (2026-08-15): "I should have it set to save every symbols data as
far back as possible if I ever load it. if I dont load the symbol for a few
days or weeks and load again it should gather everything I dont have."

THE SHAPE
  - `bar_hist` (marketdb) is the permanent store. It has no retention sweep,
    it is never read by the backtest engine, and every row names its feed.
  - A load asks `bar_hist_span` what is already held, then fills TWO gaps:
      tail  everything after the newest stored bar (the days since last visit)
      head  everything older than the oldest stored bar, until the deepest
            source for that instrument runs out — then `deep_at` is stamped
            and the head is never walked again.
  - Providers are tried in an order chosen PER INSTRUMENT CLASS, because the
    right answer differs: an equity's recent bars are best from the broker
    and its 1990s from the keyless feed; an index or a future has no broker
    bars at all here (TastyTrade sells no OHLC history — brokers/tastytrade.py
    documents this), so the keyless feed is the only source and saying so is
    the honest answer.

WHY IT IS NOT ON THE REQUEST PATH
  This sidecar is single-process. REQUIREMENTS.md 4.3 records what that costs
  when it is forgotten: an eager heavy import froze sign-in for ~10s, and the
  deferred version blew three chart requests past their deadline and tripped a
  provider's breaker for five minutes. So a load NEVER waits for a fill — it
  returns whatever is already stored and schedules the rest on a worker, one
  symbol at a time, with the newest work first.
"""
from __future__ import annotations

import datetime as dt
import queue
import threading
from typing import Any, Callable

from . import marketdb
from .brokers.base import BrokerError
from .logs import LOG

try:  # keyless deep provider; absence must not break the app
    from .providers.yahoo import YahooProvider
except ImportError:  # pragma: no cover
    YahooProvider = None  # type: ignore[assignment]

#: Only daily bars are kept forever. Intraday history is enormous, the
#: keyless feed does not serve it (yahoo.py), and nothing in the product asks
#: for years of 1-minute data — the recorder's own jobs cover that case.
DEEP_TIMEFRAME = "1Day"

#: How long a tail check stays good. Re-opening a chart is not new data; a
#: session's worth of glances should cost one look, not one per glance.
TAIL_COOLDOWN = 900.0

#: Per class, deepest-capable source LAST — the walk stops at the first one
#: that cannot go further back, and the label records which reached where.
#: 'alpaca' is only meaningful for equities: its floor is 2016 and it carries
#: no index or futures bars at all (market.py).
PROVIDER_ORDER: dict[str, tuple[str, ...]] = {
    "us_equity": ("alpaca", "yahoo"),
    "index": ("yahoo",),
    "future": ("yahoo",),
    "crypto": (),
}


def _today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def providers_for(asset_class: str | None) -> tuple[str, ...]:
    return PROVIDER_ORDER.get(asset_class or "us_equity", ("alpaca", "yahoo"))


def plan(span: dict[str, Any], asset_class: str | None,
         now: dt.date | None = None) -> dict[str, Any]:
    """What a visit still needs. Pure, so the gate can table-test it.

    Returns {"head": bool, "tail_from": str|None, "reason": str}. `head` is
    False once `deep_at` is stamped — the deepest source has already been
    walked to its horizon and asking again would re-download the same decade
    on every chart open, which is exactly the waste this module replaces.
    """
    now = now or _today()
    if not providers_for(asset_class):
        return {"head": False, "tail_from": None,
                "reason": f"no bar source for {asset_class}"}
    if not span.get("n"):
        return {"head": True, "tail_from": None, "reason": "nothing stored yet"}
    head = not span.get("deep_at")
    hi = (span.get("hi") or "")[:10]
    tail_from = None
    if hi:
        try:
            last = dt.date.fromisoformat(hi)
        except ValueError:
            last = None
        # A weekend visit is not a gap. Only ask when a session could have
        # closed since the newest stored bar.
        if last is not None and (now - last).days >= 1:
            tail_from = (last + dt.timedelta(days=1)).isoformat()
    bits = []
    if head:
        bits.append("head not yet at horizon")
    if tail_from:
        bits.append(f"tail from {tail_from}")
    return {"head": head, "tail_from": tail_from,
            "reason": ", ".join(bits) or "up to date"}


def fetch_deep_daily(symbol: str) -> list[dict[str, Any]]:
    """Everything the keyless feed has for this symbol, oldest first. The
    epoch form is mandatory — `range=max` silently returns MONTHLY bars
    (measured, providers/yahoo.py) — and daily_bars already uses it."""
    if YahooProvider is None:
        return []
    try:
        return YahooProvider().daily_bars(symbol, period="max") or []
    except Exception:  # noqa: BLE001 — depth is a bonus, never a failure
        LOG.info("deep: yahoo failed for %s", symbol, exc_info=True)
        return []


class Deepener:
    """One worker, one symbol at a time. Serial ON PURPOSE: the providers are
    rate-limited (Alpaca 200/min shared with every chart the user is looking
    at; the keyless feed has its own throttle and a 3-failure breaker), and
    opening twenty favourites must not become twenty concurrent backfills.
    """

    def __init__(self, market_con: Callable[[], Any],
                 creds_for: Callable[[int], dict[str, str] | None],
                 classify: Callable[[str], str | None]) -> None:
        self._market = market_con
        self._creds = creds_for
        self._classify = classify
        self._q: queue.Queue[tuple[int, str]] = queue.Queue()
        self._seen: set[str] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.last: dict[str, str] = {}

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="deepener")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def submit(self, user_id: int, symbol: str) -> bool:
        """Queue a symbol. Deduplicated while pending, so re-opening a chart
        five times in a minute schedules one fill, not five."""
        sym = symbol.upper().strip()
        if not sym:
            return False
        with self._lock:
            if sym in self._seen:
                return False
            self._seen.add(sym)
        self._q.put((user_id, sym))
        self.start()
        return True

    def pending(self) -> int:
        return self._q.qsize()

    # ------------------------------------------------------------------ work
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                user_id, sym = self._q.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self.fill(user_id, sym)
            except Exception:  # noqa: BLE001 — one bad symbol never kills it
                LOG.exception("deep: fill failed for %s", sym)
            finally:
                with self._lock:
                    self._seen.discard(sym)

    def fill(self, user_id: int, symbol: str) -> dict[str, Any]:
        """Bring one symbol's permanent daily history up to date. Stores what
        it fetched BEFORE claiming progress, so an interrupted fill leaves
        real rows and an unstamped marker rather than the reverse."""
        sym = symbol.upper().strip()
        con = self._market()
        try:
            span = marketdb.bar_hist_span(con, sym, DEEP_TIMEFRAME)
            todo = plan(span, self._classify(sym))
            wrote = 0
            now = dt.datetime.now(dt.timezone.utc)

            # ONE keyless fetch per fill, at most. It returns the WHOLE series
            # — head and tail together — so fetching it again for the tail
            # would double both the request and the write on every first
            # visit (measured: 8,940 writes for 4,470 distinct SPXL bars).
            deep: list[dict[str, Any]] | None = None

            def deep_series() -> list[dict[str, Any]]:
                nonlocal deep
                if deep is None:
                    deep = fetch_deep_daily(sym)
                return deep

            if todo["head"]:
                lo = (span.get("lo") or "")[:10]
                older = [b for b in deep_series() if not lo or b["ts"][:10] < lo]
                if older:
                    wrote += marketdb.bar_hist_store(
                        con, sym, DEEP_TIMEFRAME, older, "yahoo (delayed)")
                if deep_series():
                    # The horizon was reached: this feed has no more to give,
                    # so never walk the head again for this symbol.
                    marketdb.bar_hist_mark(
                        con, sym, DEEP_TIMEFRAME,
                        deep_at=now.isoformat(timespec="seconds"),
                        note=f"deepest {deep_series()[0]['ts'][:10]}"
                             " via yahoo (delayed)")
                # RE-READ before deciding the tail. The head pass just wrote a
                # full series, so the span captured above is stale by exactly
                # the rows we care about — deciding from it made a first visit
                # store everything twice (8,940 writes for 4,470 SPXL bars).
                span = marketdb.bar_hist_span(con, sym, DEEP_TIMEFRAME)
                todo = plan(span, self._classify(sym), now.date())

            # A re-open minutes later must not re-poll: `tail_at` records the
            # last look, and a chart reopened five times in an hour is one
            # user glancing, not five days of new bars.
            fresh = False
            if span.get("tail_at"):
                try:
                    fresh = (now - dt.datetime.fromisoformat(
                        span["tail_at"])).total_seconds() < TAIL_COOLDOWN
                except ValueError:
                    fresh = False

            if (todo["tail_from"] or not span.get("n")) and not fresh:
                creds = self._creds(user_id)
                got: list[dict[str, Any]] = []
                source = ""
                if creds and (self._classify(sym) or "us_equity") == "us_equity":
                    from .brokers.alpaca_data import AlpacaData
                    start = (todo["tail_from"] or "2016-01-01") + "T00:00:00Z"
                    try:
                        got = AlpacaData(creds["key_id"], creds["secret_key"]
                                         ).stock_bars(sym, DEEP_TIMEFRAME,
                                                      start=start, limit=10000)
                        source = "alpaca (IEX)"
                    except BrokerError as e:
                        LOG.info("deep: alpaca tail failed for %s: %s", sym, e)
                if not got:
                    # No broker, or a class it does not carry: whatever the
                    # keyless feed already returned covers the tail too.
                    hi = (span.get("hi") or "")[:10]
                    got = [b for b in deep_series()
                           if not hi or b["ts"][:10] > hi]
                    source = "yahoo (delayed)"
                if got:
                    wrote += marketdb.bar_hist_store(
                        con, sym, DEEP_TIMEFRAME, got, source)
                marketdb.bar_hist_mark(con, sym, DEEP_TIMEFRAME,
                                       tail_at=now.isoformat(timespec="seconds"))

            after = marketdb.bar_hist_span(con, sym, DEEP_TIMEFRAME)
            msg = (f"{after['n']} bars {(after['lo'] or '?')[:10]}.."
                   f"{(after['hi'] or '?')[:10]} (+{wrote})")
            self.last[sym] = msg
            LOG.info("deep: %s %s", sym, msg)
            return {"symbol": sym, "wrote": wrote, "span": after}
        finally:
            con.close()
