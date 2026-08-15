"""Import the Databento raw pull into the stores the app actually reads.

Sources (data/databento/raw/*.csv.zst, from databento_pull.py):
  <ROOT>_fut_ohlcv-1d  — per-contract futures dailies
  <ROOT>_definition_*  — option contract specs (instrument_id -> strike/exp/right)
  <ROOT>_statistics_*  — stat stream; stat_type 3 = settlement, 9 = open interest
  SPXL_ohlcv-1d_*      — per-contract daily closes (OPRA)

Targets:
  1. options_history.db hist_chain (the Opt page) via opthist.append_day —
     one EOD chain per (underlying, day). Futures options store settlement as
     `last` with bid/ask/iv/delta NULL (the snapshot carries none — honesty
     over invention). SPXL stores close as `last` + volume, OI where OPRA
     published it (dense only from 2023).
  2. data/backtest_data/<SYM>.db via btdata.import_chain (ChainRow shape,
     weekend rows dropped BEFORE the calendar guard sees them — a Sunday
     UTC-dated stat belongs to Monday's session and must not become a
     trading day).
  3. market.db rec_bars: '/ES' 1Day continuous series, volume-rolled front
     month per date — charts serve it as "your recorded data".

Idempotent: everything lands with INSERT OR REPLACE on natural keys.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import zstandard

HERE = Path(__file__).resolve()
CODE = HERE.parents[1]
PROJECT = HERE.parents[2]
RAW = PROJECT / "data" / "databento" / "raw"
sys.path.insert(0, str(CODE))

from backend import opthist  # noqa: E402
from backend import btdata  # noqa: E402
from backend.chainimport import ChainRow  # noqa: E402
from backend.marketdb import connect_market  # noqa: E402

# option product roots -> the underlying symbol the app knows
FAMILIES: dict[str, list[str]] = {
    "/MES": ["MES", "EX", "EX2", "EX3"],
    "/MCL": ["MCO", "MW2", "MW3"],
    "/MGC": ["OMG", "G1M"],
    "SPXL": ["SPXL"],
}
FUT_ROOTS = ["ES", "NQ", "CL", "GC", "MES", "MNQ", "RTY", "MCL", "MGC", "ZB", "ZN"]
STAT_SETTLEMENT, STAT_OI = "3", "9"


def zopen(path: Path):
    """Stream-decode a .csv.zst without holding the file in memory."""
    fh = open(path, "rb")
    reader = zstandard.ZstdDecompressor().stream_reader(fh)
    return io.TextIOWrapper(reader, encoding="utf-8", newline="")


def iter_rows(path: Path):
    with zopen(path) as f:
        yield from csv.DictReader(f)


def load_definitions(roots: list[str]) -> dict[str, dict]:
    """instrument_id -> {strike, expiration, right}. Later files win (a
    contract's definition is republished daily; the spec fields never change
    in a way we read)."""
    out: dict[str, dict] = {}
    for root in roots:
        for path in sorted(RAW.glob(f"{root}_definition_*.csv.zst")):
            for r in iter_rows(path):
                if r.get("instrument_class") not in ("C", "P"):
                    continue
                try:
                    strike = float(r["strike_price"])
                except (KeyError, ValueError):
                    continue
                exp = (r.get("expiration") or "")[:10]
                if not exp:
                    continue
                out[r["instrument_id"]] = {
                    "strike": strike, "expiration": exp,
                    "right": r["instrument_class"],
                }
    return out


def weekday(date: str) -> bool:
    return dt.date.fromisoformat(date).weekday() < 5


def import_family(underlying: str, roots: list[str], targets: set[str],
                  verbose: bool) -> dict:
    defs = load_definitions(roots)
    print(f"{underlying}: {len(defs)} option contracts defined", flush=True)

    # (date, iid) -> partial row; last write wins within a day
    days: dict[str, dict[str, dict]] = defaultdict(dict)

    for root in roots:
        for path in sorted(RAW.glob(f"{root}_statistics_*.csv.zst")):
            n = 0
            for r in iter_rows(path):
                iid = r.get("instrument_id")
                if iid not in defs:
                    continue  # futures/spreads share the stat stream
                st = r.get("stat_type")
                if st not in (STAT_SETTLEMENT, STAT_OI):
                    continue
                date = (r.get("ts_event") or "")[:10]
                if not date or not weekday(date):
                    continue
                slot = days[date].setdefault(iid, {})
                if st == STAT_SETTLEMENT:
                    try:
                        slot["settle"] = float(r.get("price") or "")
                    except ValueError:
                        pass
                else:
                    try:
                        slot["oi"] = float(r.get("quantity") or "")
                    except ValueError:
                        pass
                n += 1
            if verbose:
                print(f"  {path.name}: {n} stat rows kept", flush=True)
        # SPXL: closes ride ohlcv-1d, not settlements
        for path in sorted(RAW.glob(f"{root}_ohlcv-1d_*.csv.zst")):
            for r in iter_rows(path):
                iid = r.get("instrument_id")
                if iid not in defs:
                    continue
                date = (r.get("ts_event") or "")[:10]
                if not date or not weekday(date):
                    continue
                slot = days[date].setdefault(iid, {})
                try:
                    slot["settle"] = float(r.get("close") or "")
                except ValueError:
                    continue
                try:
                    slot["volume"] = float(r.get("volume") or "")
                except ValueError:
                    pass

    # emit per day, oldest first
    wrote_hist = wrote_bt = 0
    bt_con = None
    if "backtest" in targets:
        bt_con = btdata.connect_data(btdata.data_db_path(underlying))
    try:
        for date in sorted(days):
            contracts = []
            for iid, vals in days[date].items():
                if "settle" not in vals:
                    continue  # OI without a price is not a quotable row
                m = defs[iid]
                contracts.append({
                    "right": m["right"], "strike": m["strike"],
                    "expiration": m["expiration"],
                    "bid": None, "ask": None, "last": vals["settle"],
                    "iv": None, "delta": None,
                    "volume": vals.get("volume"),
                    "open_interest": vals.get("oi"),
                })
            if not contracts:
                continue
            if "opt" in targets:
                wrote_hist += opthist.append_day(underlying, date, contracts)
            if bt_con is not None:
                rows = [ChainRow(date=date, symbol=underlying.upper(),
                                 expiration=c["expiration"], strike=c["strike"],
                                 right=c["right"], bid=None, ask=None,
                                 last=c["last"], mark=c["last"],
                                 volume=c["volume"],
                                 open_interest=c["open_interest"],
                                 iv=None, delta=None)
                        for c in contracts]
                r = btdata.import_chain(bt_con, underlying.upper(), rows,
                                        source="databento")
                wrote_bt += r.get("rows", len(rows)) if isinstance(r, dict) else len(rows)
    finally:
        if bt_con is not None:
            bt_con.close()
    print(f"{underlying}: {len(days)} days — hist_chain rows {wrote_hist}, "
          f"backtest rows {wrote_bt}", flush=True)
    return {"days": len(days), "hist": wrote_hist, "bt": wrote_bt}


def import_futures_dailies() -> int:
    """Volume-rolled front-month continuous dailies -> rec_bars ('/ES', 1Day)."""
    con = connect_market()
    total = 0
    try:
        for root in FUT_ROOTS:
            path = RAW / f"{root}_fut_ohlcv-1d.csv.zst"
            if not path.exists():
                print(f"  {root}: no file, skipped", flush=True)
                continue
            by_date: dict[str, dict] = {}
            for r in iter_rows(path):
                sym = r.get("symbol") or ""
                # outright contracts only — spreads carry '-' in the symbol
                if "-" in sym:
                    continue
                date = (r.get("ts_event") or "")[:10]
                if not date or not weekday(date):
                    continue
                try:
                    vol = float(r.get("volume") or 0)
                except ValueError:
                    continue
                cur = by_date.get(date)
                if cur is None or vol > cur["v"]:
                    try:
                        by_date[date] = {
                            "o": float(r["open"]), "h": float(r["high"]),
                            "l": float(r["low"]), "c": float(r["close"]),
                            "v": vol,
                        }
                    except (KeyError, ValueError):
                        continue
            sym = f"/{root}"
            with con:
                con.executemany(
                    "INSERT OR REPLACE INTO rec_bars (symbol, timeframe, ts,"
                    " open, high, low, close, volume) VALUES (?,?,?,?,?,?,?,?)",
                    [(sym, "1Day", f"{d}T00:00:00Z", b["o"], b["h"], b["l"],
                      b["c"], b["v"]) for d, b in sorted(by_date.items())],
                )
            total += len(by_date)
            print(f"  {sym}: {len(by_date)} continuous dailies", flush=True)
    finally:
        con.close()
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--family", action="append",
                    help="limit to specific option families — '/MES' or 'MES' "
                         "(slashless survives Git Bash path mangling); "
                         f"known: {', '.join(sorted(FAMILIES))}")
    ap.add_argument("--targets", default="opt,backtest",
                    help="comma list of opt,backtest (chains); use 'opt' alone "
                         "to skip the engine stores")
    ap.add_argument("--no-bars", action="store_true",
                    help="skip the futures continuous dailies")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    targets = {t.strip() for t in args.targets.split(",") if t.strip()}
    if args.family:
        wanted = []
        for f in args.family:
            name = f.rsplit("/", 1)[-1].upper()  # tolerate MSYS path mangling
            match = next((k for k in FAMILIES
                          if k.lstrip("/").upper() == name), None)
            if match is None:
                ap.error(f"unknown family {f!r} — known: {', '.join(sorted(FAMILIES))}")
            wanted.append(match)
        args.family = wanted
    if not args.no_bars:
        print("== futures continuous dailies -> rec_bars ==", flush=True)
        n = import_futures_dailies()
        print(f"futures dailies total: {n}", flush=True)

    for underlying in (args.family or sorted(FAMILIES)):
        print(f"== {underlying} ({','.join(FAMILIES[underlying])}) ==", flush=True)
        import_family(underlying, FAMILIES[underlying], targets, args.verbose)
    print("IMPORT DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
