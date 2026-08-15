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
from collections import defaultdict
import sqlite3
import threading
import time
from typing import Any

from . import greeks
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
                 right: str | None, ttl_minutes: float
                 ) -> tuple[list[dict[str, Any]], float] | None:
    """Rows for this window, and their AGE in seconds, if some fetched region
    CONTAINS it and is fresh.

    Containment, not equality: a leg dragged a few dollars asks a different
    question with the same answer, and equality keying is what made the old
    in-memory cache miss on every drag. None means "ask the provider" — never
    an empty list, because a covered window with no contracts is a real answer
    and must not look like a miss.

    The age rides along because only this function knows it. A cached chain is
    a real answer, but a caller deciding on it should be told whether it was
    fetched a moment ago or a quarter of an hour ago, and the alternative is a
    second query asking what the first already read.
    """
    cutoff = (dt.datetime.now(dt.timezone.utc)
              - dt.timedelta(minutes=ttl_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cover = con.execute(
        "SELECT fetched_at FROM chain_cover WHERE underlying=?"
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
    # Stamps are written as UTC 'Z' strings; parsed back the same way rather
    # than trusting the process's local zone, which would put the age out by
    # whole hours depending on where the machine is.
    try:
        when = dt.datetime.strptime(cover["fetched_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc)
        age = (dt.datetime.now(dt.timezone.utc) - when).total_seconds()
    except (ValueError, TypeError, KeyError):
        age = 0.0  # unreadable stamp: report fresh rather than invent a number
    return [dict(r) for r in rows], age


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


# Asset classes this LIVE-CHAIN endpoint cannot serve. Answering before any
# network call is not an optimisation — it is the difference between "we asked
# and there is nothing" and "there was never anything to ask about". The
# recorder's refusals intentionally diverge for futures now (2026-08-14): it
# RECORDS futures chains through TastyTrade, while this leg-window endpoint
# still reads only the Alpaca live path — hence the honest pointer below.
#: /market-data/by-type 414s past ~200 symbols (measured live).
FUT_QUOTE_BATCH = 90

NO_CHAIN_CLASSES = {
    "index": "an index has no listed options chain of its own — trade the ETF or the index options product",
    "crypto": "crypto has no options chain",
}


def _answer(underlying: str, rows: list[dict[str, Any]], source: str, *,
            reason: str | None = None, age_s: float | None = None) -> dict[str, Any]:
    """The one success shape, built in one place.

    Three paths reach it — the database cache, the in-memory cache and a live
    fetch — and they previously each spelled the dict out. That is how a field
    added for one path silently goes missing from the other two.

    `expirations` is taken from the FULL filtered set, deliberately BEFORE the
    MAX_ROWS slice. The heatmap draws one column per expiration; reading them
    off the truncated list would drop whole columns while `total` still
    reported the contracts in them, so a grid would quietly lose its far month
    with nothing anywhere saying it had. This is also what stops the client
    inventing expirations from a weekday rule — real listed dates come from the
    data, never from a calendar guess.
    """
    total = len(rows)
    out: dict[str, Any] = {
        "underlying": underlying,
        "available": True,
        "contracts": rows[:MAX_ROWS],
        "total": total,
        "truncated": total > MAX_ROWS,
        "expirations": sorted({r["expiration"] for r in rows if r.get("expiration")}),
        "source": source,
    }
    if reason is not None:
        out["reason"] = reason
    # How stale the quotes are, in seconds. A cached chain is a real answer, but
    # "12 minutes old" and "just now" support different decisions, and only the
    # server knows which one it handed over.
    if age_s is not None:
        out["age_seconds"] = round(max(0.0, age_s), 1)
    return out


def futures_contracts(tasty: dict[str, str], underlying: str,
                      d_from: dt.date, d_to: dt.date,
                      strike_lo: float, strike_hi: float,
                      right: str | None) -> list[dict[str, Any]]:
    """One leg's futures-option contracts, live from TastyTrade.

    The nested chain gives the tradeable STRUCTURE (every listed expiration
    and strike, with the exchange's own contract symbols); /market-data/by-type
    then prices only the ones inside the window. Filtering before quoting is
    the whole trick — a full /ES chain is ~10k contracts and the quote endpoint
    414s past ~200 symbols (measured), so quoting the window is both correct
    and the only thing that fits.

    Greeks come back empty because this feed publishes none; they are left
    NULL rather than modelled here, exactly as the recorder stores them. The
    Opt page solves its own when it needs them.
    """
    from .brokers.tastytrade import TastyTradeAdapter

    ad = TastyTradeAdapter(tasty["client_secret"], tasty["refresh_token"])
    chain = ad.nested_future_chain(underlying)

    # ONE LISTING PER EXPIRATION DATE. A futures option chain can list the
    # same DATE twice under different roots, written on DIFFERENT underlying
    # futures — measured on /MES: 2026-09-18 is root MES on /MESU6 AND root
    # EX3 on /MESZ6, and 2026-12-18 is MES on /MESZ6 and EX3 on /MESH7. They
    # are not interchangeable: a different underlying is a different forward,
    # so the same strike carries a genuinely different premium, and feeding
    # both to a term structure drew two points at one DTE joined by a
    # vertical jump.
    #
    # The one to keep is the natural pairing: the option settles into the
    # NEAREST future expiring on or after it. Anything else quietly compares
    # a September option against a December future.
    trimmed_note: str | None = None
    fut_exp = {f.get("symbol"): (f.get("expiration_date") or "")[:10]
               for f in chain.get("futures", []) if f.get("symbol")}

    def pairing_rank(exp_row: dict[str, Any], e: str) -> tuple[int, str]:
        u = exp_row.get("underlying_symbol") or ""
        ue = fut_exp.get(u, "")
        if not ue:
            return (2, u)          # unknown underlying: least preferred
        return (0 if ue >= e else 1, ue)   # nearest future on/after the option

    best_for_date: dict[str, tuple[tuple[int, str], dict[str, Any]]] = {}
    for oc in chain.get("chains", []):
        for exp in oc.get("expirations", []):
            e = (exp.get("expiration_date") or "")[:10]
            if not e:
                continue
            try:
                d = dt.date.fromisoformat(e)
            except ValueError:
                continue
            if d < d_from or d > d_to:
                continue
            rank = pairing_rank(exp, e)
            cur = best_for_date.get(e)
            if cur is None or rank < cur[0]:
                best_for_date[e] = (rank, exp)

    wanted: list[dict[str, Any]] = []
    for e, (_rank, exp) in best_for_date.items():
        under = exp.get("underlying_symbol") or ""
        for st in exp.get("strikes", []):
            k = st.get("strike")
            if k is None or not (strike_lo <= k <= strike_hi):
                continue
            for rt, sym in (("C", st.get("call")), ("P", st.get("put"))):
                if not sym or (right is not None and rt != right):
                    continue
                wanted.append({"occ_symbol": sym, "expiration": e,
                               "strike": float(k), "right": rt,
                               "underlying_future": under})
    if not wanted:
        return []

    # FAIR TRUNCATION ACROSS EXPIRATIONS. The row cap is applied downstream to
    # a flat list, and a near-dated futures expiration lists FAR more strikes
    # than a far-dated one — /MES at 6 DTE lists 360 strikes across a wide
    # window while 16 DTE lists 40. Emitted expiration-by-expiration, the
    # front month ate the entire 400-row budget and every later expiration
    # arrived EMPTY: the heatmap showed two columns of prices and hatching
    # everywhere else, which reads as "these contracts do not exist" when in
    # fact they were never asked for.
    #
    # So the budget is shared: each expiration contributes in turn, nearest
    # the middle of the requested window first — that is the leg's own strike,
    # and the neighbourhood a trader is actually reading. Trimming BEFORE the
    # quote round-trips also stops us pricing rows we are about to discard.
    if len(wanted) > MAX_ROWS:
        centre = 0.5 * (strike_lo + strike_hi)
        by_exp: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for w in wanted:
            by_exp[w["expiration"]].append(w)
        for v in by_exp.values():
            v.sort(key=lambda w: (abs(w["strike"] - centre), w["strike"], w["right"]))
        order = sorted(by_exp)
        picked: list[dict[str, Any]] = []
        depth = 0
        while len(picked) < MAX_ROWS:
            added = False
            for e in order:
                rows_e = by_exp[e]
                if depth < len(rows_e):
                    picked.append(rows_e[depth])
                    added = True
                    if len(picked) >= MAX_ROWS:
                        break
            if not added:
                break
            depth += 1
        wanted = picked
        trimmed_note = (
            f"showing the {MAX_ROWS} contracts nearest your strike, shared "
            f"evenly across {len(order)} expirations — widen the window less, "
            f"or narrow the strike range, to see further out")

    quotes: dict[str, dict[str, Any]] = {}
    for i in range(0, len(wanted), FUT_QUOTE_BATCH):
        batch = [w["occ_symbol"] for w in wanted[i:i + FUT_QUOTE_BATCH]]
        for q in ad.quotes({"future-option": batch}):
            if q.get("symbol"):
                quotes[q["symbol"]] = q

    out = []
    for w in wanted:
        q = quotes.get(w["occ_symbol"], {})
        out.append({**w,
                    "bid": q.get("bid"), "ask": q.get("ask"),
                    "last": q.get("last") if q.get("last") is not None
                            else q.get("mark"),
                    "iv": None, "delta": None, "gamma": None,
                    "theta": None, "vega": None, "rho": None})

    # SOLVE THE GREEKS THIS FEED DOES NOT PUBLISH. Not a nicety: the Opt
    # page's constant-shape history matches on |delta| and falls back to the
    # STRIKE when none is known — and a fixed futures strike only exists near
    # the money, so /MES history at strike 7820 began in 2025 while the
    # archive held 1,499 delta-matched points back to 2020. The same solver
    # the purchased history used (backend/greeks.py), so one contract cannot
    # carry two different deltas depending on which surface asked.
    try:
        # The future's own mark is the Black-76 forward for options written on
        # it — and the only usable one here, since a one-right window has no
        # call/put pairs to extract it from.
        fwd = None
        try:
            fwd = ad.future_snapshot(underlying).get("price")
        except BrokerError:
            pass
        solved = greeks.solve_chain(out, forward=fwd)
        if solved:
            LOG.debug("solved greeks for %d/%d %s contracts",
                      solved, len(out), underlying)
    except Exception:  # noqa: BLE001 — a chain without greeks still renders
        LOG.info("greek solve failed for %s", underlying, exc_info=True)
    return out, trimmed_note


def fetch(creds: dict[str, str] | None, underlying: str,
          exp_from: str, exp_to: str, strike_lo: float, strike_hi: float,
          right: str | None, con: sqlite3.Connection | None = None,
          ttl_minutes: float = 0.0, asset_class: str | None = None,
          tasty: dict[str, str] | None = None) -> dict[str, Any]:
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
                    "total": 0, "truncated": False, "expirations": [],
                    "source": "alpaca (indicative)",
                    "reason": "that window is entirely in the past — every "
                              "contract in it has expired"}

    # ASSET CLASS, before creds and before any network call. An index or a
    # future has no listed chain here at all, and asking Alpaca produces an
    # empty list that is indistinguishable from a filter matching nothing —
    # so the user retunes a window that was never going to match. An unknown
    # symbol passes through: the universe is not exhaustive, and refusing on
    # absence would block every ticker it has not heard of.
    # FUTURES GO TO TASTYTRADE, which lists them. This used to sit in
    # NO_CHAIN_CLASSES saying the panel could not browse them — while the
    # recorder was already pulling the very same chains through the very same
    # adapter, so the app refused data it was demonstrably able to fetch.
    if (asset_class or "").lower() == "future":
        if tasty is None:
            return {"underlying": underlying, "available": False, "contracts": [],
                    "total": 0, "truncated": False, "expirations": [],
                    "source": "none",
                    "reason": "futures option chains come through TastyTrade — "
                              "add a TastyTrade account on the Accounts page"}
        try:
            rows, trim_note = futures_contracts(tasty, underlying, d_from, d_to,
                                                strike_lo, strike_hi, right)
        except BrokerError as e:
            return {"underlying": underlying, "available": False, "contracts": [],
                    "total": 0, "truncated": False, "expirations": [],
                    "source": "none", "reason": str(e)}
        clamp_note = ("window clamped to today — expired contracts are not "
                      "shown" if clamped else None)
        return _answer(underlying, rows, "tastytrade",
                       reason=" · ".join(x for x in (clamp_note, trim_note) if x)
                       or None)

    if asset_class:
        refusal = NO_CHAIN_CLASSES.get(asset_class.lower())
        if refusal:
            return {"underlying": underlying, "available": False, "contracts": [],
                    "total": 0, "truncated": False, "expirations": [],
                    "source": "none", "reason": refusal}

    # NOTE the guard for missing creds is NOT here. It used to be, sitting
    # directly above a block commented "the DATABASE cache, tried first" —
    # which meant data already recorded on this machine was unreachable
    # without a live key. The recorder fills that cache unattended with the
    # system key; refusing to READ it because the interactive user has no key
    # of their own tells someone they need credentials to see data they
    # already own. A key is needed to FETCH, never to READ.

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
            cached_rows, cached_age = cached
            rows = filter_contracts(cached_rows, d_from, d_to, strike_lo, strike_hi, right)
            return _answer(
                underlying, rows, "alpaca (indicative, cached)",
                reason=("window clamped to today — expired contracts are not shown"
                        if clamped else None),
                age_s=cached_age)

    key = (underlying, str(d_from), str(d_to), strike_lo, strike_hi, right)
    now = time.monotonic()
    fetched_at = now  # a live fetch below is, by definition, current
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _TTL_S:
            rows = hit[1]
            fetched_at = hit[0]
        else:
            rows = None
    if rows is None:
        # THE CREDS GUARD LIVES HERE, at the only point that needs a key: the
        # live fetch. Both caches above have already been tried and missed, so
        # this refusal now means "nothing recorded and no way to fetch",
        # which is what the message claims — rather than firing over a cache
        # full of data the recorder pulled ten minutes ago.
        if creds is None:
            return {"underlying": underlying, "available": False, "contracts": [],
                    "total": 0, "truncated": False, "expirations": [],
                    "source": "none",
                    "reason": "no data key — add an Alpaca account to see live chains"}
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
                    "total": 0, "truncated": False, "expirations": [],
                    "source": "alpaca (indicative)", "reason": str(e)}
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

    return _answer(
        underlying, rows, "alpaca (indicative)",
        reason=("window clamped to today — expired contracts are not shown"
                if clamped else None),
        age_s=time.monotonic() - fetched_at)
