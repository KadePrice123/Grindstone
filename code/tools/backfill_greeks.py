"""Solve IV + delta for archived chain rows that carry none, in place.

WHY THIS EXISTS. The Opt page's constant-shape history matches on |delta|
(opthist.series_history: `WHERE delta IS NOT NULL AND ABS(...)`), because a
fixed strike drifts through moneyness and mostly re-plots the underlying. The
Databento backfill carries settlement prices and open interest but NO greeks
— the settlement feed publishes none — so those rows were invisible to the
delta match and SPXL's history stopped at the recorder's own window.

METHOD — the engine's, not a second one. Per (date, expiration) the forward
and discount come from PAIRED call/put marks (bt.pricing.forward_and_discount,
medians, so stale wings cannot drag it); then Black-76 implied vol by
bisection and its delta. Extracting F beats assuming a rate + dividend yield,
which matters most for exactly the instruments here: a 3x leveraged ETF's
borrow and a future's carry are both in the forward already.

PROVE IT BEFORE TRUSTING IT: `--check` recomputes rows that ALREADY have a
feed delta and reports the error distribution instead of writing anything.
That is a real answer key — the same contracts, same days, priced by a live
feed — and it is the only reason to believe the solved numbers on the days
where no feed delta was ever published.

WHERE THESE NUMBERS ARE GOOD, measured on 46k SPXL rows against feed deltas
(2026-08-15, |error| vs the feed's own greek):

    |delta| 0.00-0.10   median 0.0006    84% within 0.02
    |delta| 0.10-0.25   median 0.0034    99% within 0.02   <- short-put land
    |delta| 0.25-0.45   median 0.0105    81% within 0.02
    |delta| 0.45-0.60   median 0.0159    60% within 0.02
    |delta| 0.60-1.00   median ~0.031   ~28% within 0.02   <- see below

The delta-selling region this workspace actually researches is accurate to
~0.01, an order tighter than the page's own +-0.08 matching window. DEEP
IN-THE-MONEY IS SYSTEMATICALLY OFF by ~0.03 and cannot be fixed here: those
contracts are AMERICAN and carry early-exercise premium (worst at long
tenors, hence the 251+ DTE row), while every model in this codebase is
European. The rows are still written, because a Δ0.95 contract is never what
a constant-shape history matches on — but the number is a model's opinion
there, not the market's, and this is the paragraph that says so.
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve()
CODE = HERE.parents[1]
sys.path.insert(0, str(CODE))

from backend.bt.pricing import (  # noqa: E402
    black76_greeks, forward_and_discount, implied_vol)
from backend.opthist import db_path  # noqa: E402

YEAR = 365.0


def _mark(r: sqlite3.Row) -> float | None:
    """The price to price FROM. MID FIRST, settlement second — the workspace's
    standing mid rule, and measured: preferring `last` put the median error
    against real feed deltas at 0.037 because a last trade can be hours stale
    while the feed's own greek came from the mid. Settlement rows (the
    Databento backfill) carry no bid/ask at all and fall through to `last`,
    which for them IS the official mark."""
    bid, ask = r["bid"], r["ask"]
    if bid is not None and ask is not None and ask > 0 and bid >= 0:
        mid = 0.5 * (float(bid) + float(ask))
        if mid > 0:
            return mid
    last = r["last"]
    if last is not None and float(last) > 0:
        return float(last)
    return None


def solve_day(rows: list[sqlite3.Row]) -> list[tuple[float, float, float, str]]:
    """One (date, expiration) group -> [(iv, delta, strike, right)]. The table
    is WITHOUT ROWID, so updates key on its natural primary key. Rows without a
    usable price, or whose group has no forward, are skipped entirely: a
    delta invented from a broken forward is worse than a NULL that the page
    honestly cannot match."""
    by_strike: dict[float, dict[str, float]] = defaultdict(dict)
    for r in rows:
        price = _mark(r)
        if price is None:
            continue
        by_strike[r["strike"]][r["right"]] = price

    strikes, calls, puts = [], [], []
    for k in sorted(by_strike):
        pair = by_strike[k]
        if "C" in pair and "P" in pair:
            strikes.append(k)
            calls.append(pair["C"])
            puts.append(pair["P"])
    if len(strikes) < 3:
        return []
    try:
        F, D = forward_and_discount(strikes, calls, puts)
    except Exception:  # noqa: BLE001 — a bad day is skipped, never fatal
        return []
    if not (F and math.isfinite(F) and F > 0):
        return []
    if not (D and math.isfinite(D) and 0 < D <= 1.5):
        D = 1.0

    date = rows[0]["date"]
    exp = rows[0]["expiration"]
    try:
        T = (_ord(exp) - _ord(date)) / YEAR
    except ValueError:
        return []
    if T <= 0:
        return []

    # Each contract from its own mark. Sourcing IV from the OTM twin instead
    # (put-call parity says one vol serves the strike) was tried and MEASURED:
    # it changed nothing where it mattered and made 0-7 DTE worse, because the
    # model enforces parity internally — Δcall − Δput = D by construction — so
    # the two routes are the same arithmetic. That the FEED's deltas do not
    # satisfy that identity is the finding: its greeks come from an AMERICAN
    # model, whose early-exercise boundary a European Black-76 cannot
    # reproduce at any vol. Hence the deep-ITM residual documented above.
    out = []
    for r in rows:
        price = _mark(r)
        if price is None:
            continue
        is_call = r["right"] == "C"
        iv = implied_vol(price, F, float(r["strike"]), T, is_call, D)
        if not math.isfinite(iv):
            continue
        g = black76_greeks(F, float(r["strike"]), T, iv, is_call, D)
        d = g["delta"]
        if not math.isfinite(d):
            continue
        out.append((iv, d, float(r["strike"]), r["right"]))
    return out


def _ord(iso: str) -> int:
    y, m, d = (int(x) for x in iso[:10].split("-"))
    import datetime as dt
    return dt.date(y, m, d).toordinal()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--underlying", action="append", required=True,
                    help="e.g. SPXL, /MES (repeatable; slashless ok)")
    ap.add_argument("--check", action="store_true",
                    help="recompute rows that ALREADY have a feed delta and "
                         "report the error — writes nothing")
    ap.add_argument("--limit-days", type=int, default=0,
                    help="only the first N (date, expiration) groups")
    args = ap.parse_args()

    con = sqlite3.connect(db_path())
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=10000")

    for raw in args.underlying:
        u = raw.rsplit("/", 1)[-1].upper()
        u = f"/{u}" if raw.lstrip().startswith("/") or raw.startswith("C:") else u
        # accept both '/MES' and 'MES' for a futures root
        if not con.execute("SELECT 1 FROM hist_chain WHERE underlying=? LIMIT 1",
                           (u,)).fetchone():
            alt = f"/{u}" if not u.startswith("/") else u.lstrip("/")
            if con.execute("SELECT 1 FROM hist_chain WHERE underlying=? LIMIT 1",
                           (alt,)).fetchone():
                u = alt
        where = ("delta IS NOT NULL" if args.check else "delta IS NULL")
        groups = con.execute(
            f"SELECT date, expiration, COUNT(*) n FROM hist_chain"
            f" WHERE underlying=? AND {where}"
            f" GROUP BY date, expiration ORDER BY date, expiration", (u,)
        ).fetchall()
        if args.limit_days:
            groups = groups[:args.limit_days]
        print(f"{u}: {len(groups)} (date, expiration) groups to "
              f"{'check' if args.check else 'solve'}", flush=True)

        t0 = time.time()
        solved = skipped = 0
        errs: list[float] = []
        pending: list[tuple[float, float, int]] = []
        for i, g in enumerate(groups, 1):
            rows = con.execute(
                "SELECT date, expiration, strike, right, bid, ask, last,"
                " delta FROM hist_chain WHERE underlying=? AND date=? AND expiration=?",
                (u, g["date"], g["expiration"])).fetchall()
            res = solve_day(rows)
            if not res:
                skipped += g["n"]
                continue
            if args.check:
                have = {(float(r["strike"]), r["right"]): r["delta"]
                        for r in rows if r["delta"] is not None}
                for iv, d, k, rt in res:
                    if (k, rt) in have:
                        errs.append(abs(d - have[(k, rt)]))
                solved += len(res)
            else:
                pending.extend((iv, d, u, g["date"], g["expiration"], k, rt)
                               for iv, d, k, rt in res)
                solved += len(res)
                if len(pending) >= 50_000:
                    with con:
                        con.executemany(
                            "UPDATE hist_chain SET iv=?, delta=? WHERE underlying=?"
                            " AND date=? AND expiration=? AND strike=? AND right=?",
                            pending)
                    pending.clear()
            if i % 500 == 0:
                print(f"  {i}/{len(groups)} groups, {solved:,} rows, "
                      f"{time.time()-t0:.0f}s", flush=True)
        if pending:
            with con:
                con.executemany(
                    "UPDATE hist_chain SET iv=?, delta=? WHERE underlying=?"
                    " AND date=? AND expiration=? AND strike=? AND right=?", pending)

        if args.check and errs:
            errs.sort()
            n = len(errs)
            print(f"  CHECK vs feed deltas on {n:,} rows:")
            for p in (50, 80, 90, 95, 99):
                print(f"    p{p}: {errs[int(n * p / 100) - 1]:.4f}")
            print(f"    max: {errs[-1]:.4f}   mean: {sum(errs)/n:.4f}")
            within = sum(1 for e in errs if e <= 0.02) / n * 100
            print(f"    within 0.02 of the feed: {within:.1f}%")
        else:
            print(f"  {'checked' if args.check else 'wrote'} {solved:,} rows, "
                  f"skipped {skipped:,}, {time.time()-t0:.0f}s", flush=True)
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
