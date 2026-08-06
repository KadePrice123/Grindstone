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
import sqlite3
import threading
import time
from typing import Any

from .brokers.alpaca_data import AlpacaData
from .brokers.base import BrokerError
from .logs import LOG

# One leg's window is a few hundred rows; anything past this cap is a filter
# wide enough that the user is browsing, not filtering — say so, honestly.
MAX_ROWS = 400

_TTL_S = 45.0
_cache: dict[tuple, tuple[float, list[dict[str, Any]]]] = {}
_cache_lock = threading.Lock()

# How much wider than the request a live fetch goes, so the drag that follows
# lands inside it. Bounded: the point is to cover a neighbourhood, not to creep
# back toward pulling the whole chain.
PAD_DAYS = 7
PAD_STRIKE_MIN = 5.0
PAD_STRIKE_MAX = 40.0
# Cached contracts older than this are swept on write. Independent of the
# freshness setting: that says when to REFETCH, this says when a row stops
# being worth the disk.
CACHE_KEEP_DAYS = 7

_GREEKS = ("bid", "ask", "last", "iv", "delta", "gamma", "theta", "vega", "rho")


def cache_lookup(con: sqlite3.Connection, underlying: str, d_from: dt.date,
                 d_to: dt.date, strike_lo: float, strike_hi: float,
                 right: str | None, ttl_minutes: float) -> list[dict[str, Any]] | None:
    """Rows for this window if some fetched region CONTAINS it and is fresh.

    Containment, not equality: a leg dragged a few dollars asks a different
    question with the same answer, and equality keying is what made the old
    in-memory cache miss on every drag. None means "ask the provider" — never
    an empty list, because a covered window with no contracts is a real answer
    and must not look like a miss.
    """
    cutoff = (dt.datetime.now(dt.timezone.utc)
              - dt.timedelta(minutes=ttl_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cover = con.execute(
        "SELECT 1 FROM chain_cover WHERE underlying=?"
        " AND exp_from<=? AND exp_to>=? AND strike_lo<=? AND strike_hi>=?"
        " AND (right='' OR right=?) AND fetched_at>=? LIMIT 1",
        (underlying, str(d_from), str(d_to), strike_lo, strike_hi,
         right or "", cutoff),
    ).fetchone()
    if cover is None:
        return None
    rows = con.execute(
        "SELECT occ_symbol, expiration, strike, right, bid, ask, last, iv,"
        " delta, gamma, theta, vega, rho FROM chain_cache"
        " WHERE underlying=? AND expiration>=? AND expiration<=?"
        " AND strike>=? AND strike<=?",
        (underlying, str(d_from), str(d_to), strike_lo, strike_hi),
    ).fetchall()
    return [dict(r) for r in rows]


def cache_store(con: sqlite3.Connection, underlying: str, d_from: dt.date,
                d_to: dt.date, strike_lo: float, strike_hi: float,
                right: str | None, rows: list[dict[str, Any]]) -> None:
    """Record the contracts AND the region they cover, in one transaction."""
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with con:
        for r in rows:
            # SKIP MALFORMED ROWS, the same policy filter_contracts states: a
            # provider is entitled to hand back a partial row, and one of them
            # must not cost the cache its whole write. It very nearly did — a
            # row with strike None violates NOT NULL, which aborts the
            # transaction and takes the COVER row with it, so every later
            # request missed while the code looked like it was caching. The
            # blanket sqlite3.Error guard at the call site hid the reason.
            if r.get("right") not in ("C", "P"):
                continue
            if not isinstance(r.get("occ_symbol"), str) or not r["occ_symbol"]:
                continue
            if isinstance(r.get("strike"), bool) or not isinstance(r.get("strike"), (int, float)):
                continue
            try:
                dt.date.fromisoformat(str(r.get("expiration")))
            except ValueError:
                continue
            con.execute(
                "INSERT INTO chain_cache (underlying, occ_symbol, expiration,"
                " strike, right, bid, ask, last, iv, delta, gamma, theta, vega,"
                " rho, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(underlying, occ_symbol) DO UPDATE SET"
                " bid=excluded.bid, ask=excluded.ask, last=excluded.last,"
                " iv=excluded.iv, delta=excluded.delta, gamma=excluded.gamma,"
                " theta=excluded.theta, vega=excluded.vega, rho=excluded.rho,"
                " fetched_at=excluded.fetched_at",
                (underlying, r.get("occ_symbol"), r.get("expiration"),
                 r.get("strike"), r.get("right"),
                 *[r.get(k) for k in _GREEKS], stamp),
            )
        con.execute(
            "INSERT INTO chain_cover (underlying, exp_from, exp_to, strike_lo,"
            " strike_hi, right, fetched_at) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(underlying, exp_from, exp_to, strike_lo, strike_hi,"
            " right) DO UPDATE SET fetched_at=excluded.fetched_at",
            (underlying, str(d_from), str(d_to), strike_lo, strike_hi,
             right or "", stamp),
        )
        # Sweep on write: expired contracts can never be fetched again, and a
        # cover row for a window nobody will ask for is a permanent hit for a
        # question with no rows behind it.
        old = (dt.datetime.now(dt.timezone.utc)
               - dt.timedelta(days=CACHE_KEEP_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        con.execute("DELETE FROM chain_cache WHERE fetched_at<?", (old,))
        con.execute("DELETE FROM chain_cover WHERE fetched_at<?", (old,))
        con.execute("DELETE FROM chain_cache WHERE expiration<?",
                    (dt.date.today().isoformat(),))


def cache_stats(con: sqlite3.Connection) -> dict[str, Any]:
    """What the settings page can honestly show about the cache."""
    c = con.execute("SELECT COUNT(*) n, MAX(fetched_at) newest FROM chain_cache").fetchone()
    v = con.execute("SELECT COUNT(*) n FROM chain_cover").fetchone()
    return {"contracts": c["n"], "windows": v["n"], "newest": c["newest"]}


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


def fetch(creds: dict[str, str] | None, underlying: str,
          exp_from: str, exp_to: str, strike_lo: float, strike_hi: float,
          right: str | None, con: sqlite3.Connection | None = None,
          ttl_minutes: float = 0.0) -> dict[str, Any]:
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

    # ---- the DATABASE cache, tried first --------------------------------
    # Keyed by COVERAGE, not by the exact request. The in-memory cache below is
    # keyed by the filter tuple, and a drag mints a new tuple on every commit —
    # so it missed on precisely the motion it existed to absorb. A cover row
    # says "this whole region was fetched at time T", and any request inside it
    # is served without a call.
    if con is not None and ttl_minutes > 0:
        try:
            cached = cache_lookup(con, underlying, d_from, d_to,
                                  strike_lo, strike_hi, right, ttl_minutes)
        except sqlite3.Error as e:
            # Degrade, but SAY SO. A silent `pass` here hid a plain
            # binding-count error through two debugging cycles: the cache
            # wrote nothing, every request missed, and the code read as if it
            # were working. A cache may fail; it may not fail quietly.
            LOG.warning("chain cache read failed, falling back to live: %s", e)
            cached = None
        if cached is not None:
            rows = filter_contracts(cached, d_from, d_to, strike_lo, strike_hi, right)
            total = len(rows)
            out = {"underlying": underlying, "available": True,
                   "contracts": rows[:MAX_ROWS], "total": total,
                   "truncated": total > MAX_ROWS,
                   "source": "alpaca (indicative, cached)"}
            if clamped:
                out["reason"] = "window clamped to today — expired contracts are not shown"
            return out

    key = (underlying, str(d_from), str(d_to), strike_lo, strike_hi, right)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _TTL_S:
            rows = hit[1]
        else:
            rows = None
    if rows is None:
        # NAMED fields, like every other call site. `AlpacaData(*creds)` on this
        # dict unpacks its KEYS, so the client authenticated with the literal
        # strings "key_id" and "secret_key" and Alpaca answered 401 — a real
        # bug that shipped, because the gate only ever exercised creds=None.
        client = AlpacaData(creds["key_id"], creds["secret_key"])
        # DELIBERATELY WIDER THAN ASKED. The next request will be a nudge of
        # this one — that is what dragging a leg is — so fetching a margin
        # around it turns the whole neighbourhood into cache hits for one
        # request instead of one hit per position. The pad is bounded so this
        # never drifts back toward pulling the entire ~10k-row chain.
        p_from, p_to, p_lo, p_hi = (d_from, d_to, strike_lo, strike_hi)
        if con is not None and ttl_minutes > 0:
            p_from = max(today, d_from - dt.timedelta(days=PAD_DAYS))
            p_to = d_to + dt.timedelta(days=PAD_DAYS)
            span = max(strike_hi - strike_lo, 1.0)
            pad = min(max(span, PAD_STRIKE_MIN), PAD_STRIKE_MAX)
            p_lo = max(0.0, strike_lo - pad)
            p_hi = strike_hi + pad
        try:
            raw = client.chain_snapshot(
                underlying, exp_gte=str(p_from), exp_lte=str(p_to),
                strike_gte=p_lo, strike_lte=p_hi, right=right)
        except BrokerError as e:
            return {"underlying": underlying, "available": False, "contracts": [],
                    "total": 0, "truncated": False, "source": "alpaca (indicative)",
                    "reason": str(e)}
        if con is not None and ttl_minutes > 0:
            try:
                cache_store(con, underlying, p_from, p_to, p_lo, p_hi, right, raw)
            except sqlite3.Error as e:
                LOG.warning("chain cache write failed (%s) — serving live", e)
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
