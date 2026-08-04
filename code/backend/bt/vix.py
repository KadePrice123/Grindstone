"""A VIX series for the backtester.

No volatility index ships with the databases, so the engine synthesises one
from the SPY chain using the CBOE VIX white-paper method (a 30-day
constant-maturity variance swap rate built from out-of-the-money quotes), then
applies an affine calibration so the level matches the real index.

An external CSV of true VIX closes can be supplied instead and always wins; the
proxy exists so the engine is self-contained.
"""
from __future__ import annotations

import csv
import datetime as dt
import math
import os
import sqlite3

from .data import CALL, PUT, MarketData, from_ymd, to_ymd

# Fitted against the 34 "reached minimum exit VIX" events in the tastytrade
# calibration set plus known index closes; see calibrate_vix() to refit.
DEFAULT_CALIBRATION = (0.0, 1.0)

_MIN_DTE, _MAX_DTE = 5, 60
_YEAR = 365.0


def _variance(strikes: list[float], quotes: dict[float, float], F: float, T: float, r: float = 0.0) -> float:
    """CBOE model-free implied variance for one expiration."""
    if not strikes or T <= 0:
        return float("nan")
    k0 = max((k for k in strikes if k <= F), default=strikes[0])
    total = 0.0
    for i, k in enumerate(strikes):
        if i == 0:
            dk = strikes[1] - k if len(strikes) > 1 else k * 0.01
        elif i == len(strikes) - 1:
            dk = k - strikes[i - 1]
        else:
            dk = 0.5 * (strikes[i + 1] - strikes[i - 1])
        q = quotes.get(k, 0.0)
        if q <= 0:
            continue
        total += (dk / (k * k)) * math.exp(r * T) * q
    return (2.0 / T) * total - (1.0 / T) * (F / k0 - 1.0) ** 2


def _otm_quotes(exp, F):
    """OTM mid quotes keyed by strike, truncated at two consecutive zero bids
    on each wing exactly as the CBOE method prescribes."""
    ks_p = sorted(k for k in exp.puts if k < F)
    ks_c = sorted(k for k in exp.calls if k > F)
    out: dict[float, float] = {}

    zeros = 0
    for k in reversed(ks_p):                      # walk down from the money
        c = exp.puts[k]
        if c.bid <= 0:
            zeros += 1
            if zeros >= 2:
                break
            continue
        zeros = 0
        out[k] = 0.5 * (c.bid + c.ask)
    zeros = 0
    for k in ks_c:                                # walk up from the money
        c = exp.calls[k]
        if c.bid <= 0:
            zeros += 1
            if zeros >= 2:
                break
            continue
        zeros = 0
        out[k] = 0.5 * (c.bid + c.ask)

    # The at-the-money strike contributes the average of its call and put.
    k0 = max((k for k in exp.puts if k <= F), default=None)
    if k0 is not None and k0 in exp.calls and k0 in exp.puts:
        p, c = exp.puts[k0], exp.calls[k0]
        if p.bid > 0 and c.bid > 0:
            out[k0] = 0.25 * (p.bid + p.ask + c.bid + c.ask)
    return out


def raw_proxy(chain) -> float:
    """Uncalibrated 30-day constant-maturity implied volatility, in points."""
    if chain is None:
        return float("nan")
    near = next_ = None
    for e in chain.sorted_expiries():
        if not (_MIN_DTE <= e.dte <= _MAX_DTE):
            continue
        if e.dte <= 30:
            near = e                              # last one at or under 30 days
        elif next_ is None:
            next_ = e
    if near is None or next_ is None:
        cands = [e for e in chain.sorted_expiries() if _MIN_DTE <= e.dte <= _MAX_DTE]
        if len(cands) < 2:
            return float("nan")
        near, next_ = cands[0], cands[-1]

    out = []
    for e in (near, next_):
        q = _otm_quotes(e, e.forward)
        if len(q) < 5:
            return float("nan")
        T = e.dte / _YEAR
        var = _variance(sorted(q), q, e.forward, T)
        if not math.isfinite(var) or var <= 0:
            return float("nan")
        out.append((T, var, e.dte))

    (t1, v1, d1), (t2, v2, d2) = out
    if d1 == d2:
        return 100.0 * math.sqrt(v1)
    w = (d2 - 30.0) / (d2 - d1)
    w = min(max(w, -1.0), 2.0)                   # allow mild extrapolation only
    blended = t1 * v1 * w + t2 * v2 * (1.0 - w)
    if blended <= 0:
        return float("nan")
    return 100.0 * math.sqrt(blended * (_YEAR / 30.0))


class VixSeries:
    """Date -> VIX level, from a CSV if given, otherwise the chain-derived proxy."""

    def __init__(self, md: MarketData, csv_path: str | None = None,
                 calibration: tuple[float, float] = DEFAULT_CALIBRATION,
                 cache_db: str | None = None):
        self.md = md
        self.a, self.b = calibration
        self._csv: dict[dt.date, float] = {}
        self._cache: dict[dt.date, float] = {}
        self._store = None
        if csv_path and os.path.exists(csv_path):
            self._load_csv(csv_path)
        if cache_db:
            self._open_store(cache_db)

    def _load_csv(self, path: str) -> None:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                key = next((k for k in row if k and k.strip().lower() in ("date", "day")), None)
                val = next((k for k in row if k and k.strip().lower() in
                            ("close", "vix", "vix close", "vixclose", "adj close")), None)
                if not key or not val:
                    continue
                raw = (row[key] or "").strip()
                for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y%m%d"):
                    try:
                        d = dt.datetime.strptime(raw, fmt).date()
                        break
                    except ValueError:
                        d = None
                if d is None:
                    continue
                try:
                    self._csv[d] = float(row[val])
                except (TypeError, ValueError):
                    pass

    def _open_store(self, path: str) -> None:
        self._store = sqlite3.connect(path, check_same_thread=False)
        self._store.execute("CREATE TABLE IF NOT EXISTS vix_proxy (d INTEGER PRIMARY KEY, v REAL)")
        self._store.commit()
        for d, v in self._store.execute("SELECT d, v FROM vix_proxy"):
            self._cache[from_ymd(d)] = v

    def __call__(self, date: dt.date) -> float:
        return self.get(date)

    def get(self, date: dt.date) -> float:
        v = self._csv.get(date)
        if v is not None:
            return v
        v = self._cache.get(date)
        if v is None:
            v = raw_proxy(self.md.chain(date))
            self._cache[date] = v
            if self._store is not None and math.isfinite(v):
                self._store.execute("INSERT OR REPLACE INTO vix_proxy VALUES (?,?)", (to_ymd(date), v))
        if not math.isfinite(v):
            return float("nan")
        return self.a + self.b * v

    def flush(self) -> None:
        if self._store is not None:
            self._store.commit()

    def precompute(self, dates, progress=None) -> None:
        for i, d in enumerate(dates):
            self.get(d)
            if progress and i % 200 == 0:
                progress(i, len(dates))
        self.flush()
