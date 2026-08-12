#!/usr/bin/env python3
"""Load the vault's daily option-chain archive into the app as recorded data.

Builds ``<data>/options_history.db`` from the workspace vault
(``projects/data/data/archive.zip`` + ``options_archive_new/``), producing the
two tables the Opt page's history side runs on:

  hist_chain       one row per contract per trading day, for the RECENT window
                   (default 12 months) — the price history of every contract
                   that is currently listed, since a contract's own life rarely
                   reaches further back than that.
  hist_spread_pct  bid-ask spread percentiles per (underlying, right, DTE,
                   |delta| bucket) over the ENTIRE archive — 16 years for SPY.
                   This is the fan chart's band: a band needs a distribution,
                   not 45 million raw rows, and this table is a few thousand.

Why this exists at all: Alpaca sells no historical option quotes on any plan
(verified 2026-08-06 — see RESEARCH.md), so the archive is the only source of
a spread through time. The archive itself is irreplaceable (the upstream went
paid in 2026-01) and is NEVER written by this tool.

Run:  python tools/loadhist.py            (from code/; SPY + every archived symbol)
      python tools/loadhist.py --months 6 --symbols SPY
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import io
import json
import re
import sqlite3
import sys
import time
import zipfile
from array import array
from collections import defaultdict
from pathlib import Path

CODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE))
from backend import datapaths  # noqa: E402
from backend.db import data_dir  # noqa: E402

# The vault resolves through datapaths — the uniform tree first, the legacy
# <workspace>/data/data second (labeled, so consolidation actually happens).
_VAULT, _VAULT_WHERE = datapaths.resolve_vault_dir()
VAULT = _VAULT if _VAULT is not None else datapaths.vault_dir()
ARCHIVE = VAULT / "archive.zip"
NEW_DIR = VAULT / "options_archive_new"

DB_PATH = data_dir() / "options_history.db"

# |delta| buckets — "similar contracts" for the band. Delta is the archive's own
# similarity measure (moneyness would need the underlying close, which the
# chain files do not carry). Rows with a missing/zero delta are skipped and
# counted: a contract whose delta is unknown cannot be pooled with anything.
BUCKETS = [(0.005, 0.05), (0.05, 0.15), (0.15, 0.25),
           (0.25, 0.35), (0.35, 0.50), (0.50, 1.01)]
MAX_DTE = 400

SCHEMA = """
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
CREATE TABLE IF NOT EXISTS hist_spread_pct (
    underlying TEXT NOT NULL,
    right      TEXT NOT NULL,
    dte        INTEGER NOT NULL,
    bucket     INTEGER NOT NULL,
    p10 REAL, p25 REAL, p50 REAL, p75 REAL, p90 REAL,
    n   INTEGER NOT NULL,
    PRIMARY KEY (underlying, right, dte, bucket)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS hist_meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
"""

DAY_RE = re.compile(r"_(\d{4}-\d{2}-\d{2})\.csv\.gz$")


def bucket_of(adelta: float) -> int | None:
    for i, (lo, hi) in enumerate(BUCKETS):
        if lo <= adelta < hi:
            return i
    return None


def iter_files(symbols: set[str] | None):
    """Every archive day file as (underlying, date, open()->bytes), zip then new/."""
    if ARCHIVE.exists():
        z = zipfile.ZipFile(ARCHIVE)
        for name in z.namelist():
            m = re.search(r"options_archive/([A-Z.]+)/\d{4}/[A-Z.]+_(\d{4}-\d{2}-\d{2})\.csv\.gz$", name)
            if not m:
                continue
            sym, day = m.group(1), m.group(2)
            if symbols and sym not in symbols:
                continue
            yield sym, day, (lambda n=name, zz=z: zz.read(n))
    if NEW_DIR.exists():
        for p in sorted(NEW_DIR.glob("*/*/*.csv.gz")):
            m = DAY_RE.search(p.name)
            if not m:
                continue
            sym = p.parent.parent.name
            if symbols and sym not in symbols:
                continue
            yield sym, m.group(1), (lambda pp=p: pp.read_bytes())


def parse_day(raw: bytes):
    """Rows of one day file as dicts of the columns we keep.

    The header is read per file, never assumed: the upstream switched column
    order around 2024-11, and a positional parse would have silently swapped
    fields across that boundary — the exact class of corruption a manifest
    cannot catch because the file itself is well-formed.
    """
    text = gzip.decompress(raw).decode("utf-8", errors="replace")
    lines = text.splitlines()
    if not lines:
        return
    head = lines[0].split(",")
    ix = {c: i for i, c in enumerate(head)}
    need = ("date", "expiration", "strike", "type", "bid", "ask")
    if any(c not in ix for c in need):
        raise ValueError(f"unrecognised header: {lines[0][:120]}")
    gi = {c: ix.get(c) for c in
          ("date", "expiration", "strike", "type", "bid", "ask", "last",
           "implied_volatility", "delta", "volume", "open_interest")}

    def num(parts, col):
        i = gi[col]
        if i is None or i >= len(parts):
            return None
        v = parts[i]
        if not v:
            return None
        try:
            return float(v)
        except ValueError:
            return None

    for ln in lines[1:]:
        p = ln.split(",")
        if len(p) < 6:
            continue
        right = "C" if p[gi["type"]].lower().startswith("c") else "P"
        yield (p[gi["date"]], p[gi["expiration"]], num(p, "strike"), right,
               num(p, "bid"), num(p, "ask"), num(p, "last"),
               num(p, "implied_volatility"), num(p, "delta"),
               num(p, "volume"), num(p, "open_interest"))


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover
        pass
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--months", type=int, default=12,
                    help="per-contract history window loaded into hist_chain (default 12)")
    ap.add_argument("--symbols", nargs="*", help="restrict to these underlyings")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--engine-db", nargs="?", const="APP", default=None, metavar="DIR",
                    help="ALSO write the BACKTEST ENGINE's store (data/backtest_data/"
                         "<SYM>.db). Bare flag uses the app's own location; give a "
                         "directory to put it somewhere that is not cloud-synced. "
                         "The engine reads only this store — options_history.db "
                         "feeds the Opt page and nothing else.")
    ap.add_argument("--engine-months", type=int, default=24,
                    help="window written to the engine store (default 24). The "
                         "engine's default warmup is 260 SESSIONS and high_252 "
                         "needs 252, so anything under ~18 months leaves the "
                         "gated rules permanently NaN — which reports zero "
                         "trades and no error at all.")
    ap.add_argument("--engine-symbols", nargs="*",
                    help="engine-store symbols (default: SPY, or --symbols)")
    args = ap.parse_args()

    syms = set(s.upper() for s in args.symbols) if args.symbols else None
    cutoff = (dt.date.today() - dt.timedelta(days=31 * args.months)).isoformat()

    # --- the engine store, optional and explicitly bounded --------------------
    engine_syms: set[str] = set()
    engine_cutoff = "9999-99-99"
    btdata = ChainRow = None
    engine_cons: dict[str, sqlite3.Connection] = {}
    engine_count: dict[str, int] = defaultdict(int)
    engine_days: dict[str, set] = defaultdict(set)
    if args.engine_db:
        sys.path.insert(0, str(CODE))
        from backend import btdata  # noqa: PLC0415 - optional, heavy-ish
        from backend.chainimport import ChainRow  # noqa: PLC0415
        engine_syms = set(s.upper() for s in (args.engine_symbols or args.symbols or ["SPY"]))
        if syms:
            engine_syms &= syms
        engine_cutoff = (dt.date.today()
                         - dt.timedelta(days=31 * args.engine_months)).isoformat()
        print(f"engine store: {', '.join(sorted(engine_syms))} from {engine_cutoff} "
              f"({args.engine_months} months)")

    if not ARCHIVE.exists() and not NEW_DIR.exists():
        sys.exit(f"no archive at {ARCHIVE} and no {NEW_DIR}")
    if _VAULT_WHERE == "legacy":
        print(f"NOTE: reading the LEGACY vault at {VAULT} — run "
              f"tools/consolidate.py --apply to move it into the uniform tree")

    con = sqlite3.connect(args.db)
    con.executescript(SCHEMA)
    con.execute("DELETE FROM hist_chain")
    con.execute("DELETE FROM hist_spread_pct")
    con.commit()

    # Percentile accumulators: array('f') per key keeps 45M floats at 4 bytes
    # each instead of 28 for a Python float — the difference between a pass
    # that fits in memory and one that does not.
    acc: dict[tuple, array] = defaultdict(lambda: array("f"))
    t0 = time.time()
    files = rows = kept_recent = skipped_delta = 0
    day_cache: dict[str, dt.date] = {}

    def d(iso: str) -> dt.date:
        v = day_cache.get(iso)
        if v is None:
            v = dt.date.fromisoformat(iso)
            day_cache[iso] = v
        return v

    batch: list[tuple] = []
    for sym, day, read in iter_files(syms):
        files += 1
        engine_batch: list = []
        want_engine = sym in engine_syms and day >= engine_cutoff
        try:
            for (date, exp, strike, right, bid, ask, last, iv, delta, vol, oi) in parse_day(read()):
                rows += 1
                if strike is None:
                    continue
                # ---- percentile pass: spread needs a two-sided market -------
                if bid is not None and ask is not None and bid > 0 and ask >= bid:
                    try:
                        dte = (d(exp) - d(date)).days
                    except ValueError:
                        continue
                    if 0 <= dte <= MAX_DTE:
                        adelta = abs(delta) if delta else 0.0
                        b = bucket_of(adelta)
                        if b is None:
                            skipped_delta += 1
                        else:
                            acc[(sym, right, dte, b)].append(ask - bid)
                # ---- recent window: the per-contract history -----------------
                if date >= cutoff:
                    kept_recent += 1
                    batch.append((sym, date, exp, strike, right,
                                  bid, ask, last, iv, delta, vol, oi))
                    if len(batch) >= 50_000:
                        con.executemany(
                            "INSERT OR REPLACE INTO hist_chain VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            batch)
                        con.commit()
                        batch.clear()
                # ---- the engine's own store ---------------------------------
                # Through btdata.import_chain rather than a hand-rolled INSERT,
                # so the vault takes exactly the same strike rounding, mark
                # fallback and NULL handling as the recorder and the uploader.
                # A second mapping here is a second chance to key contracts
                # the engine can never find again — which does not raise.
                if want_engine:
                    engine_batch.append(ChainRow(
                        date=date, symbol=sym, expiration=exp, strike=strike,
                        right=right, bid=bid, ask=ask, last=last, mark=None,
                        volume=vol, open_interest=oi, iv=iv, delta=delta))
            if engine_batch:
                ec = engine_cons.get(sym)
                if ec is None:
                    path = (btdata.data_db_path(sym) if args.engine_db == "APP"
                            else Path(args.engine_db) / f"{sym}.db")
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                    ec = engine_cons[sym] = btdata.connect_data(path)
                    print(f"  engine store for {sym}: {path}")
                btdata.import_chain(ec, sym, engine_batch, "vault")
                engine_count[sym] += len(engine_batch)
                engine_days[sym].add(day)
        except Exception as e:  # a corrupt day must not kill a 10k-file pass
            print(f"  SKIPPED {sym} {day}: {type(e).__name__}: {e}")
        if files % 500 == 0:
            print(f"  {files} files, {rows:,} rows, {time.time() - t0:.0f}s")
    if batch:
        con.executemany("INSERT OR REPLACE INTO hist_chain VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", batch)
        con.commit()

    print(f"parsed {files} files / {rows:,} rows in {time.time() - t0:.0f}s; "
          f"{kept_recent:,} recent rows kept; {skipped_delta:,} band rows skipped (no delta)")

    # ---- collapse the accumulators into the percentile table ---------------
    def q(sorted_a: array, frac: float) -> float:
        # nearest-rank with linear interpolation; the arrays are already sorted
        k = frac * (len(sorted_a) - 1)
        lo = int(k)
        hi = min(lo + 1, len(sorted_a) - 1)
        return sorted_a[lo] + (sorted_a[hi] - sorted_a[lo]) * (k - lo)

    out = []
    for (sym, right, dte, b), a in acc.items():
        if len(a) < 30:  # a band from a handful of observations is noise
            continue
        s = array("f", sorted(a))
        out.append((sym, right, dte, b,
                    q(s, 0.10), q(s, 0.25), q(s, 0.50), q(s, 0.75), q(s, 0.90), len(s)))
    con.executemany("INSERT OR REPLACE INTO hist_spread_pct VALUES (?,?,?,?,?,?,?,?,?,?)", out)
    meta = {
        "built_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "months": args.months,
        "files": files,
        "rows": rows,
        "recent_rows": kept_recent,
        "band_rows": len(out),
        "source": "vault options archive (daily EOD, OnclickMedia)",
        "buckets": json.dumps(BUCKETS),
    }
    con.executemany("INSERT OR REPLACE INTO hist_meta VALUES (?,?)",
                    list(meta.items()))
    con.commit()
    con.execute("VACUUM")
    con.close()
    size = args.db.stat().st_size / 1e6
    print(f"{args.db.name}: {kept_recent:,} history rows, {len(out):,} band rows, {size:.0f} MB")

    # ---- the engine store, reported in the terms that decide whether a
    #      backtest run on it will mean anything -----------------------------
    for sym, ec in sorted(engine_cons.items()):
        st = btdata.stats(ec)
        path = Path(ec.execute("PRAGMA database_list").fetchone()[2])
        ec.close()
        mb = path.stat().st_size / 1e6 if path.is_file() else 0
        print(f"{path.name}: {st['days']} sessions {st['first']} .. {st['last']}, "
              f"{engine_count[sym]:,} contracts, {mb:.0f} MB")
        # NOT a silent cap. The engine warms up over 260 sessions by default
        # and high_252 needs 252 of them; below that, every gated rule stays
        # NaN, NaN is falsey, and the run reports zero trades with no error —
        # which reads exactly like a strategy that simply never triggered.
        if st["days"] < 260:
            print(f"  WARNING: {st['days']} sessions is under the engine's 260-session "
                  f"warmup. Rules using sma_200 / high_252 / vix_rank will be NaN for "
                  f"the whole run and report zero trades WITHOUT an error. "
                  f"Re-run with --engine-months {max(24, args.engine_months * 2)}.")
        if mb > 400:
            print(f"  NOTE: {mb:.0f} MB inside a Drive-synced folder. It is derived and "
                  f"re-buildable; pass --engine-db <dir> to put it elsewhere and point "
                  f"Settings > backtest_options_db at it.")


if __name__ == "__main__":
    main()
