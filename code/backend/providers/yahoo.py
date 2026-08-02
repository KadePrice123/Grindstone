"""Keyless Yahoo Finance fallback (research-verified 2026-08-02).

Rules of engagement, from the research and Yahoo's terms:
- PERSONAL USE ONLY: this provider exists so a user without a data API still
  sees quotes; data is delayed, labeled, never used to price orders, never
  redistributed.
- yfinance is pinned >=1.5.2,<2.0 with curl_cffi as a coupled pair — they
  have broken each other twice; upgrade both together and re-run the gate.
- The nospam caching extra is broken with the curl_cffi backend (yfinance
  #2913), so throttling is OUR job: a module token-bucket at ~1 req/s.
- repair=True is NOT enabled (open bug: it can rescale a price table 100x).
"""
from __future__ import annotations

import concurrent.futures
import threading
import time
from typing import Any

_MIN_INTERVAL = 1.1  # seconds between Yahoo calls, community-safe zone
_lock = threading.Lock()
_last_call = 0.0

# A fallback that hangs is worse than a fallback that fails: an unbounded
# yfinance call stalled a chart request indefinitely in testing.
#
# Two layers, because the first alone was not enough (also caught in testing):
#  1. every call runs on a worker with a hard deadline;
#  2. a CIRCUIT BREAKER, because a timeout does NOT cancel the underlying
#     thread — stuck threads accumulate, exhaust the pool, and then even
#     "bounded" calls queue forever. After repeated failures the provider
#     reports unavailable immediately until a cooldown expires.
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=6, thread_name_prefix="yahoo")
CALL_TIMEOUT = 10.0
BREAKER_THRESHOLD = 2
BREAKER_COOLDOWN = 300.0

_breaker_lock = threading.Lock()
_consecutive_failures = 0
_open_until = 0.0


def available() -> bool:
    with _breaker_lock:
        return time.monotonic() >= _open_until


def _record(success: bool) -> None:
    global _consecutive_failures, _open_until
    with _breaker_lock:
        if success:
            _consecutive_failures = 0
            _open_until = 0.0
        else:
            _consecutive_failures += 1
            if _consecutive_failures >= BREAKER_THRESHOLD:
                _open_until = time.monotonic() + BREAKER_COOLDOWN


def _bounded(fn, *args, **kwargs):
    """Run fn with a hard timeout; None on timeout/failure/open breaker."""
    if not available():
        return None
    try:
        out = _pool.submit(fn, *args, **kwargs).result(timeout=CALL_TIMEOUT)
    except Exception:  # noqa: BLE001 — includes TimeoutError; callers degrade
        _record(False)
        return None
    _record(True)
    return out


def _throttle() -> None:
    global _last_call
    with _lock:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


class YahooProvider:
    """Lazy-imports yfinance so the sidecar's startup cost is unaffected."""

    def _ticker(self, symbol: str):
        import yfinance as yf  # deferred: ~1s import

        return yf.Ticker(self._map(symbol))

    @staticmethod
    def _map(symbol: str) -> str:
        # Platform symbology -> Yahoo symbology for the indices we list.
        return {"SPX": "^GSPC", "NDX": "^NDX", "VIX": "^VIX", "XSP": "^XSP"}.get(
            symbol.upper(), symbol.upper()
        )

    def quote(self, symbol: str) -> dict[str, Any] | None:
        def work():
            _throttle()
            fi = self._ticker(symbol).fast_info
            return fi.last_price, fi.previous_close

        got = _bounded(work)
        if got is None:
            return None
        price, prev = got
        if price is None:
            return None
        change = pct = None
        if prev:
            change = price - prev
            pct = 100.0 * change / prev
        return {
            "symbol": symbol.upper(),
            "price": float(price),
            "prev_close": float(prev) if prev else None,
            "change": change,
            "change_pct": pct,
            "bid": None, "ask": None, "ts": None,
            "day_open": None, "day_high": None, "day_low": None,
            "day_volume": None,
        }

    def daily_bars(self, symbol: str, period: str = "1y") -> list[dict[str, Any]]:
        def work():
            _throttle()
            return self._ticker(symbol).history(period=period, interval="1d",
                                                repair=False, auto_adjust=True)

        df = _bounded(work)
        if df is None or getattr(df, "empty", True):
            return []
        out = []
        for ts, row in df.iterrows():
            # sanity per research: drop obviously-broken rows
            if row.get("Close") is None or row["Close"] <= 0:
                continue
            out.append({
                "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "open": float(row["Open"]), "high": float(row["High"]),
                "low": float(row["Low"]), "close": float(row["Close"]),
                "volume": float(row.get("Volume") or 0),
            })
        return out
