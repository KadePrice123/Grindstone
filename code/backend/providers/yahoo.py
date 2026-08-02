"""Keyless Yahoo Finance fallback (rewritten 2026-08-02, measured).

This provider exists so a user with no data API still sees a real quote and a
real chart. Data is delayed, always labeled, never used to price an order,
never redistributed.

WHY THIS IS PLAIN HTTP AND NOT yfinance
---------------------------------------
It used to call yfinance. Importing yfinance drags in pandas and costs ~8s in
this environment, and it HOLDS THE GIL for most of that — which in a
single-process sidecar means the whole backend stops answering. That is not a
theory: it froze login once when it ran at boot (see main.py), and on
2026-08-02 the e2e caught the same import blowing three concurrent chart
requests past their deadline and tripping the circuit breaker, so a fresh
install reported "no data source available" for every symbol for five minutes
while working perfectly from a shell. Raising the deadline only converted a
wrong answer into a 90-second hang.

One documented JSON endpoint replaces all of it. It answers in ~0.6s, needs
no API key, no cookie and no crumb, and carries the quote metadata AND the
OHLC series in the same response — so a chart costs one request, not two.
No heavy import means no GIL freeze and no fragile yfinance/curl_cffi version
pair to keep in step.
"""
from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from typing import Any

import httpx

LOG = logging.getLogger("grindstone.yahoo")

_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

# Yahoo serves a degraded page to obviously-automated clients. A normal
# browser UA is the difference between 200 and 429.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

_MIN_INTERVAL = 0.35  # seconds between calls; polite, and far below any limit
_lock = threading.Lock()
_last_call = 0.0

# A fallback that hangs is worse than a fallback that fails, so every call
# runs on a worker with a hard deadline, behind a circuit breaker: a timeout
# does NOT cancel the underlying thread, and stuck threads would otherwise
# accumulate until even "bounded" calls queue forever.
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=6, thread_name_prefix="yahoo")
CALL_TIMEOUT = 12.0
BREAKER_THRESHOLD = 3
BREAKER_COOLDOWN = 120.0

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
                LOG.warning("yahoo breaker opened for %.0fs after %d failures",
                            BREAKER_COOLDOWN, _consecutive_failures)


def _bounded(fn, *args, **kwargs):
    """Run fn with a hard timeout; None on timeout/failure/open breaker.

    Every exit path is LOGGED. This used to fail in total silence, so a
    fallback that worked from a shell but not inside the app presented to the
    user as a flat "no data source available" with nothing to debug from.
    """
    if not available():
        LOG.info("yahoo call skipped: breaker open for another %.0fs",
                 _open_until - time.monotonic())
        return None
    try:
        out = _pool.submit(fn, *args, **kwargs).result(timeout=CALL_TIMEOUT)
    except concurrent.futures.TimeoutError:
        _record(False)
        LOG.warning("yahoo call exceeded %.0fs", CALL_TIMEOUT)
        return None
    except Exception as e:  # noqa: BLE001 — callers degrade, but say why
        _record(False)
        LOG.warning("yahoo call failed: %s: %s", type(e).__name__, e)
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


def _f(v: Any) -> float | None:
    """A number, or None. Yahoo pads its series with nulls on holidays and
    halts; a null must stay a null rather than become a zero price."""
    return float(v) if isinstance(v, (int, float)) else None


class YahooProvider:
    """One HTTP call answers both quote and chart. Never raises."""

    @staticmethod
    def _map(symbol: str) -> str:
        # Platform symbology -> Yahoo symbology for the indices we list.
        return {"SPX": "^GSPC", "NDX": "^NDX", "VIX": "^VIX", "XSP": "^XSP"}.get(
            symbol.upper(), symbol.upper()
        )

    def _chart(self, symbol: str, rng: str, interval: str) -> dict[str, Any] | None:
        def work():
            _throttle()
            r = httpx.get(_CHART.format(symbol=self._map(symbol)),
                          params={"range": rng, "interval": interval},
                          headers=_HEADERS, timeout=10.0, follow_redirects=True)
            r.raise_for_status()
            body = r.json()
            err = (body.get("chart") or {}).get("error")
            if err:
                raise ValueError(f"yahoo: {err}")
            results = (body.get("chart") or {}).get("result") or []
            return results[0] if results else None

        return _bounded(work)

    @staticmethod
    def _series(result: dict[str, Any]) -> list[dict[str, Any]]:
        stamps = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        opens, highs = quote.get("open") or [], quote.get("high") or []
        lows, closes = quote.get("low") or [], quote.get("close") or []
        vols = quote.get("volume") or []
        out: list[dict[str, Any]] = []
        for i, ts in enumerate(stamps):
            close = _f(closes[i]) if i < len(closes) else None
            if close is None or close <= 0:
                continue  # holiday/halt padding — drop, never zero-fill
            out.append({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
                "open": _f(opens[i]) if i < len(opens) else close,
                "high": _f(highs[i]) if i < len(highs) else close,
                "low": _f(lows[i]) if i < len(lows) else close,
                "close": close,
                "volume": _f(vols[i]) if i < len(vols) else 0.0,
            })
        return out

    def quote(self, symbol: str) -> dict[str, Any] | None:
        result = self._chart(symbol, "1mo", "1d")
        if result is None:
            return None
        meta = result.get("meta") or {}
        price = _f(meta.get("regularMarketPrice"))
        if price is None:
            return None

        bars = self._series(result)
        # Yahoo's meta carries no previousClose on a ranged request, and
        # chartPreviousClose is the close before the RANGE, not before today.
        # The daily series has the real one.
        prev = _f(meta.get("previousClose"))
        if prev is None and len(bars) >= 2:
            prev = bars[-2]["close"]
        change = pct = None
        if prev:
            change = price - prev
            pct = 100.0 * change / prev

        return {
            "symbol": symbol.upper(),
            "price": price,
            "prev_close": prev,
            "change": change,
            "change_pct": pct,
            # A delayed feed has no order book, and an invented bid/ask is the
            # one number a trading app must never make up.
            "bid": None, "ask": None,
            "ts": (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(meta["regularMarketTime"]))
                   if isinstance(meta.get("regularMarketTime"), int) else None),
            "day_open": bars[-1]["open"] if bars else None,
            "day_high": _f(meta.get("regularMarketDayHigh")),
            "day_low": _f(meta.get("regularMarketDayLow")),
            "day_volume": _f(meta.get("regularMarketVolume")),
            "year_high": _f(meta.get("fiftyTwoWeekHigh")),
            "year_low": _f(meta.get("fiftyTwoWeekLow")),
            "avg_volume": None,
            "market_cap": None,
        }

    def daily_bars(self, symbol: str, period: str = "1y") -> list[dict[str, Any]]:
        result = self._chart(symbol, period, "1d")
        return self._series(result) if result else []
