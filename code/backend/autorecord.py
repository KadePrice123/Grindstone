"""Auto-record favourites: starring a symbol starts recording it.

DS-17/18. Two rules do most of the work here, and both are about not
surprising the user.

**Un-starring never deletes data.** It stops the job and says so. Deleting
recorded data as a side effect of an unrelated click is not a thing this app
does — the user un-starred a shortcut, they did not ask to destroy months of
history they cannot re-fetch (OnclickMedia's window has already moved past
most of it, and Alpaca's free tier will not sell it back).

**"Starred, but not recordable, and why."** The legal domains genuinely
differ: favourites accept any `kind in ('symbol','page','web')` with no asset
filter, while `recorder.validate_job` refuses index outright, refuses futures
bars always and futures chains only without a TastyTrade account,
refuses crypto for bars and chains, and an unsynced symbol is a 422. So there
are symbols a user may legitimately star that cannot be recorded. The wrong
answers are refusing the star (the user wanted a shortcut, and the shortcut
works fine) and failing silently (they enabled a setting and reasonably
believe it is running). This module returns the reason instead, and the
caller surfaces it.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Callable

from . import recorder as recorder_mod
from .logs import LOG

SETTING = "autorecord_favorites"

# What a starred symbol gets recorded at. Chains are the expensive kind and
# the one the options work actually needs; daily bars are cheap and make the
# chart useful immediately. Deliberately conservative: this fires without the
# user choosing anything per-symbol, so it must not be the aggressive option.
PLAN: tuple[dict[str, Any], ...] = (
    {"kind": "bars", "timeframe": "1Day", "interval_seconds": 3600,
     "retention_days": 3650},
    {"kind": "chain", "timeframe": "", "interval_seconds": 900,
     "retention_days": 365},
)


def _existing(con: sqlite3.Connection, user_id: int, kind: str, symbol: str,
              timeframe: str) -> dict[str, Any] | None:
    r = con.execute(
        "SELECT id, enabled FROM record_jobs WHERE user_id=? AND kind=?"
        " AND symbol=? AND timeframe=?",
        (user_id, kind, symbol, timeframe)).fetchone()
    return {"id": r[0], "enabled": r[1]} if r else None


def recordability(symbol: str, asset_class: str | None, known: bool,
                  has_tasty: bool = False) -> tuple[bool, str]:
    """Can this symbol be recorded at all, and if not, why not — in the words
    the user should see. Pure, so the gate can table-test every asset class
    without a database or a network.

    ANY-of-PLAN semantics, not all-of: a futures root with a TastyTrade
    account can record its option chain but never bars (no OHLC source), and
    refusing the possible half because the impossible half exists would leave
    futures unrecordable forever."""
    if not known:
        if symbol.startswith("/"):
            # "Sync the universe" is a false promise here: the sync carries
            # no futures and the supplement roots are a built-in list.
            return False, (f"{symbol} is not in the built-in futures list, so "
                           f"recording cannot classify it — only the "
                           f"supplement roots are recordable")
        return False, (f"{symbol} is not in the synced universe yet, so there "
                       f"is nothing to record against — sync the universe and "
                       f"it will start on its own")
    errors: list[str] = []
    for spec in PLAN:
        err = recorder_mod.validate_job(
            spec["kind"], symbol, spec["timeframe"], spec["interval_seconds"],
            spec["retention_days"], asset_class, has_tasty=has_tasty)
        if err is None:
            return True, ""
        if err not in errors:
            errors.append(err)
    return False, "; ".join(errors)


def on_favorite_added(con: sqlite3.Connection, user_id: int, symbol: str,
                      asset_class: str | None, known: bool,
                      enabled: bool, has_tasty: bool = False) -> dict[str, Any]:
    """Star -> record. Returns what happened, always; the caller shows it.

    Never raises for an unrecordable symbol: the favourite itself is valid and
    has already been created by the time this runs. Refusing here would undo
    a thing the user successfully did.
    """
    if not enabled:
        return {"recording": False, "reason": "", "jobs": []}
    symbol = symbol.upper().strip()
    ok, why = recordability(symbol, asset_class, known, has_tasty=has_tasty)
    if not ok:
        # STARRED, BUT NOT RECORDABLE, AND WHY (DS-18).
        return {"recording": False, "reason": why, "jobs": []}

    created: list[dict[str, Any]] = []
    for spec in PLAN:
        # Per-spec validation: recordability said SOME plan entry works, not
        # all of them — a futures root records chains, never bars.
        if recorder_mod.validate_job(
                spec["kind"], symbol, spec["timeframe"], spec["interval_seconds"],
                spec["retention_days"], asset_class, has_tasty=has_tasty):
            continue
        have = _existing(con, user_id, spec["kind"], symbol, spec["timeframe"])
        if have:
            # Already recording. Re-enable if the user (or a previous
            # un-star) had switched it off, but never duplicate the job.
            if not have["enabled"]:
                with con:
                    con.execute("UPDATE record_jobs SET enabled=1 WHERE id=?",
                                (have["id"],))
                created.append({"id": have["id"], "kind": spec["kind"],
                                "action": "resumed"})
            continue
        # A futures chain snapshot is ~89 sequential TastyTrade requests
        # (measured on /ES) and futures trade ~23h/day — the equity default
        # of 15 minutes would be ~8,500 requests/day per starred root. Hourly
        # matches what the backtest setup wiring uses for chains.
        interval = spec["interval_seconds"]
        if spec["kind"] == "chain" and symbol.startswith("/"):
            interval = max(interval, 3600)
        with con:
            cur = con.execute(
                "INSERT INTO record_jobs (user_id, kind, symbol, timeframe,"
                " interval_seconds, retention_days) VALUES (?,?,?,?,?,?)",
                (user_id, spec["kind"], symbol, spec["timeframe"],
                 interval, spec["retention_days"]))
            created.append({"id": cur.lastrowid, "kind": spec["kind"],
                            "action": "started"})
    LOG.info("autorecord: %s -> %s", symbol,
             ", ".join(f"{c['kind']} {c['action']}" for c in created) or "already running")
    return {"recording": True, "reason": "", "jobs": created}


def on_favorite_removed(con: sqlite3.Connection, user_id: int, symbol: str,
                        enabled: bool) -> dict[str, Any]:
    """Un-star -> STOP recording. The data stays.

    DS-17 is explicit and it is the right call: removing the favourite must
    not delete recorded data. Months of chain snapshots cannot be re-fetched
    — OnclickMedia's window has moved past them and Alpaca does not sell the
    history back — so an accidental un-star would be unrecoverable. Disabling
    the job is reversible; deleting the rows is not.
    """
    if not enabled:
        return {"stopped": [], "kept_data": True}
    symbol = symbol.upper().strip()
    stopped: list[dict[str, Any]] = []
    for spec in PLAN:
        have = _existing(con, user_id, spec["kind"], symbol, spec["timeframe"])
        if have and have["enabled"]:
            with con:
                con.execute("UPDATE record_jobs SET enabled=0,"
                            " last_status='stopped: no longer a favourite'"
                            " WHERE id=?", (have["id"],))
            stopped.append({"id": have["id"], "kind": spec["kind"]})
    return {"stopped": stopped, "kept_data": True}


def sync_all(con: sqlite3.Connection, user_id: int, symbols: list[str],
             classify: Callable[[str], tuple[str | None, bool]],
             enabled: bool, has_tasty: bool = False) -> dict[str, Any]:
    """Bring every current favourite into line — what turning the setting ON
    should do, rather than only affecting symbols starred from then on. A
    setting that silently applies to future actions only is a setting the
    user believes is running when it is not.
    """
    if not enabled:
        return {"started": [], "skipped": []}
    started: list[str] = []
    skipped: list[dict[str, str]] = []
    for sym in symbols:
        asset_class, known = classify(sym)
        r = on_favorite_added(con, user_id, sym, asset_class, known, True,
                              has_tasty=has_tasty)
        if r["recording"]:
            started.append(sym.upper())
        else:
            skipped.append({"symbol": sym.upper(), "reason": r["reason"]})
    return {"started": started, "skipped": skipped}
