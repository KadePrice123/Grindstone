#!/usr/bin/env python3
"""Bake the ALL-REGIME insurance expectancy from the deep chain store.

The Insure page's runtime sweep measures the recent archive — ~12 months, one
regime, no crash. THIS tool runs the SAME engine (backend/insurance.py) over
the deep per-symbol chain store (spy_options.db: 2008→present for SPY, 25.7M
rows, delta on every row) and writes the result into options_history.db as
``hist_expectancy``, which the runtime PREFERS. The fair line that remembers
2008 and 2020 (docs/INSURE.md v1.1).

SETTLEMENT COMES FROM THE CHAIN ITSELF: the daily spot is recovered by
put-call parity at the strike where call mid == put mid (S = K + C − P).
Deliberately not an external bar series — adjusted closes drift from strike
space by a decade of dividends, and the chain cannot disagree with itself.
The engine's own DTE-0 self-check then cross-examines these spots against
expiration-day mids, so a parity failure cannot pass silently.

Run:  python tools/bakeexpectancy.py                 (SPY from ../spy_options.db)
      python tools/bakeexpectancy.py --store X.db --symbol USO
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
import time
from pathlib import Path

CODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE))
from backend import insurance  # noqa: E402

APP_DATA = CODE.parent / "data"
DB_PATH = APP_DATA / "options_history.db"
CHUNK = 250_000


def iso(d: int) -> str:
    s = str(d)
    return f"{s[:4]}-{s[4:6]}-{s[6:]}"


def parity_spots(con: sqlite3.Connection) -> dict[str, float]:
    """One spot per day: S = K* + C(K*) − P(K*) at the strike where the
    nearest-expiry call and put mids cross. ~4,600 tiny PK-indexed queries."""
    days = [r[0] for r in con.execute("SELECT DISTINCT d FROM opt ORDER BY d")]
    closes: dict[str, float] = {}
    for day in days:
        row = con.execute(
            """SELECT c.strike, (c.bid+c.ask)/2.0, (p.bid+p.ask)/2.0
               FROM opt c JOIN opt p
                 ON p.d=c.d AND p.exp=c.exp AND p.strike=c.strike AND p.cp=1
               WHERE c.d=? AND c.cp=0
                 AND c.bid>0 AND c.ask>=c.bid AND p.bid>0 AND p.ask>=p.bid
                 AND c.exp=(SELECT MIN(exp) FROM opt WHERE d=? AND exp>=d)
               ORDER BY ABS((c.bid+c.ask)/2.0-(p.bid+p.ask)/2.0) LIMIT 1""",
            (day, day)).fetchone()
        if row:
            k, cm, pm = row
            closes[iso(day)] = k / 1000.0 + cm - pm
    return closes


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--store", type=Path, default=CODE.parent.parent / "spy_options.db",
                    help="deep chain store (default ../../spy_options.db)")
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--db", type=Path, default=DB_PATH,
                    help="options_history.db to bake into")
    args = ap.parse_args()
    sym = args.symbol.upper()

    if not args.store.exists():
        sys.exit(f"no deep store at {args.store}")
    if not args.db.exists():
        sys.exit(f"no {args.db} — run loadhist.py first; the bake rides beside it")

    src = sqlite3.connect(f"file:{args.store.as_posix()}?mode=ro", uri=True)
    t0 = time.time()
    print(f"parity spots for {sym}…")
    closes = parity_spots(src)
    print(f"  {len(closes)} sessions {min(closes)} .. {max(closes)}"
          f" in {time.time() - t0:.0f}s")

    # Stream puts in chunks through the SAME trials() the runtime sweep uses.
    today = dt.date.today().isoformat()
    agg = insurance.new_agg()
    cur = src.execute(
        "SELECT d, exp, strike, bid, ask, delta FROM opt WHERE cp=1")
    n = kept = 0
    selfcheck_checks = selfcheck_viol = 0
    buf: list[dict] = []

    def flush() -> None:
        nonlocal kept
        ts = insurance.trials(buf, closes, today=today)
        kept += len(ts)
        insurance.fold_trials(agg, ts)
        buf.clear()

    for d_i, exp_i, strike_i, bid, ask, delta in cur:
        n += 1
        d_s, exp_s = iso(d_i), iso(exp_i)
        k = strike_i / 1000.0
        # The DTE-0 self-check, inline (trials() drops DTE-0 before classing).
        if d_i == exp_i and bid is not None and ask is not None and 0 < bid <= ask:
            s = closes.get(d_s)
            if s is not None:
                selfcheck_checks += 1
                if abs((bid + ask) / 2 - max(0.0, k - s)) > max(1.0, 0.02 * k):
                    selfcheck_viol += 1
        buf.append({"date": d_s, "expiration": exp_s, "strike": k,
                    "bid": bid, "ask": ask, "delta": delta})
        if len(buf) >= CHUNK:
            flush()
            if n % 2_000_000 < CHUNK:
                print(f"  {n:,} rows, {kept:,} trials, {time.time() - t0:.0f}s")
    flush()
    src.close()

    suspect = selfcheck_checks >= 20 and selfcheck_viol > 0.05 * selfcheck_checks
    print(f"{n:,} put rows -> {kept:,} trials in {time.time() - t0:.0f}s; "
          f"selfcheck {selfcheck_viol}/{selfcheck_checks}"
          f" ({'SUSPECT — NOT BAKING' if suspect else 'ok'})")
    if suspect:
        sys.exit("the parity spots disagree with expiration-day mids — refusing "
                 "to bake numbers built on them")

    classes = insurance.finish_stats(agg)
    payload = {
        "classes": classes,
        "n_rows": n,
        "n_trials": kept,
        "selfcheck": {"checks": selfcheck_checks, "violations": selfcheck_viol,
                      "suspect": False},
        "window": {"first": min(closes), "last": max(closes)},
        "settle_sources": "put-call parity (the chain itself)",
        "regimes": "includes 2008 and 2020" if min(closes) <= "2009" else "",
    }

    out = sqlite3.connect(args.db)
    out.execute("""CREATE TABLE IF NOT EXISTS hist_expectancy (
        underlying  TEXT PRIMARY KEY,
        computed_at TEXT NOT NULL,
        payload     TEXT NOT NULL)""")
    with out:
        out.execute(
            "INSERT OR REPLACE INTO hist_expectancy VALUES (?,?,?)",
            (sym, dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             json.dumps(payload)))
    out.close()

    solid = sum(1 for c in classes.values() if c.get("n_exp", 0) >= 20)
    print(f"baked {sym}: {len(classes)} classes ({solid} solid), "
          f"window {payload['window']['first']} .. {payload['window']['last']}")
    for key in sorted(classes):
        c = classes[key]
        if not c.get("n_exp"):
            continue
        need = c.get("expected_loss_pct")
        print(f"  {key:<24} n_exp {c['n_exp']:>4}  claim {c['claim_freq']*100:5.1f}%"
              f"  need {(need * 100 if need is not None else float('nan')):6.3f}%"
              f"  worst {(c['severity']['worst'] * 100 if c.get('severity') else 0):5.1f}%"
              f" ({c['severity']['worst_date'] if c.get('severity') else '—'})")


if __name__ == "__main__":
    main()
