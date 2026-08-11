"""The unattended recorder: records market data with nobody signed in.

Run by a Windows scheduled task at logon:

    python -m backend.recorder_main

WHY A SEPARATE ENTRY POINT rather than the sidecar started headless. Three
reasons, each sufficient on its own:

  * **Constructing the app is destructive.** `BacktestManager.__init__` marks
    every run still flagged `running` as "interrupted by app restart", so a
    headless sidecar starting in the background would kill a backtest the user
    is watching.
  * **It would serve an API nobody can reach.** A self-started sidecar mints
    its own boot token, which the Electron shell does not know, so every route
    would 401 for the only client that exists.
  * **It would crash silently.** The sidecar's parent-watchdog dereferences
    `sys.stdin`, which is `None` under a console-less launch — and the `print`
    that would report it is a no-op with no console attached. (That has been
    fixed too, but this path never needed a parent watchdog.)

So: no FastAPI, no port to serve, no boot token, no BacktestManager, no stdin.
A market.db connection, a Recorder, and a loop.

"STARTS WITH THE PC" IS NOT WHAT THIS DOES, and the difference matters. The key
is sealed to the logged-on Windows user, so it can only be unsealed inside that
user's session. A machine rebooted and left sitting at the login screen records
nothing. The honest promise is **it starts at your Windows login, with no app
sign-in** — which is what kills the "locked — sign in to record" state, and
what captures overnight sessions, but it is not a service.
"""
from __future__ import annotations

import json
import signal
import sqlite3
import sys
import time
from typing import Any

from . import instancelock, syskey
from .db import data_dir
from .logs import LOG, setup as setup_logging
from .marketdb import connect_market
from .recorder import Recorder

#: Checked each tick. A file rather than IPC because this process serves no
#: API — and the toggle that removes the scheduled task must also be able to
#: stop the run already in flight.
STOP_FLAG = "recorder.stop"


def system_settings_provider(user_id: int) -> dict[str, Any]:
    """The user's settings, read straight from app.db.

    A settings row is NOT a secret: no DEK, no session, no `security` import
    — this process must never be able to unwrap the vault, and reading a
    boolean does not. Opened read-only per call and closed, because the app
    may be writing the same file.
    """
    try:
        path = data_dir() / "app.db"
        if not path.is_file():
            return {}
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT key, value FROM user_settings WHERE user_id=?",
                (user_id,)).fetchall()
        finally:
            con.close()
        out: dict[str, Any] = {}
        for k, v in rows:
            try:
                out[k] = json.loads(v)
            except (TypeError, ValueError):
                out[k] = v
        return out
    except Exception:  # noqa: BLE001 — never kill the recorder over a setting
        LOG.exception("unattended recorder: settings unreadable")
        return {}


def system_creds_provider(user_id: int) -> dict[str, str] | None:
    """Credentials for a recording job, from the sealed system key.

    Keyed to the enrolling user when the blob records one. A provider that
    ignored `user_id` and answered with one key for everybody would run user
    B's jobs on user A's credentials and budget — which on a single-profile
    machine is invisible and on a shared one is a real leak. Blobs written
    before that field existed carry no owner and answer for anyone, which is
    correct for the single-profile case they were written on."""
    data = syskey.load()
    if not data:
        return None
    owner = data.get("user_id")
    if owner is not None and int(owner) != int(user_id):
        return None
    return {"key_id": data["key_id"], "secret_key": data["secret_key"]}


def main() -> int:
    log_path = setup_logging()
    root = data_dir()

    # THE LOCK FIRST, before any database is opened. A second recorder that
    # gets as far as opening market.db has already had the chance to do harm.
    lock = instancelock.acquire_or_none(root)
    if lock is None:
        # A CLEAN exit. Task Scheduler treats a non-zero result as failure and
        # will retry forever, turning "the app is already running" into a
        # restart loop.
        LOG.info("unattended recorder: another instance owns %s, exiting", root)
        return 0

    LOG.info("unattended recorder starting — data dir %s, log %s", root, log_path)
    if syskey.virtualized():
        # Not fatal, but it decides whether this process can see the key another
        # process wrote. Said plainly here because the symptom downstream is
        # "no system key" while the file plainly exists.
        LOG.warning(
            "this interpreter's %%LOCALAPPDATA%% may be REDIRECTED (Microsoft "
            "Store Python). If the key was enrolled by a differently-launched "
            "process, this one may not see it. Expected path: %s",
            syskey.key_path())

    if syskey.load() is None:
        LOG.error("unattended recorder: no readable system key at %s — enrol one "
                  "in Data management, then restart. Not retrying.",
                  syskey.key_path())
        return 0  # clean: nothing to do until a human acts

    con = connect_market()
    rec = Recorder(con, system_creds_provider, system_settings_provider)
    rec.start()
    LOG.info("unattended recorder running")

    stop = root / STOP_FLAG
    stop.unlink(missing_ok=True)   # a stale flag must not kill this run

    def _signal(_sig: int, _frm: Any) -> None:
        LOG.info("unattended recorder: signal received, stopping")
        rec.stop()

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(s, _signal)
        except (ValueError, OSError):  # not available in every context
            pass

    try:
        while True:
            time.sleep(5)
            if stop.is_file():
                LOG.info("unattended recorder: stop flag set, shutting down")
                stop.unlink(missing_ok=True)
                break
            if not rec._thread or not rec._thread.is_alive():  # noqa: SLF001
                LOG.error("unattended recorder: the recorder thread died")
                break
    except KeyboardInterrupt:
        pass
    finally:
        rec.stop()
        con.close()
        lock.release()
        LOG.info("unattended recorder stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
