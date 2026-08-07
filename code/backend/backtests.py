"""Backtest orchestration: presets, validation, and the runner subprocess.

Plain library like recorder.py — no FastAPI imports; app.py owns the routes.

The engine itself (backend/bt/) is calibrated against real tastytrade exports
and ships those references in backend/bt/calibration/, so a modified engine
can always be re-verified against the trades it is KNOWN to have made from the
exact data (a 'calibration' run). Two hard rules learned elsewhere in this
codebase shape the design:

- The sidecar process never imports numpy. This module touches only the
  engine's pure-Python validation surface (spec, expr, tastyio); everything
  heavy happens in bt_runner.py, a subprocess. A heavy import in the sidecar
  holds the GIL and the import lock long enough to freeze sign-in — the
  yfinance incident (requirements.txt) is the standing warning.
- A run takes 60-110s and the shell's IPC proxy destroys any request at 30s,
  so runs are job + poll: POST returns at once, GET reports progress, and
  cancellation is a process kill — the engine has no cancellation hook.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import btdata
from .bt import spec as bt_spec
from .bt import tastyio
from .bt.expr import RuleError
from .db import data_dir
from .logs import LOG
from .marketdb import connect_market, market_path

HERE = Path(__file__).resolve().parent          # .../code/backend
PRESETS_DIR = HERE / "bt" / "presets"           # bundled JSON strategy specs
CALIBRATION_DIR = HERE / "bt" / "calibration"   # bundled tastytrade references
VIX_CSV = HERE / "bt" / "data" / "vix_history.csv"

#: Bundled preset (by name) that reproduces each reference export. The mapping
#: lived in the old CLI (bt.py CALIBRATION_MAP); calibration always loads these
#: bundled files, never a user's edited copy — the reference must stay exact.
CALIBRATION_MAP = {
    "put spread": "put_spread_45dte",
    "call spread": "call_spread_45dte",
    "put": "naked_put_7dte",
}

#: Presets seeded for a new user. Calibration specs first — they double as the
#: known-good examples — then the showcase strategies.
SEED_ORDER = [
    "put_spread_45dte", "call_spread_45dte", "naked_put_7dte",
    "iron_condor_regime", "rsi_scaled_put", "bwc_call_spread_put",
    "calendar_put",
]

REPORT_KEY_TTL = 60.0  # seconds a minted report key stays valid


# ------------------------------------------------------------------- paths
def engine_paths(user_settings: dict[str, Any] | None = None,
                 underlying: str = "SPY") -> dict[str, str]:
    """Where the engine's inputs live, most explicit first:

    1. a Settings-supplied path (the user said exactly where),
    2. the workspace layout (this machine's multi-GB spy_options.db),
    3. the app-owned recorded-data store (data/backtest_data/<SYM>.db) —
       created by the app, filled by the recorder via btdata.sync, and the
       only option a fresh install ever needs to think about.

    The app-owned file holds BOTH engine tables (opt + bars), so it serves as
    options_db and bars_db at once. `source` tells the UI which world a run
    would read from."""
    s = user_settings or {}
    u = underlying.upper()
    workspace = data_dir().parent.parent  # <workspace>/<project>/data -> <workspace>
    app_db = str(btdata.data_db_path(u))
    explicit_opt = s.get("backtest_options_db") or None
    explicit_bars = s.get("backtest_bars_db") or None
    ws_opt = workspace / "spy_options.db"
    ws_bars = workspace / "spy_bars.db"
    if explicit_opt:
        source = "custom"
        options_db = explicit_opt
        bars_db = explicit_bars or (str(ws_bars) if ws_bars.is_file() else app_db)
    elif u == "SPY" and ws_opt.is_file():
        source = "workspace"
        options_db = str(ws_opt)
        bars_db = explicit_bars or str(ws_bars)
    else:
        source = "recorded"
        options_db = app_db
        bars_db = explicit_bars or app_db
    return {
        "options_db": options_db,
        "bars_db": bars_db,
        "source": source,
        "underlying": u,
        "app_db": app_db,
        "vix_csv": str(VIX_CSV),
        "cache": str(data_dir() / "backtest_cache.db"),
        "calibration": str(CALIBRATION_DIR),
        "out_root": str(data_dir() / "backtests"),
    }


def data_status(user_settings: dict[str, Any] | None = None,
                underlying: str = "SPY") -> dict[str, Any]:
    """Honest availability report: what can run on this machine right now,
    and how much recorded data the app-owned store holds. Opening the store
    CREATES it — a fresh install has its database from the first look, which
    is the point: the user never makes one by hand."""
    p = engine_paths(user_settings, underlying)
    opt = Path(p["options_db"])
    bars = Path(p["bars_db"])
    refs = tastyio.discover(p["calibration"]) if os.path.isdir(p["calibration"]) else []
    con = btdata.connect_data(p["app_db"])
    try:
        recorded = btdata.stats(con)
    finally:
        con.close()
    if p["source"] == "recorded":
        can_run = recorded["days"] > 0
        reason = ("" if can_run else
                  "no chain data yet — import a file, run the chain puller, or"
                  " set up recording below; you can also point Settings at an"
                  " existing chain database")
    else:
        can_run = opt.is_file()
        reason = "" if can_run else f"chain database not found at {p['options_db']}"
    # WHO ACTUALLY FILLED IT. `source` above says which FILE the engine will
    # read (app-owned / workspace / custom) — it says nothing about where the
    # rows came from. Reporting the app-owned store as "recorded" regardless
    # would describe an uploaded CSV as recorded market data, which is the
    # same class of claim as labelling indicative quotes as live.
    filled_by = ", ".join(
        f"{s['src']} ({s['days']}d)" for s in recorded.get("sources", []) if s["src"]
    ) or ("nothing yet" if not recorded["days"] else "unknown")
    return {
        "options_db": {"path": p["options_db"], "present": opt.is_file(),
                       "size_mb": round(opt.stat().st_size / 2**20, 1) if opt.is_file() else 0},
        "bars_db": {"path": p["bars_db"], "present": bars.is_file()},
        "vix_csv": {"present": Path(p["vix_csv"]).is_file()},
        "calibration": {"references": [os.path.basename(f) for f in refs],
                        "mapped": sorted(CALIBRATION_MAP)},
        "source": p["source"],
        "filled_by": filled_by,
        "underlying": p["underlying"],
        "recorded": recorded,
        "can_run": can_run,
        "reason": reason,
    }


# ----------------------------------------------------------------- presets
def bundled_presets() -> list[dict[str, Any]]:
    out = []
    for name in SEED_ORDER:
        f = PRESETS_DIR / f"{name}.json"
        if f.is_file():
            out.append(json.loads(f.read_text(encoding="utf-8")))
    return out


def seed_presets(db: sqlite3.Connection, user_id: int) -> int:
    """First touch of the presets API ships the bundled strategies to the
    user, calibration references flagged. Only when the user has NO rows —
    deleting a seeded preset is a choice, not a bug to undo."""
    n = db.execute("SELECT COUNT(*) FROM backtest_presets WHERE user_id=?",
                   (user_id,)).fetchone()[0]
    if n:
        return 0
    added = 0
    for raw in bundled_presets():
        name = raw.get("name", "strategy")
        # OR IGNORE: two first-ever GETs can race past the count check; the
        # UNIQUE(user_id, name) key makes the second writer a no-op, not a 500.
        db.execute(
            "INSERT OR IGNORE INTO backtest_presets"
            " (user_id, name, spec, builtin, calibration) VALUES (?,?,?,1,?)",
            (user_id, name, json.dumps(raw),
             1 if name in CALIBRATION_MAP.values() else 0),
        )
        added += 1
    return added


def validate_spec(raw: dict[str, Any]) -> dict[str, Any]:
    """Full engine validation without running anything. SpecError/RuleError
    carry the engine's own high-quality messages (unknown keys, unknown rule
    variables with the list of what exists) — surface them verbatim."""
    strat = bt_spec.Strategy.from_dict(raw)
    return {"ok": True, "name": strat.name, "describe": strat.describe()}


def vocab() -> dict[str, Any]:
    """Name sets for the spec editor: autocomplete and inline docs come from
    the same constants the engine validates against, so they cannot drift."""
    from .bt.expr import CONTEXT_FUNCTIONS, FUNCTIONS
    return {
        "market_vars": sorted(bt_spec.MARKET_VARS),
        "position_vars": sorted(bt_spec.POSITION_VARS),
        "portfolio_vars": sorted(bt_spec.PORTFOLIO_VARS),
        "entry_names": sorted(bt_spec.ENTRY_NAMES),
        "exit_names": sorted(bt_spec.EXIT_NAMES),
        "exit_sugar": sorted(bt_spec.EXIT_SUGAR),
        "selectors": sorted(bt_spec.SELECTORS),
        "functions": sorted(FUNCTIONS),
        "context_functions": sorted(CONTEXT_FUNCTIONS),
    }


# ------------------------------------------------------------------ runner
class RunActive(Exception):
    """A run is already in flight (one at a time by design: each run holds a
    3 GB read-only chain DB open and up to 8 day-chains in memory)."""


class RunnerUnavailable(Exception):
    """The runner cannot exist in this build (frozen exe has no -m entry)."""


class BacktestManager:
    """Owns the runner subprocess and the run rows in market.db.

    One run at a time. POST /runs spawns `python -m backend.bt_runner job.json`
    and returns; a reader thread follows the runner's JSON-line stdout for
    progress and ingests its result file when it exits. Cancel is SIGKILL —
    the engine cannot be interrupted politely, which is exactly why it runs
    in a process of its own.
    """

    def __init__(self, market_path: Path | None = None) -> None:
        self._market_path = market_path
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._run_id: int | None = None
        self._progress: dict[str, Any] = {}
        self._cancelled_run: int | None = None  # per-RUN, not a bare flag: a
        # bare flag raced start() and could mislabel the previous run's finish
        self._report_keys: dict[int, tuple[str, str, float]] = {}  # run -> (key, file, expiry)
        self._sync_lock = threading.Lock()
        self._sync_thread: threading.Thread | None = None
        self._sync_status: dict[str, Any] = {"state": "idle"}
        # A sidecar restart orphans any 'running' row — the subprocess died
        # with us (parent watchdog) or will write into a row nobody reads.
        con = self._market()
        try:
            with con:
                con.execute(
                    "UPDATE backtest_runs SET status='error',"
                    " error='interrupted by app restart',"
                    " finished_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')"
                    " WHERE status='running'"
                )
        finally:
            con.close()

    def _market(self) -> sqlite3.Connection:
        return connect_market(self._market_path)

    # ---------------------------------------------------------- lifecycle
    def start(self, user_id: int, kind: str, spec: dict[str, Any] | None,
              name: str, preset_id: int | None,
              start: str | None, end: str | None,
              paths: dict[str, str]) -> dict[str, Any]:
        if getattr(sys, "frozen", False):
            # A PyInstaller exe has no `-m backend.bt_runner` entry —
            # sys.executable would relaunch the whole sidecar. Refuse loudly
            # until the packaged build ships a runner entry point.
            raise RunnerUnavailable()
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                raise RunActive()
            con = self._market()
            try:
                with con:
                    cur = con.execute(
                        "INSERT INTO backtest_runs (user_id, preset_id, name, kind,"
                        " spec, status) VALUES (?,?,?,?,?,'running')",
                        (user_id, preset_id, name, kind,
                         json.dumps(spec) if spec else ""),
                    )
                    run_id = cur.lastrowid
            finally:
                con.close()

            try:
                out_dir = Path(paths["out_root"]) / str(run_id)
                out_dir.mkdir(parents=True, exist_ok=True)
                job = {
                    "run_id": run_id, "kind": kind, "spec": spec, "name": name,
                    "start": start, "end": end, "out_dir": str(out_dir),
                    "paths": paths, "calibration_map": CALIBRATION_MAP,
                    "presets_dir": str(PRESETS_DIR),
                }
                if paths.get("source") == "recorded":
                    # The runner refreshes the store from recorded data first,
                    # so a run always sees everything captured up to now. In
                    # the subprocess, not here: a first-ever sync can move
                    # millions of rows and the start endpoint must return fast.
                    job["sync"] = {
                        "market_db": str(self._market_path or market_path()),
                        "underlying": paths.get("underlying", "SPY"),
                    }
                job_path = out_dir / "job.json"
                job_path.write_text(json.dumps(job), encoding="utf-8")

                code_dir = HERE.parent  # .../code — so `backend` is importable
                env = dict(os.environ, PYTHONIOENCODING="utf-8")
                # The runner has no business holding the app token (it never
                # calls the API), so don't let it inherit one.
                env.pop("GRINDSTONE_BOOT_TOKEN", None)
                env.pop("GRINDSTONE_PORT", None)
                # stdin MUST be DEVNULL, never inherited: the sidecar's parent-
                # death watchdog holds a blocking ReadFile on our stdin pipe, and
                # duplicating that contended handle into the child wedged the
                # child's stdio — the runner froze on its FIRST stdout write, but
                # only under the real app (dev sidecars have no watchdog), found
                # 2026-08-03 via e2e. stderr goes to a file, not a pipe: nobody
                # reads it until exit, and a chatty run filling a 64KB pipe
                # buffer would deadlock the same way.
                with open(out_dir / "runner.stderr", "w", encoding="utf-8") as stderr_fh:
                    self._proc = subprocess.Popen(
                        [sys.executable, "-u", "-m", "backend.bt_runner", str(job_path)],
                        cwd=str(code_dir), env=env, text=True, encoding="utf-8",
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=stderr_fh,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
            except Exception as exc:  # noqa: BLE001 — the row must not stay 'running'
                LOG.exception("backtest %d: runner failed to launch", run_id)
                self._finish(run_id, "error", f"runner failed to launch: {exc}")
                raise RuntimeError(f"runner failed to launch: {exc}") from exc
            self._run_id = run_id
            self._cancelled_run = None
            self._progress = {"phase": "starting", "pct": 0.0}
            threading.Thread(target=self._follow, args=(self._proc, run_id, out_dir),
                             daemon=True, name=f"bt-run-{run_id}").start()
            return {"id": run_id, "status": "running"}

    def cancel(self, run_id: int) -> bool:
        with self._lock:
            if self._run_id != run_id or self._proc is None or self._proc.poll() is not None:
                return False
            self._cancelled_run = run_id
            self._proc.kill()
            return True

    def is_active(self, run_id: int) -> bool:
        with self._lock:
            return (self._run_id == run_id and self._proc is not None
                    and self._proc.poll() is None)

    # ----------------------------------------------------------- data sync
    def sync_now(self, underlying: str) -> bool:
        """Kick a background recorded-data sync (kick_market_refresh style:
        the lock closes the double-spawn gap, failures land in status, never
        an exception in a request thread). sqlite-to-sqlite only — no numpy
        enters this process."""
        with self._sync_lock:
            if self._sync_thread is not None and self._sync_thread.is_alive():
                return False
            self._sync_status = {"state": "running", "underlying": underlying.upper()}

            def run() -> None:
                try:
                    mcon = self._market()
                    dcon = btdata.connect_data(btdata.data_db_path(underlying))
                    try:
                        result = btdata.sync_from_recorded(mcon, dcon, underlying)
                    finally:
                        mcon.close()
                        dcon.close()
                    self._sync_status = {"state": "done",
                                         "underlying": underlying.upper(), **result}
                    LOG.info("backtest data sync (%s): %s", underlying, result)
                except Exception as exc:  # noqa: BLE001 — report, never crash
                    LOG.exception("backtest data sync failed")
                    self._sync_status = {"state": "error", "error": str(exc)}

            self._sync_thread = threading.Thread(target=run, daemon=True,
                                                 name="bt-data-sync")
            self._sync_thread.start()
            return True

    def sync_status(self) -> dict[str, Any]:
        return dict(self._sync_status)

    def active(self) -> dict[str, Any] | None:
        with self._lock:
            if self._proc is None or self._run_id is None:
                return None
            if self._proc.poll() is not None and not self._progress:
                return None
            return {"id": self._run_id, "progress": dict(self._progress)}

    # ------------------------------------------------------------- reader
    def _follow(self, proc: subprocess.Popen, run_id: int, out_dir: Path) -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except ValueError:
                    continue  # engine noise, not ours
                if evt.get("event") == "progress":
                    self._progress = {k: evt[k] for k in ("phase", "i", "n", "pct")
                                      if k in evt}
            proc.wait()
            stderr_tail = ""
            try:
                stderr_tail = (out_dir / "runner.stderr").read_text(
                    encoding="utf-8", errors="replace")[-2000:]
            except OSError:
                pass
            self._ingest(run_id, out_dir, proc.returncode, stderr_tail)
        except Exception:  # noqa: BLE001 — the reader must never die silently
            LOG.exception("backtest %d: reader thread failed", run_id)
            try:
                proc.kill()  # a reader failure must not leave the engine running
            except OSError:
                pass
            try:
                self._finish(run_id, "error", "internal: reader thread failed")
            except Exception:  # noqa: BLE001 — a DB failure here strands the row;
                # the restart sweep in __init__ is the backstop, but say so.
                LOG.exception("backtest %d: could not record reader failure", run_id)
        finally:
            with self._lock:
                if self._run_id == run_id:
                    # Guarded: an old reader must never wipe the NEXT run's state.
                    self._progress = {}
                    self._run_id = None
                    self._proc = None

    def _ingest(self, run_id: int, out_dir: Path, returncode: int,
                stderr_tail: str) -> None:
        result_path = out_dir / "result.json"
        with self._lock:
            cancelled = self._cancelled_run == run_id
            if cancelled:
                self._cancelled_run = None
        if cancelled:
            self._finish(run_id, "cancelled", "")
            shutil.rmtree(out_dir, ignore_errors=True)
            return
        if returncode != 0 or not result_path.is_file():
            # The runner reports engine errors as a result file with an
            # 'error' key when it can; a hard crash leaves only stderr.
            msg = f"runner exited {returncode}"
            if result_path.is_file():
                try:
                    msg = json.loads(result_path.read_text(encoding="utf-8")).get(
                        "error", msg)
                except ValueError:
                    pass
            elif stderr_tail:
                msg = f"{msg}: {stderr_tail.strip().splitlines()[-1][:300]}"
            LOG.warning("backtest %d failed: %s", run_id, msg)
            self._finish(run_id, "error", msg)
            return
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except ValueError:
            self._finish(run_id, "error", "runner wrote an unreadable result")
            return
        if result.get("error"):
            self._finish(run_id, "error", str(result["error"]))
            return
        con = self._market()
        try:
            with con:
                con.execute(
                    "UPDATE backtest_runs SET status='done', error='',"
                    " finished_at=strftime('%Y-%m-%dT%H:%M:%SZ','now'),"
                    " summary=?, trades=?, daily=?, calib=?, report_files=?"
                    " WHERE id=?",
                    (json.dumps(result.get("summary", {})),
                     json.dumps(result.get("trades", [])),
                     json.dumps(result.get("daily", [])),
                     json.dumps(result.get("calib", None)),
                     json.dumps(result.get("report_files", [])),
                     run_id),
                )
        finally:
            con.close()
        try:
            result_path.unlink()          # ingested — the DB row owns it now
            (out_dir / "job.json").unlink(missing_ok=True)
            (out_dir / "runner.stderr").unlink(missing_ok=True)
        except OSError:
            pass
        LOG.info("backtest %d done", run_id)

    def _finish(self, run_id: int, status: str, error: str) -> None:
        con = self._market()
        try:
            with con:
                con.execute(
                    "UPDATE backtest_runs SET status=?, error=?,"
                    " finished_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                    (status, error, run_id),
                )
        finally:
            con.close()

    # -------------------------------------------------------- report keys
    def mint_report_key(self, run_id: int, filename: str) -> str:
        """Single-use, short-lived key so ONE report can be fetched by a
        browser tab that cannot send the app-token header. Minting again
        invalidates the previous key; consuming removes it."""
        key = secrets.token_urlsafe(32)
        with self._lock:
            self._report_keys[run_id] = (key, filename, time.monotonic() + REPORT_KEY_TTL)
        return key

    def consume_report_key(self, run_id: int, supplied: str) -> str | None:
        """Returns the registered filename if the key matches, else None."""
        with self._lock:
            entry = self._report_keys.get(run_id)
            if entry is None:
                return None
            key, filename, expires = entry
            if time.monotonic() > expires:
                del self._report_keys[run_id]
                return None
            # bytes, not str: compare_digest raises on non-ASCII str input,
            # and a 500 from garbage in an UNAUTHENTICATED route is a gift.
            if not hmac.compare_digest(supplied.encode("utf-8", "replace"),
                                       key.encode("ascii")):
                return None
            del self._report_keys[run_id]  # single use
            return filename

    def delete_run(self, run_id: int, user_id: int) -> bool:
        """Remove a finished run: DB row, report directory, and any key."""
        with self._lock:
            if self._run_id == run_id:
                return False  # cancel first
            self._report_keys.pop(run_id, None)  # rowids can be reused
        con = self._market()
        try:
            with con:
                cur = con.execute(
                    "DELETE FROM backtest_runs WHERE id=? AND user_id=?"
                    " AND status != 'running'", (run_id, user_id))
        finally:
            con.close()
        if cur.rowcount == 0:
            return False
        shutil.rmtree(Path(engine_paths()["out_root"]) / str(run_id),
                      ignore_errors=True)
        return True


__all__ = [
    "BacktestManager", "CALIBRATION_MAP", "RunActive", "RuleError",
    "RunnerUnavailable", "bundled_presets", "data_status", "engine_paths",
    "seed_presets", "validate_spec", "vocab",
]
