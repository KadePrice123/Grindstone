"""Bulk-pull the purchased Databento history into data/databento/raw/.

Cart (Kade, 2026-08-15 — $125 account credits + $150 card):
- GLBX: futures daily bars for all 11 supplement roots + the COMPLETE option
  families of the micros he trades — MES (EX EX2 EX3 MES), MCL (MCO MW2 MW3),
  MGC (OMG G1M) — definition + statistics, full range (~$111, inside credits).
- OPRA: SPXL options through the COVID drawdown — 2020-01-01 to 2024-11-01,
  where the vault's own SPXL coverage begins — ohlcv-1d + statistics +
  definition (~$56, measured; equity options have no settlement schema, so
  closes + OI is what exists pre-2023).
The ES/NQ/CL/GC big-contract families are deliberately NOT here: ES alone is
$410 (21 weekly roots, measured); MNQ's family ($56) awaits more budget.

Safety: every slice is priced with the FREE metadata.get_cost call before it
is pulled, a running total is kept, and the pull ABORTS before any slice that
would push the total past --cap. Slices already on disk are skipped, so a
resumed run never pays twice.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import sys
import time
from pathlib import Path

import httpx


def _d(iso: str) -> _dt.datetime:
    return _dt.datetime.fromisoformat(iso[:10])

HERE = Path(__file__).resolve()
ENV = HERE.parents[2] / "env" / "databento.env"
RAW = HERE.parents[2] / "data" / "databento" / "raw"

BASE = "https://hist.databento.com/v0"
START_YEAR = 2010

FUT_ROOTS = ["ES", "NQ", "CL", "GC", "MES", "MNQ", "RTY", "MCL", "MGC", "ZB", "ZN"]
OPT_ROOTS = ["MES", "EX", "EX2", "EX3",        # micro S&P family
             "MCO", "MW2", "MW3",              # micro crude family
             "OMG", "G1M"]                     # micro gold family
# SPXL options: the COVID window, ending where the vault's coverage begins.
SPXL_START, SPXL_END = "2020-01-01", "2024-11-01"

# ---- quotes (added 2026-08-15, Kade: "that's a major hole") ---------------
# TBBO is the bid/ask AT EACH TRADE. The continuous quote feeds price out at
# $12.4k (bbo-1m) and $18.9k (mbp-1) for these same roots, so this is the only
# affordable real spread — and it covers exactly the contracts that actually
# traded, which is the population a fill can be assumed in anyway.
# ONLY THE DAYS WE LACK: the recorder has been capturing live two-sided
# quotes since it started, so each range ends where our own coverage begins.
QUOTE_PULLS = [
    # (dataset, symbols, schema, start, end)
    ("GLBX.MDP3", ["MES", "EX", "EX2", "EX3", "MCO", "MW2", "MW3", "OMG", "G1M"],
     "tbbo", "2010-06-06", "2026-08-13"),
    # OPRA carries NO quotes before 2023-03-28 at any price — the COVID window
    # simply has none to buy — and our own recorder took over 2025-07-30.
    ("OPRA.PILLAR", ["SPXL"], "tcbbo", "2023-03-28", "2025-07-30"),
]


def key() -> str:
    for line in ENV.read_text().splitlines():
        if line.startswith("DATABENTO_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"no DATABENTO_API_KEY in {ENV}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cap", type=float, default=180.0,
                    help="hard spend ceiling in USD (abort before exceeding; "
                         "$125 credits + card cover it, plan is ~$167)")
    ap.add_argument("--quotes", action="store_true",
                    help="also pull the trade-time bid/ask (QUOTE_PULLS)")
    ap.add_argument("--only-quotes", action="store_true",
                    help="pull ONLY QUOTE_PULLS — everything else is already "
                         "on disk and must not be re-priced or re-bought")
    ap.add_argument("--dry-run", action="store_true",
                    help="price every slice, download nothing")
    args = ap.parse_args()
    if args.only_quotes:
        args.quotes = True

    auth = (key(), "")
    RAW.mkdir(parents=True, exist_ok=True)
    spent = 0.0
    pulled = skipped = empty = 0

    # Asking past a dataset's live edge 422s the WHOLE request
    # (dataset_unavailable_range, measured) — clamp to each real end.
    ends: dict[str, str] = {}
    for ds in ("GLBX.MDP3", "OPRA.PILLAR"):
        r = httpx.get(f"{BASE}/metadata.get_dataset_range", auth=auth,
                      timeout=60, params={"dataset": ds})
        r.raise_for_status()
        # Second precision: the nanosecond form is accepted by get_cost but
        # untested on get_range — don't find out on a billed call.
        ends[ds] = r.json()["end"][:19] + "Z"
        print(f"dataset end {ds}: {ends[ds]}", flush=True)
    END = ends["GLBX.MDP3"]

    def get_cost(symbols: str, schema: str, start: str, end: str,
                 dataset: str = "GLBX.MDP3") -> float | None:
        """None = permanently unresolvable (422). Transient failures retry —
        NQ.FUT and CL.FUT were silently skipped by one flaky response each."""
        for attempt in (1, 2, 3):
            try:
                r = httpx.get(f"{BASE}/metadata.get_cost", auth=auth, timeout=90,
                              params={"dataset": dataset, "symbols": symbols,
                                      "schema": schema, "start": start,
                                      "end": end, "stype_in": "parent"})
            except httpx.HTTPError:
                time.sleep(10 * attempt)
                continue
            if r.status_code == 200:
                try:
                    return float(r.json())
                except ValueError:
                    return None
            if r.status_code == 422:
                return None
            time.sleep(10 * attempt)
        return None

    def pull(symbols: str, schema: str, start: str, end: str, dest: Path,
             dataset: str = "GLBX.MDP3") -> None:
        nonlocal spent, pulled, skipped, empty
        if dest.exists():
            skipped += 1
            return
        time.sleep(1.5)  # pace the gateway — it throttles hard when hammered
        cost = get_cost(symbols, schema, start, end, dataset)
        if cost is None:
            print(f"  SKIP (unresolved) {symbols} {schema} {start[:4]}", flush=True)
            return
        if cost == 0.0:
            empty += 1
            dest.with_suffix(dest.suffix + ".empty").touch()
            return
        if spent + cost > args.cap:
            print(f"ABORT: {symbols} {schema} {start[:4]} would cost ${cost:.2f} "
                  f"and push the total past ${args.cap:.2f} (at ${spent:.2f})",
                  flush=True)
            raise SystemExit(2)
        if args.dry_run:
            spent += cost
            print(f"  would pull {symbols:22s} {schema:10s} {start[:4]} ${cost:6.2f}",
                  flush=True)
            return
        t0 = time.time()
        status = None
        body = b""
        last_exc = ""
        for attempt in (1, 2, 3):
            try:
                with httpx.stream("GET", f"{BASE}/timeseries.get_range", auth=auth,
                                  timeout=httpx.Timeout(60.0, read=300.0),
                                  params={
                                      "dataset": dataset, "symbols": symbols,
                                      "stype_in": "parent", "schema": schema,
                                      "start": start, "end": end,
                                      "encoding": "csv", "compression": "zstd",
                                      "pretty_px": "true", "pretty_ts": "true",
                                      "map_symbols": "true"}) as r:
                    status = r.status_code
                    # The gateway streams with 206 Partial Content — success.
                    if status in (200, 206):
                        tmp = dest.with_suffix(".part")
                        with open(tmp, "wb") as f:
                            for chunk in r.iter_bytes():
                                f.write(chunk)
                        tmp.rename(dest)
                        spent += cost
                        pulled += 1
                        mb = dest.stat().st_size / 1e6
                        print(f"  pulled {symbols:22s} {schema:10s} {start[:4]} "
                              f"${cost:6.2f} {mb:8.1f} MB  {time.time()-t0:5.1f}s"
                              f"  (total ${spent:.2f})", flush=True)
                        return
                    body = r.read()[:150]
            except httpx.HTTPError as e:
                # Mid-stream disconnects are routine on a throttled gateway
                # (measured: 'peer closed connection ... incomplete chunked
                # read'). Clean the partial, back off, go again — a slice
                # only counts as spent when its file fully lands.
                last_exc = f"{e.__class__.__name__}: {e}"
                dest.with_suffix(".part").unlink(missing_ok=True)
                print(f"  retry {attempt} {symbols} {schema} {start[:4]} — "
                      f"{last_exc[:100]}", flush=True)
                time.sleep(20 * attempt)
                continue
            if status == 504:
                # Gateway timeout: too much resolution work in one window.
                # A 504 delivers nothing and bills nothing — halve and recurse
                # when the window is big enough to split, else brief backoff.
                days = (_d(end) - _d(start)).days
                if days > 500:
                    mid = (_d(start) + (_d(end) - _d(start)) / 2).strftime("%Y-%m-%d")
                    print(f"  504 on {symbols} {schema} {start[:7]}..{end[:7]} — "
                          f"splitting at {mid}", flush=True)
                    a = dest.with_name(dest.name.replace(".csv.zst", "a.csv.zst"))
                    b = dest.with_name(dest.name.replace(".csv.zst", "b.csv.zst"))
                    pull(symbols, schema, start, mid, a, dataset)
                    pull(symbols, schema, mid, end, b, dataset)
                    return
                time.sleep(20)
                continue
            break
        # repr() keeps the cp1252 console alive: error bodies can be binary.
        print(f"  FAILED {symbols} {schema} {start[:4]}: HTTP {status} "
              f"{body!r} {last_exc[:100]}", flush=True)

    # 1) futures daily bars, per root — the combined 11-parent request 504s
    # (measured); one root over 16 years answers in seconds.
    if not args.only_quotes:
        for root in FUT_ROOTS:
            pull(f"{root}.FUT", "ohlcv-1d", f"{START_YEAR}-06-06", END,
                 RAW / f"{root}_fut_ohlcv-1d.csv.zst")

    # 2) option families: per root x schema x year (resumable slices)
    for root in (() if args.only_quotes else OPT_ROOTS):
        for schema in ("definition", "statistics"):
            for year in range(START_YEAR, 2027):
                s = f"{year}-01-01" if year > START_YEAR else f"{year}-06-06"
                e = f"{year + 1}-01-01" if year < 2026 else END
                dest = RAW / f"{root}_{schema}_{year}.csv.zst"
                pull(f"{root}.OPT", schema, s, e, dest)

    # 3) SPXL options through COVID (OPRA), per-year slices
    opra_end = min(SPXL_END, ends["OPRA.PILLAR"])
    for schema in (() if args.only_quotes else ("definition", "ohlcv-1d", "statistics")):
        for year in range(2020, 2025):
            s = max(f"{year}-01-01", SPXL_START)
            e = min(f"{year + 1}-01-01", opra_end)
            if s >= e:
                continue
            pull("SPXL.OPT", schema, s, e,
                 RAW / f"SPXL_{schema}_{year}.csv.zst", dataset="OPRA.PILLAR")

    # 4) quotes, per root x year (resumable, and only where we lack our own)
    if args.quotes:
        for dataset, roots, schema, q0, q1 in QUOTE_PULLS:
            q1 = min(q1, ends[dataset])
            for root in roots:
                y0, y1 = int(q0[:4]), int(q1[:4])
                for year in range(y0, y1 + 1):
                    s = max(f"{year}-01-01", q0)
                    e = min(f"{year + 1}-01-01", q1)
                    if s >= e:
                        continue
                    pull(f"{root}.OPT", schema, s, e,
                         RAW / f"{root}_{schema}_{year}.csv.zst", dataset=dataset)

    print(f"\nDONE: pulled {pulled}, skipped-existing {skipped}, empty {empty}, "
          f"TOTAL SPENT ${spent:.2f} (cap ${args.cap:.2f})", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
