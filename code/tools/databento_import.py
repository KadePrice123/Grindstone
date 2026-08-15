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


def load_definitions(roots: list[str]) -> dict[str, list[tuple[str, dict]]]:
    """instrument_id -> [(published_on, contract), ...] sorted by date.

    POINT IN TIME, NOT LAST-WINS. The first version of this collapsed every
    definition into one dict where later files overwrote earlier ones, on the
    assumption that a republished definition says the same thing. CME REUSES
    instrument_ids: measured on this archive, 74 MES option ids (0.4%) name
    two entirely different contracts across 2020-2026 — id 339201 is a
    2020-12-18 3770 put and later a 2023-03-17 3390 call.

    The damage was not proportional to that 0.4%. Rebuilding 2024-03-05 from
    the raw files with point-in-time definitions yields ZERO puts for the
    2024-07-12 expiration, while the last-wins import wrote 27 — a chain with
    a 6900 put at 0.15 and a 4720 put at 1143.50 against a 5088 future. Whole
    groups were mislabelled, not stray rows, because a price is only as
    correct as the contract it is attached to.
    """
    out: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for root in roots:
        for path in sorted(RAW.glob(f"{root}_definition_*.csv.zst")):
            seen: set[tuple[str, str]] = set()
            for r in iter_rows(path):
                if r.get("instrument_class") not in ("C", "P"):
                    continue
                try:
                    strike = float(r["strike_price"])
                except (KeyError, ValueError):
                    continue
                exp = (r.get("expiration") or "")[:10]
                iid = r.get("instrument_id")
                when = (r.get("ts_event") or "")[:10]
                if not exp or not iid or not when:
                    continue
                # One row per (id, day): definitions republish every session
                # and only the first of a day carries new information.
                if (iid, when) in seen:
                    continue
                seen.add((iid, when))
                out[iid].append((when, {"strike": strike, "expiration": exp,
                                        "right": r["instrument_class"]}))
    for v in out.values():
        v.sort(key=lambda t: t[0])
    return dict(out)


def definition_on(defs: dict[str, list[tuple[str, dict]]], iid: str,
                  date: str) -> dict | None:
    """The contract this id named ON THAT DAY — the latest definition
    published at or before it, else the earliest known (a price can precede
    the first definition snapshot we hold, and that is still the same
    contract; what must never happen is borrowing a LATER id's meaning)."""
    hist = defs.get(iid)
    if not hist:
        return None
    best = None
    for when, meta in hist:
        if when <= date:
            best = meta
        else:
            break
    return best if best is not None else hist[0][1]


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
                date0 = (r.get("ts_event") or "")[:10]
                meta = definition_on(defs, iid, date0)
                if meta is None:
                    continue  # futures/spreads share the stat stream
                st = r.get("stat_type")
                if st not in (STAT_SETTLEMENT, STAT_OI):
                    continue
                date = (r.get("ts_event") or "")[:10]
                if not date or not weekday(date):
                    continue
                slot = days[date].setdefault(iid, {})
                # THE CONTRACT IS RESOLVED HERE, where the date is known, and
                # travels with the row. Looking it up again at emit time is
                # what re-introduces the last-wins bug by the back door.
                slot["meta"] = meta
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
                date = (r.get("ts_event") or "")[:10]
                if not date or not weekday(date):
                    continue
                meta = definition_on(defs, iid, date)
                if meta is None:
                    continue
                slot = days[date].setdefault(iid, {})
                slot["meta"] = meta
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
                m = vals.get("meta")
                if m is None:
                    continue
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
