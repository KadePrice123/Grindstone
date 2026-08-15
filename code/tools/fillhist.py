"""Fill MISSING days in options_history.db from the vault. Never wipes.

Why this exists next to loadhist.py: loadhist REBUILDS, and its first act is
`DELETE FROM hist_chain` (loadhist.py:222). That is correct for a rebuild and
catastrophic here — the archive now also holds purchased Databento history
(16M+ rows, real money) and the recorder's own captures, none of which the
vault can regenerate. So filling a gap needs a tool that only ever ADDS.

What it fills: the vault's daily chain snapshots for days the archive does
not already have. The case that prompted it — SPXL had a 272-day hole,
2024-10-31 to 2025-07-30, between where the Databento purchase stopped and
where the recorder started, and the vault covered exactly that window for
free (181 files).

Idempotent: opthist.append_day is INSERT OR REPLACE on the archive's own key,
and days already present are skipped before any parsing.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

CODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE))

from backend import opthist  # noqa: E402
from tools.loadhist import iter_files, parse_day  # noqa: E402


def existing_days(underlying: str) -> set[str]:
    con = sqlite3.connect(opthist.db_path())
    try:
        return {r[0] for r in con.execute(
            "SELECT DISTINCT date FROM hist_chain WHERE underlying=?",
            (underlying.upper(),))}
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", nargs="+", required=True,
                    help="underlyings to fill, e.g. SPXL TQQQ")
    ap.add_argument("--from", dest="lo", default="", help="YYYY-MM-DD inclusive")
    ap.add_argument("--to", dest="hi", default="", help="YYYY-MM-DD inclusive")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what WOULD be filled and stop")
    args = ap.parse_args()

    want = {s.upper() for s in args.symbols}
    have = {s: existing_days(s) for s in want}
    for s in want:
        print(f"{s}: archive already holds {len(have[s])} days", flush=True)

    todo: dict[str, list] = {s: [] for s in want}
    for sym, day, read in iter_files(want):
        s = sym.upper()
        if s not in want or day in have[s]:
            continue
        if args.lo and day < args.lo:
            continue
        if args.hi and day > args.hi:
            continue
        todo[s].append((day, read))

    for s in want:
        days = sorted(d for d, _ in todo[s])
        if not days:
            print(f"{s}: nothing missing in range — no work", flush=True)
            continue
        print(f"{s}: {len(days)} missing days to fill, {days[0]} .. {days[-1]}",
              flush=True)
    if args.dry_run:
        return 0

    t0 = time.time()
    for s in want:
        wrote = failed = 0
        for i, (day, read) in enumerate(sorted(todo[s]), 1):
            try:
                rows = [{"expiration": exp, "strike": k, "right": rt,
                         "bid": bid, "ask": ask, "last": last,
                         "iv": iv, "delta": dl,
                         "volume": vol, "open_interest": oi}
                        for (_d, exp, k, rt, bid, ask, last, iv, dl, vol, oi)
                        in parse_day(read())
                        if k is not None]
            except Exception as e:  # noqa: BLE001 — one bad file is not fatal
                failed += 1
                print(f"  {s} {day}: parse failed — {e.__class__.__name__}",
                      flush=True)
                continue
            if not rows:
                continue
            wrote += opthist.append_day(s, day, rows)
            if i % 25 == 0:
                print(f"  {s}: {i}/{len(todo[s])} days, {wrote:,} rows, "
                      f"{time.time()-t0:.0f}s", flush=True)
        print(f"{s}: filled {wrote:,} rows"
              f"{f', {failed} files unreadable' if failed else ''}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
