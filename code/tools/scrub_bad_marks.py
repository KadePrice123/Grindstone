"""Blank the greeks on archived rows whose MARK violates no-arbitrage.

Why greeks and not the price: the price is what the exchange published and
this archive's job is to record what a market showed, wrong prints included.
The DELTA is ours — solved — and a delta solved from a broken price is what
does the damage, because it looks ordinary and therefore WINS matches. Blank
it and the Opt page's constant-shape matcher simply skips that day's bad row
and takes the next best contract, which is the honest answer.

The rule (backend/greeks.suspect_marks): within one (date, expiration, right)
the prices are monotone in strike, so an INTERIOR mark above BOTH neighbours
cannot exist. Found in the /MES archive: a 2025-11-21 6690 put settled at
1700.25 between a 6650 at 374.00 and a 6700 at 407.75, implying ~102% vol in
a 13% neighbourhood and spiking the history chart to 25% of strike.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

CODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE))

from backend import greeks, opthist  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--underlying", action="append", required=True,
                    help="repeatable; slashless ok (MES == /MES)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(opthist.db_path())
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=10000")
    total_bad = 0
    for raw in args.underlying:
        u = raw.rsplit("/", 1)[-1].upper()
        for cand in (f"/{u}", u):
            if con.execute("SELECT 1 FROM hist_chain WHERE underlying=? LIMIT 1",
                           (cand,)).fetchone():
                u = cand
                break
        groups = con.execute(
            "SELECT date, expiration FROM hist_chain WHERE underlying=?"
            " GROUP BY date, expiration ORDER BY date", (u,)).fetchall()
        print(f"{u}: scanning {len(groups)} (date, expiration) groups", flush=True)
        t0 = time.time()
        bad_rows: list[tuple] = []
        for i, g in enumerate(groups, 1):
            rows = [dict(r) for r in con.execute(
                "SELECT date, expiration, strike, right, bid, ask, last, delta"
                " FROM hist_chain WHERE underlying=? AND date=? AND expiration=?",
                (u, g["date"], g["expiration"]))]
            for idx in greeks.suspect_marks(rows):
                r = rows[idx]
                if r.get("delta") is None:
                    continue          # already blank: nothing to undo
                bad_rows.append((u, r["date"], r["expiration"], r["strike"],
                                 r["right"]))
            if i % 2000 == 0:
                print(f"  {i}/{len(groups)}, {len(bad_rows):,} flagged, "
                      f"{time.time()-t0:.0f}s", flush=True)
        print(f"{u}: {len(bad_rows):,} rows carry a solved greek from a bad mark")
        total_bad += len(bad_rows)
        if bad_rows and not args.dry_run:
            with con:
                con.executemany(
                    "UPDATE hist_chain SET delta=NULL, iv=NULL WHERE underlying=?"
                    " AND date=? AND expiration=? AND strike=? AND right=?",
                    bad_rows)
            print(f"{u}: blanked {len(bad_rows):,} greeks", flush=True)
    con.close()
    print(f"TOTAL {'would blank' if args.dry_run else 'blanked'}: {total_bad:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
