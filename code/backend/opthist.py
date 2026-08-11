"""Archived option-chain history — the data behind the Opt page's history side.

Reads ``<data>/options_history.db``, which ``tools/loadhist.py`` builds from
the workspace vault (daily EOD chains; SPY back to 2010). This is the ONLY
source of an option's price/spread through time: Alpaca sells no historical
option quotes on any plan (RESEARCH.md, verified 2026-08-06), so when this
database is absent the honest answer is a named reason, never an empty chart.

Read-only throughout — the sqlite URI carries mode=ro, so no code path here
can create, grow or lock the database the loader owns. Pure stdlib.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any

from .db import data_dir

NO_DB_REASON = ("no archived chain history is loaded — the live feed cannot "
                "provide it (Alpaca sells no historical option quotes), so "
                "history comes from your own archive once it is imported")

# Mirrors loadhist.BUCKETS; read from hist_meta at query time when present so
# the two cannot drift silently after a loader change.
DEFAULT_BUCKETS = [(0.005, 0.05), (0.05, 0.15), (0.15, 0.25),
                   (0.25, 0.35), (0.35, 0.50), (0.50, 1.01)]


def db_path() -> Path:
    return data_dir() / "options_history.db"


def _open() -> sqlite3.Connection | None:
    p = db_path()
    if not p.exists():
        return None
    con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _meta(con: sqlite3.Connection) -> dict[str, str]:
    try:
        return {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM hist_meta")}
    except sqlite3.Error:
        return {}


def _refuse(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason, "rows": [], "source": "none"}


def history(underlying: str, expiration: str, strike: float,
            right: str) -> dict[str, Any]:
    """One contract's daily row through its archived life.

    The rows carry both the calendar date and the DTE at that date, because the
    two views chart different x-axes: price history runs on the date, the fan
    chart runs on days-to-expiry. Spread is None on any day with no real bid —
    a one-sided market has no spread, and the chart draws that as a gap.
    """
    con = _open()
    if con is None:
        return _refuse(NO_DB_REASON)
    try:
        exp = dt.date.fromisoformat(expiration)
        rows = con.execute(
            "SELECT date, bid, ask, last, iv, delta, volume, open_interest"
            " FROM hist_chain WHERE underlying=? AND expiration=? AND right=?"
            " AND strike BETWEEN ? AND ? ORDER BY date",
            (underlying.upper(), expiration, right, strike - 0.005, strike + 0.005),
        ).fetchall()
        meta = _meta(con)
    finally:
        con.close()
    if not rows:
        return _refuse(f"no archived rows for {underlying.upper()} {strike:g}"
                       f" {right} {expiration} — the archive's recent window may"
                       " not reach this contract")
    out = []
    for r in rows:
        day = dt.date.fromisoformat(r["date"])
        bid, ask = r["bid"], r["ask"]
        two_sided = bid is not None and ask is not None and bid > 0 and ask >= bid
        out.append({
            "date": r["date"],
            "dte": (exp - day).days,
            "bid": bid, "ask": ask, "last": r["last"],
            "mid": round((bid + ask) / 2, 4) if two_sided else None,
            "spread": round(ask - bid, 4) if two_sided else None,
            "iv": r["iv"], "delta": r["delta"],
            "volume": r["volume"], "open_interest": r["open_interest"],
        })
    return {"available": True, "rows": out,
            "source": meta.get("source", "your archive"),
            "first": out[0]["date"], "last": out[-1]["date"]}


# The archive's own schema, so a live collector can extend it. Mirrors
# tools/loadhist.py, which built the file in the first place.
_ARCHIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS hist_chain (
    underlying TEXT NOT NULL,
    date       TEXT NOT NULL,
    expiration TEXT NOT NULL,
    strike     REAL NOT NULL,
    right      TEXT NOT NULL CHECK (right IN ('C','P')),
    bid        REAL, ask REAL, last REAL,
    iv         REAL, delta REAL,
    volume     REAL, open_interest REAL,
    PRIMARY KEY (underlying, expiration, strike, right, date)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS hist_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def append_day(underlying: str, date: str,
               contracts: list[dict[str, Any]]) -> int:
    """Add one session's chain to the archive THE OPT PAGE READS.

    This exists because the chain backfill was writing somewhere else. It
    stored into `data/backtest_data/<SYM>.db` — the backtest engine's store —
    while the Opt page reads `data/options_history.db`. Both are real
    databases, both were being written correctly, and the page showed
    nothing new, because filling one has never had any effect on the other.

    Idempotent: INSERT OR REPLACE on the archive's own primary key, so
    re-running a day corrects it rather than duplicating it.
    """
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    try:
        con.executescript(_ARCHIVE_SCHEMA)
        # TWO SHAPES REACH THIS. The live Alpaca collector hands over dicts;
        # chainimport.parse_text hands over ChainRow dataclasses. Assuming
        # dicts crashed the entire chain backfill on every tick with
        # `'ChainRow' object has no attribute 'get'` — and because the
        # recorder loop catches and logs, it kept running while silently
        # never archiving a thing.
        def field(c: Any, name: str) -> Any:
            if isinstance(c, dict):
                return c.get(name)
            return getattr(c, name, None)

        rows = []
        for c in contracts:
            right = str(field(c, "right") or "").upper()[:1]
            if right not in ("C", "P"):
                continue
            try:
                strike = float(field(c, "strike"))
            except (TypeError, ValueError):
                continue
            exp = str(field(c, "expiration") or "")[:10]
            if not exp:
                continue
            rows.append((underlying.upper(), date[:10], exp, strike, right,
                         field(c, "bid"), field(c, "ask"), field(c, "last"),
                         field(c, "iv"), field(c, "delta"),
                         field(c, "volume"), field(c, "open_interest")))
        if rows:
            with con:
                con.executemany(
                    "INSERT OR REPLACE INTO hist_chain VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    rows)
        return len(rows)
    finally:
        con.close()


def archived_dates(underlying: str, start: str, end: str) -> list[str]:
    """Sessions already in the archive for this symbol, within a range."""
    con = _open()
    if con is None:
        return []
    try:
        return [r[0] for r in con.execute(
            "SELECT DISTINCT date FROM hist_chain WHERE underlying=?"
            " AND date BETWEEN ? AND ? ORDER BY date",
            (underlying.upper(), start[:10], end[:10]))]
    except sqlite3.Error:
        return []
    finally:
        con.close()


#: The flat tolerances, which are now FLOORS rather than the whole story.
#: Keeping them as the minimum is what makes proportional matching safe to
#: turn on: a 21-DTE trade at 3% would be ±0.6 days, which matches fewer days
#: than the flat window did, so a setting meant to add data would have removed
#: it. Below ~100 DTE the floor wins and behaviour is unchanged.
MIN_DTE_TOL = 3
MIN_STRIKE_TOL = 1.0


def resolve_tolerances(dte: int, strike: float | None,
                       dte_pct: float = 0.0, strike_pct: float = 0.0,
                       dte_tol: int = MIN_DTE_TOL,
                       strike_tol: float = MIN_STRIKE_TOL) -> tuple[int, float]:
    """Turn the user's PERCENTAGE grace settings into absolute windows.

    Proportional because tenor is the thing that varies: ±3 days is a third of
    a 9-DTE trade and a hundredth of a 300-DTE one, and the archive's supply of
    matches varies the same way (weeklies near the front, quarterlies far out).
    A percentage says "same trade, near enough" once and means it at both ends.

    Floored, never scaled down — see MIN_DTE_TOL. Returns the window actually
    used so the caller can report it rather than implying the flat default.
    """
    d = max(int(dte_tol), int(round(dte * max(0.0, dte_pct) / 100.0)))
    s = strike_tol
    if strike is not None:
        s = max(float(strike_tol), abs(strike) * max(0.0, strike_pct) / 100.0)
    return d, s


def series_history(underlying: str, right: str, dte: int,
                   delta: float | None = None, strike: float | None = None,
                   dte_tol: int = MIN_DTE_TOL, delta_tol: float = 0.08,
                   strike_tol: float = MIN_STRIKE_TOL,
                   dte_pct: float = 0.0, strike_pct: float = 0.0) -> dict[str, Any]:
    """The CONSTANT-SHAPE series: what "a contract like this one" has cost,
    day by day, across the archive's recent window.

    "Like this one" is |DELTA| + DTE when a delta is known — Kade's call, and
    the industry's: a fixed strike drifts through moneyness as the underlying
    moves, so its series mostly re-plots the underlying (a 765P was $75 when
    SPY sat at 690 — that number is moneyness, not richness). Constant delta
    holds the trade's RISK SHAPE fixed, which is the apples-to-apples line.
    Strike matching remains the fallback for contracts whose delta the feed
    omitted (0DTE intraday, dead wings).

    Per day, one best match: nearest DTE, then nearest |delta| (or strike) —
    and each point CARRIES what it actually used, so a day matched at D.27
    instead of D.30 says so rather than pretending the series is constant.
    """
    # The ceiling is a sanity guard against nonsense input, NOT a policy on
    # tenor: it was 400 and a USO put at 402 DTE (a perfectly listed LEAPS)
    # blanked the whole history panel with a raw 422. Listed options run to
    # ~5 years; past that the number is a typo, not a trade.
    if dte <= 0 or dte > 2000:
        raise ValueError("dte out of range")
    if delta is None and strike is None:
        raise ValueError("need a delta or a strike to define the shape")
    # The percentage settings widen the flat windows; everything below this
    # line works in absolute terms, and `target` reports what was used.
    dte_tol, strike_tol = resolve_tolerances(
        dte, strike, dte_pct=dte_pct, strike_pct=strike_pct,
        dte_tol=dte_tol, strike_tol=strike_tol)
    con = _open()
    if con is None:
        return _refuse(NO_DB_REASON)
    adelta = abs(delta) if delta is not None else None
    try:
        if adelta is not None:
            rows = con.execute(
                "SELECT date, expiration, strike, bid, ask, iv, delta"
                " FROM hist_chain WHERE underlying=? AND right=?"
                " AND delta IS NOT NULL AND ABS(ABS(delta) - ?) <= ?"
                # The DTE window in SQL keeps the scan proportional to the
                # answer: julianday prunes the 7M-row table to candidates.
                " AND julianday(expiration) - julianday(date) BETWEEN ? AND ?",
                (underlying.upper(), right, adelta, delta_tol,
                 dte - dte_tol, dte + dte_tol),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT date, expiration, strike, bid, ask, iv, delta"
                " FROM hist_chain WHERE underlying=? AND right=?"
                " AND strike BETWEEN ? AND ?"
                " AND julianday(expiration) - julianday(date) BETWEEN ? AND ?",
                (underlying.upper(), right, strike - strike_tol, strike + strike_tol,
                 dte - dte_tol, dte + dte_tol),
            ).fetchall()
        meta = _meta(con)
    finally:
        con.close()
    if not rows:
        what = (f"delta {adelta:.2f}+-{delta_tol:g}" if adelta is not None
                else f"${strike:g}+-{strike_tol:g}")
        return _refuse(f"no archived {underlying.upper()} {right} near {what}"
                       f" at {dte}+-{dte_tol} DTE")

    # WHICH CANDIDATE WINS THE DAY, when several are inside the window.
    #
    # This was lexicographic — nearest DTE, then nearest |delta| — which was
    # right while the window was a flat ±3 days: every candidate was the same
    # trade and DTE was an almost-exact tie-break. A PROPORTIONAL window makes
    # it actively wrong. At 300 DTE the window is ±9 days, and lexicographic
    # ordering would take a 300-DTE contract at Δ.20 over a 299-DTE one at
    # Δ.30 — throwing away the shape match to win one day of tenor that the
    # grace setting has just declared unimportant.
    #
    # So both dimensions count, each SCALED BY ITS OWN TOLERANCE and summed.
    # Dividing by the tolerance is what makes days and deltas comparable at
    # all: "one tolerance away" costs the same on either axis whatever the
    # units. Squared so a candidate that is close on both beats one that is
    # perfect on one axis and at the edge of the other.
    dscale = float(max(dte_tol, 1))
    nscale = float(delta_tol if adelta is not None else strike_tol) or 1.0

    def score(r: sqlite3.Row) -> float:
        d = (dt.date.fromisoformat(r["expiration"]) - dt.date.fromisoformat(r["date"])).days
        near = (abs(abs(r["delta"]) - adelta) if adelta is not None
                else abs(r["strike"] - strike))
        return (abs(d - dte) / dscale) ** 2 + (near / nscale) ** 2

    # Scored once per row rather than once per comparison — the wide windows
    # this setting enables make the candidate list a lot longer.
    best: dict[str, tuple[float, sqlite3.Row]] = {}
    for r in rows:
        sc = score(r)
        cur = best.get(r["date"])
        if cur is None or sc < cur[0]:
            best[r["date"]] = (sc, r)

    out = []
    for date in sorted(best):
        r = best[date][1]
        bid, ask = r["bid"], r["ask"]
        two_sided = bid is not None and ask is not None and bid > 0 and ask >= bid
        d = (dt.date.fromisoformat(r["expiration"]) - dt.date.fromisoformat(date)).days
        out.append({
            "date": date,
            "mid": round((bid + ask) / 2, 4) if two_sided else None,
            "bid": bid, "ask": ask,
            "spread": round(ask - bid, 4) if two_sided else None,
            "used_dte": d, "used_strike": r["strike"],
            "used_delta": r["delta"],
            # The matched contract's IDENTITY, verbatim. Every node on the
            # history chart is one archived contract, and clicking one to
            # follow its exit needs (expiration, strike, right) exactly — not
            # re-derived client-side from date + used_dte, which is the same
            # number today but only by arithmetic nobody is checking.
            "used_expiration": r["expiration"],
            "iv": r["iv"], "delta": r["delta"],
        })
    return {"available": True, "rows": out,
            "source": meta.get("source", "your archive"),
            "mode": "delta" if adelta is not None else "strike",
            # The RESOLVED windows, not the requested ones — the caption quotes
            # these, and a chart that says "±3 days" while matching ±9 is the
            # kind of quiet lie this file exists to avoid.
            "target": {"delta": adelta, "strike": strike, "right": right,
                       "dte": dte, "dte_tol": dte_tol,
                       "delta_tol": delta_tol, "strike_tol": strike_tol,
                       "dte_pct": dte_pct, "strike_pct": strike_pct},
            "first": out[0]["date"], "last": out[-1]["date"]}


def fanchart(underlying: str, expiration: str, strike: float,
             right: str) -> dict[str, Any]:
    """The fan chart: this contract's spread path over its life, drawn against
    the archive-wide spread percentiles of SIMILAR contracts at each DTE.

    Similar means the same |delta| bucket, chosen from the MEDIAN |delta| of the
    contract's own archived life — a contract drifts across deltas as the
    underlying moves, and the median is the band it actually lived in. The band
    says how contracts like this one have historically been priced at each
    point of their life; the path says how this one actually was.
    """
    hist = history(underlying, expiration, strike, right)
    if not hist["available"]:
        return {**hist, "band": [], "path": []}
    deltas = sorted(abs(r["delta"]) for r in hist["rows"] if r["delta"])
    if not deltas:
        return {"available": False, "band": [], "path": [], "source": hist["source"],
                "reason": "this contract's archived rows carry no delta, so there"
                          " is nothing to pool it with"}
    med = deltas[len(deltas) // 2]

    con = _open()
    if con is None:  # raced away since history() — treat as absent
        return _refuse(NO_DB_REASON) | {"band": [], "path": []}
    try:
        meta = _meta(con)
        try:
            buckets = [tuple(b) for b in json.loads(meta.get("buckets", ""))]
        except (ValueError, TypeError):
            buckets = DEFAULT_BUCKETS
        bucket = next((i for i, (lo, hi) in enumerate(buckets) if lo <= med < hi), None)
        band_rows = [] if bucket is None else con.execute(
            "SELECT dte, p10, p25, p50, p75, p90, n FROM hist_spread_pct"
            " WHERE underlying=? AND right=? AND bucket=? ORDER BY dte",
            (underlying.upper(), right, bucket),
        ).fetchall()
    finally:
        con.close()

    lo, hi = (buckets[bucket] if bucket is not None else (None, None))
    return {
        "available": True,
        "source": hist["source"],
        "path": [{"dte": r["dte"], "spread": r["spread"], "date": r["date"]}
                 for r in hist["rows"]],
        "band": [dict(r) for r in band_rows],
        "bucket": {"median_delta": round(med, 3), "lo": lo, "hi": hi},
        # An empty band with a real path is a legitimate state (thin bucket);
        # say why rather than leaving a bare line where bands were promised.
        **({} if band_rows else
           {"band_reason": "not enough similar archived contracts to draw a band"}),
    }
