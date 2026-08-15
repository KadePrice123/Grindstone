"""Solve IV and delta for a chain whose feed publishes neither.

ONE implementation, deliberately. Two surfaces need this — the live futures
chain (options.futures_contracts) and the purchased history
(tools/backfill_greeks.py) — and if they solved it separately the same
contract could carry two different deltas depending on which screen asked.

METHOD (the engine's own, bt.pricing): per expiration the forward and
discount come from PAIRED call/put marks, medians so stale wings cannot drag
them; then Black-76 implied vol by bisection and its delta. Extracting the
forward beats assuming a rate and a carry — for a future the carry is already
in the forward, and for a leveraged ETF so is the borrow.

WHERE IT IS GOOD, measured against 46k rows of real feed deltas
(2026-08-15): |delta| 0.10-0.25 -> 0.0034 median error, 99% within 0.02;
0.25-0.45 -> 0.0105; deep ITM ~0.031 and systematically off, because those
contracts are AMERICAN and this model is European. The short-delta region
this workspace trades is an order tighter than the +-0.08 window the Opt
page matches on; deep ITM is a model's opinion and is labelled as solved,
never as the feed's own number.
"""
from __future__ import annotations

import datetime as dt
import math
import statistics
from collections import defaultdict
from typing import Any


# NO NUMPY. bt.pricing holds the same maths but imports numpy at module
# scope, and the gate forbids numpy anywhere in the sidecar's import chain —
# a heavy import holds the GIL and this backend is single-process
# (REQUIREMENTS 4.3: an eager one froze sign-in for ~10s). The scalar core is
# therefore reproduced here in pure `math`, and the GATE PINS THE TWO EQUAL on
# a grid of inputs, so they cannot drift into two different answers for one
# contract. bt.pricing stays the engine's copy; this is the sidecar's.
_SQRT2 = math.sqrt(2.0)
_SQRT2PI = math.sqrt(2.0 * math.pi)


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / _SQRT2))


def _npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT2PI


def black76(F: float, K: float, T: float, vol: float, is_call: bool,
            D: float = 1.0) -> float:
    if T <= 0 or vol <= 0 or F <= 0 or K <= 0:
        return D * (max(F - K, 0.0) if is_call else max(K - F, 0.0))
    st = vol * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * st * st) / st
    d2 = d1 - st
    if is_call:
        return D * (F * _ncdf(d1) - K * _ncdf(d2))
    return D * (K * _ncdf(-d2) - F * _ncdf(-d1))


def black76_greeks(F: float, K: float, T: float, vol: float, is_call: bool,
                   D: float = 1.0) -> dict:
    if T <= 0 or vol <= 0 or F <= 0 or K <= 0:
        itm = (F > K) if is_call else (F < K)
        return {"delta": (1.0 if is_call else -1.0) * (1.0 if itm else 0.0),
                "gamma": 0.0, "vega": 0.0, "theta": 0.0}
    st = vol * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * st * st) / st
    nd1 = _npdf(d1)
    return {
        "delta": D * (_ncdf(d1) if is_call else -_ncdf(-d1)),
        "gamma": D * nd1 / (F * st),
        "vega": D * F * nd1 * math.sqrt(T) / 100.0,
        "theta": -D * F * nd1 * vol / (2.0 * math.sqrt(T)) / 365.0,
    }


def implied_vol(price: float, F: float, K: float, T: float, is_call: bool,
                D: float = 1.0, lo: float = 1e-4, hi: float = 6.0) -> float:
    """Bisection, identical bounds and iteration count to bt.pricing so the
    two agree bit for bit; nan when the quote is outside the no-arbitrage
    band, which is common for stale or crossed marks."""
    if T <= 0 or price is None or price <= 0 or F <= 0 or K <= 0:
        return float("nan")
    intrinsic = D * (max(F - K, 0.0) if is_call else max(K - F, 0.0))
    upper = D * (F if is_call else K)
    if price <= intrinsic + 1e-9 or price >= upper - 1e-12:
        return float("nan")
    if black76(F, K, T, lo, is_call, D) - price > 0:
        return float("nan")
    if black76(F, K, T, hi, is_call, D) - price < 0:
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
    """(F, D) from paired call/put marks, by MEDIANS — the wings carry stale
    and crossed quotes and F comes out of an intercept, so a least-squares fit
    amplifies one bad row into dollars of error."""
    pairs = [(float(k), float(c) - float(p))
             for k, c, p in zip(strikes, calls, puts)
             if all(math.isfinite(float(x)) for x in (k, c, p))]
    if len(pairs) < 2:
        if pairs:
            k, y = min(pairs, key=lambda t: abs(t[1]))
            return k + y, 1.0
        return float("nan"), float("nan")
    pairs.sort()
    ks = [k for k, _ in pairs]
    ys = [y for _, y in pairs]
    slopes = [(ys[i] - ys[i - 1]) / (ks[i] - ks[i - 1])
              for i in range(1, len(pairs))
              if ks[i] - ks[i - 1] >= min_slope_gap]
    if not slopes:
        slopes = [(ys[i] - ys[i - 1]) / (ks[i] - ks[i - 1])
                  for i in range(1, len(pairs)) if ks[i] != ks[i - 1]]
    if not slopes:
        return float("nan"), float("nan")
    D = -statistics.median(slopes)
    if not (math.isfinite(D)) or D <= 0:
        D = 1.0
    atm = min(range(len(pairs)), key=lambda i: abs(ys[i]))
    near = sorted(range(len(pairs)), key=lambda i: abs(ks[i] - ks[atm]))[:9]
    F = statistics.median([ks[i] + ys[i] / D for i in near])
    return F, D


YEAR = 365.0
#: Marks it will price from, best first. MID leads: a last trade goes stale
#: while the feed's own greek came from the mid, and preferring `last` was
#: measured at a 0.037 median error against real deltas versus 0.014 for mid.
_PRICE_KEYS = ("mid", "last")


def mark_of(row: dict[str, Any]) -> float | None:
    bid, ask = row.get("bid"), row.get("ask")
    if bid is not None and ask is not None:
        try:
            b, a = float(bid), float(ask)
            if a > 0 and b >= 0:
                mid = 0.5 * (b + a)
                if mid > 0:
                    return mid
        except (TypeError, ValueError):
            pass
    for k in _PRICE_KEYS:
        v = row.get(k)
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0:
            return f
    return None


def solve_expiration(rows: list[dict[str, Any]], asof: dt.date,
                     forward: float | None = None) -> int:
    """Fill iv/delta IN PLACE for one expiration's rows. Returns how many were
    solved. Rows whose group yields no forward are left untouched — a delta
    invented from a broken forward is worse than the honest blank the UI
    already knows how to render."""
    by_strike: dict[float, dict[str, float]] = defaultdict(dict)
    exp = ""
    for r in rows:
        m = mark_of(r)
        k = r.get("strike")
        rt = r.get("right")
        if m is None or k is None or rt not in ("C", "P"):
            continue
        exp = exp or str(r.get("expiration") or "")[:10]
        by_strike[float(k)][rt] = m

    strikes, calls, puts = [], [], []
    for k in sorted(by_strike):
        pair = by_strike[k]
        if "C" in pair and "P" in pair:
            strikes.append(k)
            calls.append(pair["C"])
            puts.append(pair["P"])
    if not exp:
        return 0
    # PARITY FIRST, the underlying's own price second. Parity is the better
    # estimate when both sides are present because it carries the borrow and
    # the carry for free — but the Opt page fetches ONE RIGHT at a time, so a
    # puts-only window has no pairs at all and solved nothing until this
    # fallback existed. For a FUTURES option the fallback is not an
    # approximation: the option is written on the future, so the future's
    # price IS the Black-76 forward.
    F = D = None
    if len(strikes) >= 3:
        try:
            F, D = forward_and_discount(strikes, calls, puts)
        except Exception:  # noqa: BLE001 — fall through to the given forward
            F = D = None
    if not (F and math.isfinite(F) and F > 0):
        F, D = forward, 1.0
    if not (F and math.isfinite(F) and F > 0):
        return 0
    if not (D and math.isfinite(D) and 0 < D <= 1.5):
        D = 1.0
    try:
        T = (dt.date.fromisoformat(exp) - asof).days / YEAR
    except ValueError:
        return 0
    if T <= 0:
        return 0

    n = 0
    for r in rows:
        m = mark_of(r)
        k, rt = r.get("strike"), r.get("right")
        if m is None or k is None or rt not in ("C", "P"):
            continue
        iv = implied_vol(m, F, float(k), T, rt == "C", D)
        if not math.isfinite(iv):
            continue
        g = black76_greeks(F, float(k), T, iv, rt == "C", D)
        if not math.isfinite(g["delta"]):
            continue
        r["iv"] = iv
        r["delta"] = g["delta"]
        r["gamma"] = g["gamma"]
        r["theta"] = g["theta"]
        r["vega"] = g["vega"]
        r["greeks_solved"] = True
        n += 1
    return n


def solve_chain(rows: list[dict[str, Any]], asof: dt.date | None = None,
                forward: float | None = None) -> int:
    """Fill iv/delta for every expiration present. Only touches rows that
    arrived without a delta, so a feed that DOES publish greeks always wins —
    the model is the fallback, never an override."""
    asof = asof or dt.date.today()
    need = [r for r in rows if r.get("delta") is None]
    if not need:
        return 0
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in need:
        groups[str(r.get("expiration") or "")[:10]].append(r)
    return sum(solve_expiration(g, asof, forward)
               for g in groups.values() if g)
