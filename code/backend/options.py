"""Filtered options chains for the chart's leg objects.

A leg on the chart is a point (expiration, strike) with an acceptance window
around it (± DTE days, ± $ strike). This module answers "which contracts fall
inside that window" — FILTERING ONLY, no order surface anywhere near it.

Shape of the service:
  - filter_contracts() is a PURE function over parsed rows. It runs on every
    response even though the same bounds already rode to the provider as query
    params — both as a guard against a provider ignoring a param, and because
    a pure function is what the gate can test offline and mutate.
  - fetch() carries the creds/caching/degradation policy. No creds is a
    DESIGNED state, not an error: the e2e profile has none, and the panel
    renders the reason. A short TTL cache keyed by the exact filter tuple
    absorbs drag-storms; the CLIENT additionally debounces, and nothing here
    ever polls — refetch belongs to leg-edit events, the same religion as the
    engine's no-repaint-at-rest rule.
"""
from __future__ import annotations

import datetime as dt
import threading
import time
from typing import Any

from .brokers.alpaca_data import AlpacaData
from .brokers.base import BrokerError

# One leg's window is a few hundred rows; anything past this cap is a filter
# wide enough that the user is browsing, not filtering — say so, honestly.
MAX_ROWS = 400

_TTL_S = 45.0
_cache: dict[tuple, tuple[float, list[dict[str, Any]]]] = {}
_cache_lock = threading.Lock()


def _iso_date(v: str, what: str) -> dt.date:
    try:
        return dt.date.fromisoformat(v)
    except ValueError:
        raise ValueError(f"{what} must be YYYY-MM-DD, got {v!r}") from None


def filter_contracts(rows: list[dict[str, Any]], exp_from: dt.date, exp_to: dt.date,
                     strike_lo: float, strike_hi: float,
                     right: str | None) -> list[dict[str, Any]]:
    """Inclusive on every bound — a strike sitting exactly on the edge of an
    acceptance zone is inside it, which is what the drawn rectangle says."""
    out = []
    for r in rows:
        try:
            exp = dt.date.fromisoformat(r["expiration"])
        except (KeyError, ValueError):
            continue  # a malformed row is dropped, never a crash
        if not (exp_from <= exp <= exp_to):
            continue
        strike = r.get("strike")
        if not isinstance(strike, (int, float)) or not (strike_lo <= strike <= strike_hi):
            continue
        if right is not None and r.get("right") != right:
            continue
        out.append(r)
    # Nearest-first is the panel's reading order; expiration breaks ties so a
    # same-strike weekly/monthly pair lists sooner-first.
    mid_strike = (strike_lo + strike_hi) / 2
    out.sort(key=lambda r: (abs(r["strike"] - mid_strike), r["expiration"], r["strike"]))
    return out


def fetch(creds: tuple[str, str] | None, underlying: str,
          exp_from: str, exp_to: str, strike_lo: float, strike_hi: float,
          right: str | None) -> dict[str, Any]:
    """One leg's contracts, or an honest reason there are none.

    Raises ValueError on malformed parameters (the route turns that into 422);
    provider failures come back as available=False with the reason, because a
    chain that quietly shows zero contracts is indistinguishable from a filter
    that matches zero — and those need different user reactions.
    """
    underlying = underlying.upper()
    d_from = _iso_date(exp_from, "exp_from")
    d_to = _iso_date(exp_to, "exp_to")
    if d_to < d_from:
        raise ValueError("exp_to is before exp_from")
    if not (strike_lo <= strike_hi):
        raise ValueError("strike range is inverted")
    if right is not None and right not in ("C", "P"):
        raise ValueError("right must be C or P")
    # Expired contracts are not in the snapshot; asking for them silently
    # returns nothing. Clamp and REPORT the clamp rather than let a
    # yesterday-inclusive window read as "no contracts exist".
    today = dt.date.today()
    clamped = d_from < today
    if clamped:
        d_from = today
        if d_to < d_from:
            return {"underlying": underlying, "available": True, "contracts": [],
                    "total": 0, "truncated": False, "source": "alpaca (indicative)",
                    "reason": "that window is entirely in the past — every "
                              "contract in it has expired"}

    if creds is None:
        return {"underlying": underlying, "available": False, "contracts": [],
                "total": 0, "truncated": False, "source": "none",
                "reason": "no data key — add an Alpaca account to see live chains"}

    key = (underlying, str(d_from), str(d_to), strike_lo, strike_hi, right)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _TTL_S:
            rows = hit[1]
        else:
            rows = None
    if rows is None:
        client = AlpacaData(*creds)
        try:
            raw = client.chain_snapshot(
                underlying, exp_gte=str(d_from), exp_lte=str(d_to),
                strike_gte=strike_lo, strike_lte=strike_hi, right=right)
        except BrokerError as e:
            return {"underlying": underlying, "available": False, "contracts": [],
                    "total": 0, "truncated": False, "source": "alpaca (indicative)",
                    "reason": str(e)}
        rows = filter_contracts(raw, d_from, d_to, strike_lo, strike_hi, right)
        with _cache_lock:
            _cache[key] = (now, rows)
            # The cache is per filter tuple and drags mint new tuples; sweep
            # stale entries so a long session cannot grow it without bound.
            for k in [k for k, (ts, _) in _cache.items() if now - ts >= _TTL_S]:
                del _cache[k]

    total = len(rows)
    out = {"underlying": underlying, "available": True,
           "contracts": rows[:MAX_ROWS], "total": total,
           "truncated": total > MAX_ROWS, "source": "alpaca (indicative)"}
    if clamped:
        out["reason"] = "window clamped to today — expired contracts are not shown"
    return out
