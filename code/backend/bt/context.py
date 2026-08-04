"""Daily market state exposed to strategy rules.

Everything in `MARKET_VARS` is produced here.  The series are computed once for
the whole backtest window, so a rule referencing `rsi_14` or `iv_rank` costs a
dictionary lookup rather than a recomputation.
"""
from __future__ import annotations

import datetime as dt
import math

import numpy as np

from .data import MarketData
from .vix import VixSeries

_NAN = float("nan")


def _rolling_mean(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(x.shape, _NAN)
    if len(x) < n:
        return out
    c = np.cumsum(np.insert(np.nan_to_num(x, nan=0.0), 0, 0.0))
    valid = np.cumsum(np.insert((~np.isnan(x)).astype(float), 0, 0.0))
    s = c[n:] - c[:-n]
    v = valid[n:] - valid[:-n]
    out[n - 1:] = np.where(v == n, s / n, _NAN)
    return out


def _ema(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(x.shape, _NAN)
    k = 2.0 / (n + 1.0)
    prev = _NAN
    for i, v in enumerate(x):
        if not math.isfinite(v):
            continue
        prev = v if not math.isfinite(prev) else prev + k * (v - prev)
        if i >= n - 1:
            out[i] = prev
    return out


def _rsi(x: np.ndarray, n: int = 14) -> np.ndarray:
    out = np.full(x.shape, _NAN)
    d = np.diff(x)
    if len(d) < n:
        return out
    gain = np.where(d > 0, d, 0.0)
    loss = np.where(d < 0, -d, 0.0)
    ag, al = gain[:n].mean(), loss[:n].mean()
    for i in range(n, len(d) + 1):
        if i > n:
            ag = (ag * (n - 1) + gain[i - 1]) / n
            al = (al * (n - 1) + loss[i - 1]) / n
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    return out


def _rank(x: np.ndarray, n: int) -> np.ndarray:
    """Position of today's value within its trailing `n`-day range, 0..100."""
    out = np.full(x.shape, _NAN)
    for i in range(len(x)):
        lo_i = max(0, i - n + 1)
        w = x[lo_i:i + 1]
        w = w[np.isfinite(w)]
        if len(w) < 20 or not math.isfinite(x[i]):
            continue
        lo, hi = w.min(), w.max()
        out[i] = 100.0 * (x[i] - lo) / (hi - lo) if hi > lo else 50.0
    return out


def _percentile(x: np.ndarray, n: int) -> np.ndarray:
    """Share of the trailing `n` days spent below today's value, 0..100."""
    out = np.full(x.shape, _NAN)
    for i in range(len(x)):
        lo_i = max(0, i - n + 1)
        w = x[lo_i:i + 1]
        w = w[np.isfinite(w)]
        if len(w) < 20 or not math.isfinite(x[i]):
            continue
        out[i] = 100.0 * float((w < x[i]).sum()) / len(w)
    return out


class MarketContext:
    """Precomputed per-date market variables."""

    def __init__(self, md: MarketData, dates: list[dt.date], vix: VixSeries,
                 warmup: list[dt.date] | None = None, progress=None):
        self.md = md
        self.vix = vix
        # Indicators need history before the first traded day.
        self.dates = list(warmup or []) + list(dates)
        self.index = {d: i for i, d in enumerate(self.dates)}

        spot = np.array(md.spot_series(self.dates, progress=progress), dtype=float)
        self.spot = spot
        with np.errstate(invalid="ignore", divide="ignore"):
            logret = np.diff(np.log(spot), prepend=np.nan)
        self.logret = logret

        self.sma20 = _rolling_mean(spot, 20)
        self.sma50 = _rolling_mean(spot, 50)
        self.sma200 = _rolling_mean(spot, 200)
        self.rsi14 = _rsi(spot, 14)

        self.rv10 = self._realized(logret, 10)
        self.rv20 = self._realized(logret, 20)
        self.rv60 = self._realized(logret, 60)

        vx = np.array([vix.get(d) for d in self.dates], dtype=float)
        self.vixv = vx
        self.vix_rank = _rank(vx, 252)
        self.vix_pct = _percentile(vx, 252)

        self.high252 = self._roll(spot, 252, np.max)
        self.low252 = self._roll(spot, 252, np.min)
        self.atr14 = self._atr(14)

        self._iv30: np.ndarray | None = None      # solved lazily; costly
        # Arbitrary-lookback indicators, computed once per period on first use.
        self._series: dict[tuple, np.ndarray] = {}

    # ------------------------------------------------------------- builders
    @staticmethod
    def _realized(logret: np.ndarray, n: int) -> np.ndarray:
        out = np.full(logret.shape, _NAN)
        for i in range(n, len(logret)):
            w = logret[i - n + 1:i + 1]
            w = w[np.isfinite(w)]
            if len(w) >= n // 2:
                out[i] = float(np.std(w, ddof=1)) * math.sqrt(252.0) * 100.0
        return out

    @staticmethod
    def _roll(x: np.ndarray, n: int, fn) -> np.ndarray:
        out = np.full(x.shape, _NAN)
        for i in range(len(x)):
            w = x[max(0, i - n + 1):i + 1]
            w = w[np.isfinite(w)]
            if len(w):
                out[i] = fn(w)
        return out

    def _atr(self, n: int) -> np.ndarray:
        out = np.full(self.spot.shape, _NAN)
        tr = np.full(self.spot.shape, _NAN)
        for i, d in enumerate(self.dates):
            lo, hi = self.md.day_range(d)
            if math.isfinite(lo) and math.isfinite(hi):
                prev = self.spot[i - 1] if i else _NAN
                tr[i] = max(hi - lo,
                            abs(hi - prev) if math.isfinite(prev) else 0.0,
                            abs(lo - prev) if math.isfinite(prev) else 0.0)
        return _rolling_mean(tr, n) if np.isfinite(tr).any() else out

    def iv30(self, i: int) -> float:
        """30-day ATM implied vol.  Approximated by the (uncalibrated) VIX proxy,
        which is the same construction."""
        return self.vixv[i]

    # ------------------------------------------------ arbitrary-lookback set
    def series(self, kind: str, n: int) -> np.ndarray:
        """A full-history indicator series, cached per (kind, period)."""
        n = int(n)
        if n < 1:
            raise ValueError(f"{kind}() needs a period of at least 1, got {n}")
        key = (kind, n)
        hit = self._series.get(key)
        if hit is not None:
            return hit
        s = self.spot
        if kind == "rsi":
            out = _rsi(s, n)
        elif kind == "sma":
            out = _rolling_mean(s, n)
        elif kind == "ema":
            out = _ema(s, n)
        elif kind == "ret":
            out = np.full(s.shape, _NAN)
            if len(s) > n:
                with np.errstate(invalid="ignore", divide="ignore"):
                    out[n:] = 100.0 * (s[n:] / s[:-n] - 1.0)
        elif kind == "rvol":
            out = self._realized(self.logret, n)
        elif kind == "high":
            out = self._roll(s, n, np.max)
        elif kind == "low":
            out = self._roll(s, n, np.min)
        elif kind == "vix_avg":
            out = _rolling_mean(self.vixv, n)
        else:
            raise ValueError(f"unknown indicator {kind!r}")
        self._series[key] = out
        return out

    def _percentile_of(self, i: int, name: str, n: int) -> float:
        """Percentile rank of today's value of `name` over the trailing n days."""
        base = {"vix": self.vixv, "spot": self.spot, "iv30": self.vixv}.get(str(name))
        if base is None:
            try:
                kind, per = str(name).split(":", 1)
                base = self.series(kind, int(per))
            except (ValueError, KeyError):
                return _NAN
        lo = max(0, i - int(n) + 1)
        w = base[lo:i + 1]
        w = w[np.isfinite(w)]
        if len(w) < 5 or not math.isfinite(base[i]):
            return _NAN
        return 100.0 * float((w < base[i]).sum()) / len(w)

    def _callables(self, i: int) -> dict:
        def at(kind):
            def fn(n):
                v = self.series(kind, n)
                return float(v[i]) if i < len(v) else _NAN
            return fn
        out = {k: at(k) for k in ("rsi", "sma", "ema", "ret", "rvol", "high", "low", "vix_avg")}
        out["percentile_of"] = lambda name, n: self._percentile_of(i, name, n)
        return out

    # -------------------------------------------------------------- lookup
    def at(self, date: dt.date) -> dict:
        i = self.index.get(date)
        if i is None:
            return {}
        s = self.spot[i]

        def back(n):
            j = i - n
            return self.spot[j] if j >= 0 else _NAN

        def pct(n):
            p = back(n)
            return 100.0 * (s / p - 1.0) if (math.isfinite(p) and p > 0) else _NAN

        vx = self.vixv[i]
        vprev = self.vixv[i - 1] if i else _NAN
        hi = self.high252[i]
        return {
            **self._callables(i),
            "date": date, "year": date.year, "month": date.month, "day": date.day,
            "dow": date.weekday(), "dom": date.day, "week": date.isocalendar()[1],
            "is_month_start": i == 0 or self.dates[i - 1].month != date.month,
            "is_month_end": i + 1 >= len(self.dates) or self.dates[i + 1].month != date.month,
            "spot": s,
            "vix": vx, "vix_prev": vprev, "vix_chg": vx - vprev,
            "vix_rank": self.vix_rank[i], "vix_percentile": self.vix_pct[i],
            "iv30": self.iv30(i), "iv_rank": self.vix_rank[i], "iv_percentile": self.vix_pct[i],
            "term_slope": _NAN,
            "ret_1d": pct(1), "ret_5d": pct(5), "ret_20d": pct(20), "ret_60d": pct(60),
            "realized_vol_10": self.rv10[i], "realized_vol_20": self.rv20[i],
            "realized_vol_60": self.rv60[i],
            "sma_20": self.sma20[i], "sma_50": self.sma50[i], "sma_200": self.sma200[i],
            "above_sma_20": bool(s > self.sma20[i]) if math.isfinite(self.sma20[i]) else False,
            "above_sma_50": bool(s > self.sma50[i]) if math.isfinite(self.sma50[i]) else False,
            "above_sma_200": bool(s > self.sma200[i]) if math.isfinite(self.sma200[i]) else False,
            "rsi_14": self.rsi14[i], "atr_14": self.atr14[i],
            "high_252": hi, "low_252": self.low252[i],
            "drawdown_from_high": (100.0 * (s / hi - 1.0)) if (math.isfinite(hi) and hi > 0) else _NAN,
        }
