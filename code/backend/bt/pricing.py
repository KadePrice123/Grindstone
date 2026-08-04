"""Black-76 / Black-Scholes pricing, implied vol, and forward-curve extraction.

The options database stores end-of-day quotes but no reliable rate or dividend
information, and its `delta` column is sourced inconsistently.  Everything the
engine needs is therefore re-derived from the quotes themselves:

    put-call parity:  C - P = D * (F - K)

Regressing (C - P) against K across the liquid strikes of one expiration gives
the discount factor D = exp(-rT) (negative slope) and the forward F
(intercept / D).  Greeks are then computed in Black-76 forward space, which
needs no separate dividend assumption.
"""
from __future__ import annotations

import math

import numpy as np

SQRT2 = math.sqrt(2.0)


def norm_cdf(x):
    """Vectorised standard normal CDF."""
    return 0.5 * (1.0 + np.vectorize(math.erf)(np.asarray(x, dtype=float) / SQRT2))


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / SQRT2))


def _npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def black76(F: float, K: float, T: float, vol: float, is_call: bool, D: float = 1.0) -> float:
    """Undiscounted-forward Black-76 price, multiplied by discount factor D."""
    if T <= 0 or vol <= 0 or F <= 0 or K <= 0:
        intrinsic = max(F - K, 0.0) if is_call else max(K - F, 0.0)
        return D * intrinsic
    st = vol * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * st * st) / st
    d2 = d1 - st
    if is_call:
        return D * (F * _ncdf(d1) - K * _ncdf(d2))
    return D * (K * _ncdf(-d2) - F * _ncdf(-d1))


def black76_greeks(F: float, K: float, T: float, vol: float, is_call: bool, D: float = 1.0) -> dict:
    """Greeks w.r.t. the *forward*.  ``delta`` is the discounted forward delta,
    which for a non-dividend-adjusted quote equals the conventional spot delta
    to within the carry term and is what option chains display."""
    if T <= 0 or vol <= 0 or F <= 0 or K <= 0:
        itm = (F > K) if is_call else (F < K)
        d = (1.0 if is_call else -1.0) * (1.0 if itm else 0.0)
        return dict(delta=d, gamma=0.0, vega=0.0, theta=0.0)
    st = vol * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * st * st) / st
    d2 = d1 - st
    nd1 = _npdf(d1)
    delta = D * (_ncdf(d1) if is_call else -_ncdf(-d1))
    gamma = D * nd1 / (F * st)
    vega = D * F * nd1 * math.sqrt(T) / 100.0            # per 1 vol point
    theta = -D * F * nd1 * vol / (2.0 * math.sqrt(T)) / 365.0   # per calendar day
    return dict(delta=delta, gamma=gamma, vega=vega, theta=theta)


def implied_vol(price: float, F: float, K: float, T: float, is_call: bool,
                D: float = 1.0, lo: float = 1e-4, hi: float = 6.0) -> float:
    """Implied vol by bisection on the Black-76 price.  Returns nan when the
    quote is outside the no-arbitrage band (common for stale/crossed marks)."""
    if T <= 0 or price is None or price <= 0 or F <= 0 or K <= 0:
        return float("nan")
    intrinsic = D * (max(F - K, 0.0) if is_call else max(K - F, 0.0))
    upper = D * (F if is_call else K)
    if price <= intrinsic + 1e-9 or price >= upper - 1e-12:
        return float("nan")
    flo = black76(F, K, T, lo, is_call, D) - price
    fhi = black76(F, K, T, hi, is_call, D) - price
    if flo > 0 or fhi < 0:
        return float("nan")
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if black76(F, K, T, mid, is_call, D) - price > 0:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-8:
            break
    return 0.5 * (lo + hi)


def forward_and_discount(strikes, calls, puts, min_slope_gap: float = 10.0):
    """Extract (F, D) for one expiration from paired call/put marks.

    Least-squares on (C-P) vs K is unusable here: the wings carry stale and
    crossed marks, and because F comes out of the *intercept* a small slope
    error is amplified into dollars of forward error.  Both estimates are
    therefore taken as medians, which ignore the bad quotes outright:

      D = -median of pairwise slopes over strikes at least `min_slope_gap` apart
      F = median of  K + (C_K - P_K)/D  over the strikes nearest the money
    """
    k = np.asarray(strikes, dtype=float)
    y = np.asarray(calls, dtype=float) - np.asarray(puts, dtype=float)
    ok = np.isfinite(k) & np.isfinite(y)
    k, y = k[ok], y[ok]
    if k.size == 0:
        return float("nan"), float("nan")
    if k.size < 3:
        i = int(np.argmin(np.abs(y)))
        return float(k[i] + y[i]), 1.0

    order = np.argsort(k)
    k, y = k[order], y[order]

    # Discount factor from the median pairwise slope of widely separated strikes.
    dk = k[:, None] - k[None, :]
    dy = y[:, None] - y[None, :]
    far = dk >= min_slope_gap
    if far.sum() >= 3:
        D = -float(np.median(dy[far] / dk[far]))
    else:
        D = 1.0
    if not (0.90 <= D <= 1.0):
        D = min(max(D, 0.90), 1.0)

    # Forward from the strikes nearest the money, where both legs are alive.
    atm_i = int(np.argmin(np.abs(y)))
    near = np.argsort(np.abs(k - k[atm_i]))[: min(9, k.size)]
    F = float(np.median(k[near] + y[near] / D))
    return F, D
