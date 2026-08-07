"""Background data jobs: importing a file, and pulling chains slowly.

Both are long enough that they cannot run inside a request. The IPC bridge
imposes a 30-second deadline, and DATA_IMPORT.md's all-or-nothing rule makes a
timeout mid-commit the worst possible outcome — it looks like success and
leaves a gap nobody can see. So both are start-then-poll, modelled on
BacktestManager.sync_now: a lock that closes the double-spawn gap, a daemon
thread, and failures that land in a status dict rather than as an exception in
a request thread.

One job of each kind at a time, deliberately. Two concurrent importers writing
the same (d, exp, cp, strike) rows would interleave two files into one chain
with no error anywhere.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from pathlib import Path
from typing import Any

from . import btdata, chainimport, onclick

LOG = logging.getLogger("grindstone")


class DataJobs:
    """Owns the import thread and the puller thread, and their status."""

    def __init__(self) -> None:
        self._import_lock = threading.Lock()
        self._import_thread: threading.Thread | None = None
        self._import: dict[str, Any] = {"state": "idle"}

        self._pull_lock = threading.Lock()
        self._pull_thread: threading.Thread | None = None
        self._pull: dict[str, Any] = {"state": "idle"}
        self._pull_stop = threading.Event()

    # ------------------------------------------------------------- import
    @property
    def import_status(self) -> dict[str, Any]:
        return dict(self._import)

    def start_import(self, path: str, kind: str, underlying: str) -> bool:
        """False when one is already running — the caller reports that rather
        than quietly queueing a second writer."""
        with self._import_lock:
            if self._import_thread is not None and self._import_thread.is_alive():
                return False
            self._import = {"state": "running", "file": Path(path).name,
                            "kind": kind, "underlying": underlying.upper()}

            def run() -> None:
                try:
                    # PARSE FULLY FIRST. Nothing is written until the whole
                    # file has validated; a half-imported file is the outcome
                    # the spec names as worst, because it reads as success.
                    parsed = chainimport.read_path(path, kind)
                    con = btdata.connect_data(btdata.data_db_path(underlying))
                    try:
                        if kind == "option_chain":
                            res = btdata.import_chain(
                                con, underlying, parsed.chain, parsed.source,
                                progress=lambda i, n: self._import.update(
                                    {"done": i, "total": n}))
                        else:
                            res = btdata.import_bars(
                                con, underlying, parsed.bars, parsed.source)
                        stats = btdata.stats(con)
                    finally:
                        con.close()
                    self._import = {"state": "done", "file": Path(path).name,
                                    "underlying": underlying.upper(),
                                    **res, "store": stats}
                    LOG.info("import %s -> %s: %s", path, underlying, res)
                except (chainimport.Refused, btdata.ImportRefused) as e:
                    # A refusal is an ANSWER, not a crash: the user gets the
                    # reason and the row, and nothing was written.
                    self._import = {"state": "refused", "file": Path(path).name,
                                    "reason": str(e)}
                    LOG.info("import refused (%s): %s", path, e)
                except Exception as exc:  # noqa: BLE001 — report, never crash
                    LOG.exception("import failed")
                    self._import = {"state": "error", "file": Path(path).name,
                                    "error": str(exc)}

            self._import_thread = threading.Thread(target=run, daemon=True,
                                                   name="data-import")
            self._import_thread.start()
            return True

    # --------------------------------------------------------------- pull
    @property
    def pull_status(self) -> dict[str, Any]:
        lo, hi = onclick.window()
        return {**self._pull, "window_from": lo.isoformat(),
                "window_to": hi.isoformat()}

    def stop_pull(self) -> bool:
        if self._pull_thread is not None and self._pull_thread.is_alive():
            self._pull_stop.set()
            return True
        return False

    def start_pull(self, symbols: list[str], start: str, end: str,
                   delay: float = 6.0) -> tuple[bool, str]:
        with self._pull_lock:
            if self._pull_thread is not None and self._pull_thread.is_alive():
                return False, "a pull is already running"
            try:
                s0 = dt.date.fromisoformat(start)
                e0 = dt.date.fromisoformat(end)
            except ValueError:
                return False, "dates must be YYYY-MM-DD"
            syms = [s.strip().upper() for s in symbols if s.strip()]
            if not syms:
                return False, "name at least one ticker"
            s, e, note = onclick.clamp(s0, e0)
            if s > e:
                lo, hi = onclick.window()
                return False, (f"nothing to fetch: the free plan only serves "
                               f"{lo} to {hi}")
            days = onclick.trading_days(s, e)
            total = len(days) * len(syms)
            # The pace the user asked for, and the floor that protects a free
            # service the archive depends on.
            delay = max(onclick.MIN_DELAY, min(float(delay), onclick.MAX_DELAY))
            self._pull_stop.clear()
            self._pull = {"state": "running", "symbols": syms, "from": s.isoformat(),
                          "to": e.isoformat(), "total": total, "done": 0,
                          "written": 0, "skipped": 0, "note": note, "delay": delay,
                          "eta_seconds": int(total * delay)}

            def run() -> None:
                written = skipped = done = 0
                cons: dict[str, Any] = {}
                try:
                    for sym in syms:
                        for day in days:
                            if self._pull_stop.is_set():
                                self._pull = {**self._pull, "state": "stopped",
                                              "done": done, "written": written,
                                              "skipped": skipped}
                                return
                            iso = day.isoformat()
                            try:
                                body = onclick.fetch_day(sym, iso)
                            except PermissionError as e:
                                # The window moved under us. Not a failure of
                                # this day so much as of the range.
                                skipped += 1
                                self._pull = {**self._pull, "last": f"{sym} {iso}: {e}"}
                                body = None
                            if body is onclick.MISMATCH or body == onclick.MISMATCH:
                                skipped += 1
                                self._pull = {**self._pull, "last": (
                                    f"{sym} {iso}: columns differ from the archive "
                                    f"(an open session has no greeks) — not stored")}
                            elif body:
                                parsed = chainimport.parse_text(
                                    body, "option_chain", "csv", "onclickmedia")
                                con = cons.get(sym)
                                if con is None:
                                    con = cons[sym] = btdata.connect_data(
                                        btdata.data_db_path(sym))
                                res = btdata.import_chain(
                                    con, sym, parsed.chain, "onclickmedia")
                                written += res["contracts"]
                                self._pull = {**self._pull,
                                              "last": f"{sym} {iso}: {res['contracts']:,} contracts"}
                            else:
                                skipped += 1
                            done += 1
                            self._pull = {**self._pull, "done": done,
                                          "written": written, "skipped": skipped}
                            # Sleep AFTER the last one too: the thread ending a
                            # moment later costs nothing, and a special case
                            # here is one more thing that can burst the pace.
                            if self._pull_stop.wait(timeout=delay):
                                continue
                    self._pull = {**self._pull, "state": "done", "done": done,
                                  "written": written, "skipped": skipped}
                except Exception as exc:  # noqa: BLE001
                    LOG.exception("chain pull failed")
                    self._pull = {**self._pull, "state": "error", "error": str(exc),
                                  "done": done, "written": written}
                finally:
                    for c in cons.values():
                        try:
                            c.close()
                        except Exception:  # noqa: BLE001
                            pass

            self._pull_thread = threading.Thread(target=run, daemon=True,
                                                 name="chain-pull")
            self._pull_thread.start()
            return True, note
