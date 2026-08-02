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

import threading
import time
from typing import Any

_MIN_INTERVAL = 1.1  # seconds between Yahoo calls, community-safe zone
_lock = threading.Lock()
_last_call = 0.0


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
        _throttle()
        t = self._ticker(symbol)
        try:
            fi = t.fast_info
            price = fi.last_price
            prev = fi.previous_close
        except Exception:  # noqa: BLE001 — yfinance raises a zoo of types
            return None
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
        _throttle()
        t = self._ticker(symbol)
        try:
            df = t.history(period=period, interval="1d", repair=False,
                           auto_adjust=True)
        except Exception:  # noqa: BLE001
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
