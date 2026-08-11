"""Backfill: fetch the periods coverage says are missing.

DS-16. A paced, resumable, finite work list — deliberately all four:

- **finite**, because a backfill that cannot say how much is left is a
  progress bar that only goes up;
- **paced**, because the free Alpaca tier is 200 requests/minute shared with
  every interactive chart the user is looking at, and a backfill that starves
  the UI is worse than one that takes an hour;
- **resumable**, because the process WILL be killed mid-run — this one is
  designed to be launched by an unattended recorder — and restarting from
  zero each time means the tail never lands;
- **a work list**, so it can be inspected before it runs.

Resumability is free here: the plan is recomputed from `data_cover` every
time, so a period already marked `have` or `absent` is simply not in the next
plan. There is no cursor to corrupt and no queue to go stale.

**The OnclickMedia window is recomputed at EXECUTION time, never at plan
time.** Its window moves one day per day (`onclick.py`), so a plan built on
Monday contains items that have fallen outside the window by the time they
run on Thursday — a plan that "succeeded" while silently fetching nothing.
`plan()` therefore records the range it wants, and `run_one()` re-clamps
immediately before the call and reports the clamp instead of swallowing it.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import time
from typing import Any, Callable

from . import coverage as cov
from . import onclick
from .logs import LOG

# Alpaca's free tier is 200 req/min SHARED with everything the user is doing.
# A backfill is by definition not urgent; the interactive path is.
DEFAULT_PACE_S = 1.5
# A single call's span. Alpaca serves a date range in one request, so asking
# day-by-day would multiply the request count by ~250 for no benefit.
CHUNK_DAYS = 30
# Ceiling on one run's work, so a decade-wide gap cannot monopolise the
# recorder thread. The next run picks up exactly where this one stopped —
# that is what recomputing the plan from coverage buys.
MAX_CHUNKS_PER_RUN = 40


class Chunk:
    """One unit of work: a contiguous date range for one (provider, kind,
    symbol, timeframe). Carries the range it WANTS; what it actually asks for
    is decided at execution."""

    __slots__ = ("provider", "kind", "symbol", "timeframe", "start", "end", "days")

    def __init__(self, provider: str, kind: str, symbol: str, timeframe: str,
                 start: dt.date, end: dt.date, days: list[str]) -> None:
        self.provider = provider
        self.kind = kind
        self.symbol = symbol
        self.timeframe = timeframe
        self.start = start
        self.end = end
        self.days = days

    def as_dict(self) -> dict[str, Any]:
        return {"provider": self.provider, "kind": self.kind,
                "symbol": self.symbol, "timeframe": self.timeframe,
                "start": self.start.isoformat(), "end": self.end.isoformat(),
                "days": len(self.days)}


def plan(con: sqlite3.Connection, provider: str, kind: str, symbol: str,
         timeframe: str, start: dt.date, end: dt.date,
         chunk_days: int = CHUNK_DAYS, max_chunks: int = MAX_CHUNKS_PER_RUN,
         ) -> list[Chunk]:
    """The work list: contiguous runs of missing days, oldest first.

    Recomputed from coverage on every call — that IS the resume mechanism.
    Truncated to `max_chunks`, and the truncation is visible in the length
    rather than silent, because a caller that believes it received the whole
    plan will report "backfill complete" while most of it was never returned.
    """
    missing = cov.gaps(con, provider, kind, symbol, timeframe, start, end)
    if not missing:
        return []
    dates = [dt.date.fromisoformat(d) for d in missing]
    chunks: list[Chunk] = []
    run: list[dt.date] = [dates[0]]
    for d in dates[1:]:
        # Break a chunk when it reaches the span cap, or when the gap to the
        # next missing day is wide enough that asking across it would spend
        # the request on days already settled.
        if (d - run[0]).days >= chunk_days or (d - run[-1]).days > 5:
            chunks.append(_chunk(provider, kind, symbol, timeframe, run))
            run = [d]
        else:
            run.append(d)
    chunks.append(_chunk(provider, kind, symbol, timeframe, run))
    return chunks[:max_chunks]


def _chunk(provider: str, kind: str, symbol: str, timeframe: str,
           days: list[dt.date]) -> Chunk:
    return Chunk(provider, kind, symbol, timeframe, days[0], days[-1],
                 [d.isoformat() for d in days])


def remaining(con: sqlite3.Connection, provider: str, kind: str, symbol: str,
              timeframe: str, start: dt.date, end: dt.date) -> int:
    """How many periods are still unsettled — the honest denominator. Counted
    from coverage rather than from the truncated plan, so a run that is
    capped at MAX_CHUNKS still reports the real size of the job."""
    return len(cov.gaps(con, provider, kind, symbol, timeframe, start, end,
                        limit=10_000_000))


def run_one(con: sqlite3.Connection, chunk: Chunk,
            fetch: Callable[[str, str, dt.date, dt.date], list[dict[str, Any]]],
            today: dt.date | None = None) -> dict[str, Any]:
    """Execute one chunk and record what it learned.

    `fetch(symbol, timeframe, start, end) -> rows`, each row carrying a `ts`.
    Raising is fine: the days are marked `failed`, which is RETRYABLE, so the
    next plan includes them again. That is the difference between a provider
    outage and a market holiday, and it is the reason this module exists.
    """
    start, end = chunk.start, chunk.end
    note = ""

    # THE WINDOW IS RECOMPUTED HERE, not when the plan was built. OnclickMedia's
    # window slides one day per day, so a plan made on Monday contains items
    # that are outside it by Thursday. Clamping at execution turns that from a
    # silent no-op into a reported clamp.
    if chunk.provider == "onclick":
        start, end, note = onclick.clamp(start, end, today)
        if end < start:
            # Entirely outside the window now. NOT a failure and NOT absence:
            # this provider cannot answer, which says nothing about the data.
            for d in chunk.days:
                cov.mark(con, chunk.provider, chunk.kind, chunk.symbol,
                         chunk.timeframe, d, "unknown",
                         detail="outside the provider's window at execution time")
            return {"chunk": chunk.as_dict(), "rows": 0, "state": "skipped",
                    "note": note or "the whole range fell outside the window"}

    try:
        rows = fetch(chunk.symbol, chunk.timeframe, start, end)
    except Exception as e:  # noqa: BLE001 — the reason is the product here
        LOG.warning("backfill %s %s %s %s..%s failed: %s", chunk.provider,
                    chunk.symbol, chunk.timeframe, start, end, e)
        for d in chunk.days:
            cov.mark(con, chunk.provider, chunk.kind, chunk.symbol,
                     chunk.timeframe, d, "failed", detail=str(e)[:200])
        return {"chunk": chunk.as_dict(), "rows": 0, "state": "failed",
                "note": str(e)[:200]}

    got = {str(r.get("ts", ""))[:10] for r in rows}
    counts = {"have": 0, "absent": 0, "unknown": 0}
    for d in chunk.days:
        if d in got:
            cov.mark(con, chunk.provider, chunk.kind, chunk.symbol,
                     chunk.timeframe, d, "have", rows=1)
            counts["have"] += 1
        else:
            # Nothing came back for this day. Only an AUTHORITATIVE provider
            # may turn that into a permanent `absent`; for anyone else it
            # stays retryable, because "I don't carry this name" and "the
            # market was shut" are the same empty list on the wire.
            claim = "absent" if chunk.provider in cov.AUTHORITATIVE.get(
                chunk.kind, frozenset()) else "unknown"
            cov.mark(con, chunk.provider, chunk.kind, chunk.symbol,
                     chunk.timeframe, d, claim)
            counts[claim] += 1
    return {"chunk": chunk.as_dict(), "rows": len(rows), "state": "ok",
            "note": note, **counts}


def run(con: sqlite3.Connection, chunks: list[Chunk],
        fetch: Callable[[str, str, dt.date, dt.date], list[dict[str, Any]]],
        pace_s: float = DEFAULT_PACE_S, today: dt.date | None = None,
        should_stop: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Work the list, paced, stopping cleanly when asked.

    A stop is not a failure: every chunk already done stayed done, and the
    next plan simply will not contain it."""
    results: list[dict[str, Any]] = []
    for i, ch in enumerate(chunks):
        if should_stop and should_stop():
            return {"done": len(results), "planned": len(chunks),
                    "stopped": True, "results": results}
        if i and pace_s > 0:
            time.sleep(pace_s)
        results.append(run_one(con, ch, fetch, today))
    return {"done": len(results), "planned": len(chunks),
            "stopped": False, "results": results}
