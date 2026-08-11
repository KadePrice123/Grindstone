"""Coverage: which periods we have, which are genuinely empty, which to retry.

DS-15. "Backfill the missing data" was not expressible before this module,
because nothing could answer *which days am I missing for SPY*. Row counts
cannot answer it: a day with no rows is either a day the market was shut or a
day we never asked about, and those demand opposite actions.

So coverage records a CLAIM per period, not a count:

    have     rows exist
    absent   an AUTHORITATIVE provider positively says there is nothing here
    failed   the request errored
    unknown  never asked

The load-bearing line is between `absent` and the other two. `absent` is a
provider asserting a fact about the world — a weekend, a holiday, a date
before the symbol listed — and it must suppress retries forever. `failed` and
`unknown` are facts about *us*, and must not. Collapse them and the backfill
either re-requests every market holiday until the end of time, or writes a
five-minute API outage down as "the market was closed" and never looks again.

Only a provider that is AUTHORITATIVE for a kind may claim `absent`
(DS-4): a provider that simply does not carry a symbol returns the same empty
list as a provider reporting a true holiday, and treating the first as a
positive claim silently blacklists data another source has.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Any, Iterable

# Providers entitled to say "there is genuinely nothing here", per kind. An
# empty result from anyone else is `unknown` — it may mean "I don't carry
# this name", which is not a fact about the market.
AUTHORITATIVE: dict[str, frozenset[str]] = {
    "bars": frozenset({"alpaca-iex", "alpaca-sip"}),
    "chain": frozenset({"alpaca-iex", "alpaca-sip"}),
}

# Providers that can EARN authority for a kind by evidence, rather than being
# granted it outright.
#
# OnclickMedia is the case. Its empty response means either "the market was
# shut that day" or "I do not carry this ticker" — indistinguishable on a
# single day, and it genuinely does not carry SPX, SPXW or XSP. Granting it
# blanket authority would let one empty response permanently blacklist a date
# for a symbol it simply never had. Refusing it any authority is the other
# failure: every market holiday inside its 180-day window would be re-asked
# forever, which is most of what a chain backfill would end up doing.
#
# The evidence that settles it is a day it DID return for this symbol. Once
# it has answered once, a later empty day is a statement about the market
# rather than about its catalogue.
EARNS_AUTHORITY: dict[str, frozenset[str]] = {
    "chain": frozenset({"onclick"}),
}

STATES = ("have", "absent", "failed", "unknown")
# States a backfill should try again. `absent` is deliberately not here.
RETRYABLE = ("failed", "unknown")
# Its complement, and the one gaps() actually queries. DERIVED rather than
# written out a second time: the two lists were independent, so a change to
# the policy above could leave the query untouched and the constant would go
# on documenting a rule the code no longer followed. A mutation test caught
# exactly that — editing RETRYABLE changed nothing at all.
SETTLED = tuple(s for s in STATES if s not in RETRYABLE)


def _iso(d: dt.date) -> str:
    return d.isoformat()


def trading_days(start: dt.date, end: dt.date) -> list[dt.date]:
    """Weekdays in [start, end]. Deliberately NOT a holiday calendar.

    A holiday looks exactly like a weekday we have no rows for, and the honest
    way to learn it is to ask once and record `absent` — which then suppresses
    the retry forever. Hardcoding a calendar would bake a second source of
    truth that goes stale every year and disagrees with the provider.
    """
    if end < start:
        return []
    out: list[dt.date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def mark(con: sqlite3.Connection, provider: str, kind: str, symbol: str,
         timeframe: str, period: str, state: str, *, rows: int = 0,
         detail: str = "") -> None:
    """Record one period's state. Last writer wins, by design: a retry that
    succeeds must be able to overwrite an earlier `failed`."""
    if state not in STATES:
        raise ValueError(f"unknown coverage state {state!r}")
    if state == "absent" and not may_claim_absent(
            con, provider, kind, symbol, timeframe):
        # Downgrade rather than raise. The caller is a collector in a loop and
        # an exception here would abandon the rest of the backfill over what
        # is really a provenance question — but the claim must not stand,
        # because `absent` is permanent and this provider has not earned it.
        state = "unknown"
        detail = (detail + " | " if detail else "") + \
            f"{provider} is not authoritative for {kind}; empty != absent"
    with con:
        con.execute(
            "INSERT INTO data_cover"
            " (provider, kind, symbol, timeframe, period, state, rows,"
            "  checked_at, detail)"
            " VALUES (?,?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%SZ','now'),?)"
            " ON CONFLICT(provider, kind, symbol, timeframe, period) DO UPDATE SET"
            "   state=excluded.state, rows=excluded.rows,"
            "   checked_at=excluded.checked_at, detail=excluded.detail",
            (provider, kind, symbol.upper(), timeframe, period, state, rows, detail))


def may_claim_absent(con: sqlite3.Connection, provider: str, kind: str,
                     symbol: str, timeframe: str = "") -> bool:
    """Is this provider entitled to say "there is genuinely nothing here"?

    Outright for a provider that is authoritative for the kind. Otherwise by
    EVIDENCE, for the providers listed in EARNS_AUTHORITY: one day it has
    already returned for this symbol proves it carries the symbol, and from
    then on an empty day is a fact about the market rather than about its
    catalogue.

    Deliberately per (provider, kind, SYMBOL): OnclickMedia carrying SPY says
    nothing about whether it carries XSP, and it does not.
    """
    if provider in AUTHORITATIVE.get(kind, frozenset()):
        return True
    if provider not in EARNS_AUTHORITY.get(kind, frozenset()):
        return False
    row = con.execute(
        "SELECT 1 FROM data_cover WHERE provider=? AND kind=? AND symbol=?"
        " AND timeframe=? AND state='have' LIMIT 1",
        (provider, kind, symbol.upper(), timeframe)).fetchone()
    return row is not None


def state_of(con: sqlite3.Connection, provider: str, kind: str, symbol: str,
             timeframe: str, period: str) -> str:
    r = con.execute(
        "SELECT state FROM data_cover WHERE provider=? AND kind=? AND symbol=?"
        " AND timeframe=? AND period=?",
        (provider, kind, symbol.upper(), timeframe, period)).fetchone()
    return r[0] if r else "unknown"


def gaps(con: sqlite3.Connection, provider: str, kind: str, symbol: str,
         timeframe: str, start: dt.date, end: dt.date,
         limit: int = 5000) -> list[str]:
    """The periods still worth asking about, oldest first.

    A period is worth asking about when it is retryable — never asked, or
    asked and errored. `have` and `absent` are both settled answers, and the
    whole point of separating them is that neither comes back here.
    """
    placeholders = ",".join("?" for _ in SETTLED)
    settled = {
        row[0] for row in con.execute(
            "SELECT period FROM data_cover WHERE provider=? AND kind=? AND"
            f" symbol=? AND timeframe=? AND state IN ({placeholders})"
            " AND period BETWEEN ? AND ?",
            (provider, kind, symbol.upper(), timeframe, *SETTLED,
             _iso(start), _iso(end)))
    }
    return [_iso(d) for d in trading_days(start, end)
            if _iso(d) not in settled][:limit]


def summary(con: sqlite3.Connection, kind: str, symbol: str,
            timeframe: str = "") -> dict[str, Any]:
    """What a UI needs to say "SPY 1Day: 2014-01-02 → today, 41 missing"."""
    rows = con.execute(
        "SELECT state, COUNT(*), MIN(period), MAX(period) FROM data_cover"
        " WHERE kind=? AND symbol=? AND timeframe=? GROUP BY state",
        (kind, symbol.upper(), timeframe)).fetchall()
    by_state = {r[0]: {"periods": r[1], "first": r[2], "last": r[3]} for r in rows}
    have = by_state.get("have", {})
    return {
        "symbol": symbol.upper(), "kind": kind, "timeframe": timeframe,
        "have": have.get("periods", 0),
        "absent": by_state.get("absent", {}).get("periods", 0),
        "failed": by_state.get("failed", {}).get("periods", 0),
        "first": have.get("first"), "last": have.get("last"),
        # Deliberately NOT "missing = expected - have": the expected count
        # depends on a date range the caller chooses, and reporting a number
        # derived from an unstated range is how a coverage display lies.
        "by_state": by_state,
    }


def reconcile_bars(con: sqlite3.Connection, provider: str, symbol: str,
                   timeframe: str) -> int:
    """Tell coverage about rows that are ALREADY on disk.

    Without this the first backfill re-fetches everything the live recorder
    has been collecting for weeks: coverage starts empty, an empty coverage
    means "never asked", and "never asked" is retryable. On this machine that
    was two thousand bars and eighty thousand chain rows already recorded
    against zero coverage rows.

    Only ever writes `have`, so it can never manufacture a false `absent` —
    a day missing from rec_bars stays unknown and the backfill goes and asks,
    which is exactly right.

    Idempotent, and cheap: one GROUP BY over the dates already stored.
    """
    rows = con.execute(
        "SELECT DISTINCT substr(ts, 1, 10) AS d FROM rec_bars"
        " WHERE symbol=? AND timeframe=?",
        (symbol.upper(), timeframe)).fetchall()
    n = 0
    for r in rows:
        day = r[0]
        if not day:
            continue
        mark(con, provider, "bars", symbol, timeframe, day, "have", rows=1,
             detail="reconciled from rows already recorded")
        n += 1
    return n


def record_bar_days(con: sqlite3.Connection, provider: str, symbol: str,
                    timeframe: str, asked: Iterable[dt.date],
                    got_days: Iterable[str]) -> dict[str, int]:
    """Reconcile one fetch: every asked day is `have` or, if the provider is
    authoritative, `absent`. Called with what was ASKED and what came BACK,
    because the days that came back cannot describe the days that did not."""
    got = {str(g)[:10] for g in got_days}
    counts = {"have": 0, "absent": 0, "unknown": 0}
    for d in asked:
        iso = _iso(d)
        if iso in got:
            mark(con, provider, "bars", symbol, timeframe, iso, "have", rows=1)
            counts["have"] += 1
        else:
            claim = "absent" if provider in AUTHORITATIVE["bars"] else "unknown"
            mark(con, provider, "bars", symbol, timeframe, iso, claim)
            counts[claim if claim in counts else "unknown"] += 1
    return counts
