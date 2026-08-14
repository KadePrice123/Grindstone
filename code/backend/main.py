"""Sidecar entrypoint.

Binds 127.0.0.1 on an OS-assigned port (or GRINDSTONE_PORT for dev), then
announces it on stdout as one JSON line the shell waits for:

    {"event": "listening", "port": 51234, "version": "0.1.0"}

GRINDSTONE_BOOT_TOKEN must arrive via the environment (never argv — argv is
visible in the process list). Standalone dev runs may omit it; a random token
is generated and printed so curl-testing is still possible.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import sys
import threading
import time

import uvicorn

from backend.app import API_VERSION, State, create_app


def _die_with_parent() -> None:
    """Exit when the shell does.

    The shell holds our stdin; if it is killed (Task Manager, a crash, a
    forced quit) its 'before-quit' handler never runs and we would survive as
    an orphan still holding the database — observed in testing, with two stale
    sidecars left behind. A blocking read on stdin returns EOF the moment the
    parent's pipe closes, which works on Windows and Linux alike without a
    native dependency or PID polling (os.kill(pid, 0) is NOT a liveness probe
    on Windows — it terminates the target).
    """

    def wait() -> None:
        try:
            sys.stdin.buffer.read()  # blocks until the parent closes the pipe
        except Exception:
            pass
        os._exit(0)

    threading.Thread(target=wait, daemon=True, name="parent-watchdog").start()


def main() -> int:
    # getattr, not sys.stdin.closed: under a console-less launch (pythonw, a
    # scheduled task, a --noconsole build) sys.stdin is None, and the bare
    # attribute access is an AttributeError whose traceback goes nowhere,
    # because print() is also a no-op with no console attached.
    if os.environ.get("GRINDSTONE_BOOT_TOKEN") and getattr(sys.stdin, "closed", True) is False:
        _die_with_parent()

    boot_token = os.environ.get("GRINDSTONE_BOOT_TOKEN")
    if not boot_token:
        boot_token = secrets.token_urlsafe(32)
        print(json.dumps({"event": "dev-token", "token": boot_token}), flush=True)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # NOT SO_REUSEADDR: on Windows that flag lets another same-user process
    # bind our exact port and steal connections carrying the boot token,
    # session token, and (during enrollment) plaintext broker keys.
    # SO_EXCLUSIVEADDRUSE is the one that blocks it; the two are mutually
    # exclusive and must be set before bind().
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows only
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    sock.bind(("127.0.0.1", int(os.environ.get("GRINDSTONE_PORT", "0"))))
    port = sock.getsockname()[1]
    sock.listen(128)

    print(json.dumps({"event": "listening", "port": port, "version": API_VERSION}),
          flush=True)

    from backend.logs import LOG, setup as setup_logging

    log_path = setup_logging()
    LOG.info("sidecar starting — version %s, python %s", API_VERSION, sys.version.split()[0])
    print(json.dumps({"event": "log", "path": log_path}), flush=True)

    state = State(boot_token)
    app = create_app(state)

    # Load whatever universe exists from a previous run, then start the
    # recorder — it borrows vault access via unlocked sessions only.
    from backend.marketdb import connect_market
    from backend.recorder import Recorder

    boot_con = connect_market()
    state.universe.load(boot_con)
    boot_con.close()
    # ONE recorder per data directory. If an unattended recorder already owns
    # this store, the app runs without one: two would halve each other's
    # effective interval against a shared 200 req/min budget and flap every
    # job's status. The Data page still shows everything, because it reads
    # record_jobs rather than in-memory state.
    from backend import instancelock
    from backend.db import data_dir as _data_dir

    state.recorder_lock = instancelock.acquire_or_none(_data_dir())
    if state.recorder_lock is not None:
        # Only the process that owns the runner may declare its runs orphaned.
        if state.backtests is not None:
            n = state.backtests.sweep_orphans()
            if n:
                LOG.info("marked %d interrupted backtest run(s)", n)
        state.recorder = Recorder(connect_market(), state.creds_for,
                                  state.settings_for,
                                  tasty_creds_provider=state.tasty_creds_for)
        state.recorder.start()
    else:
        LOG.info("an unattended recorder owns this data directory — the app "
                 "will not start a second one")

    # NO HEAVY IMPORTS HERE, EAGER OR LAZY. A heavy import holds the GIL and
    # the import lock, so every in-flight request stalls for its duration —
    # in a single-process sidecar that is a full server freeze. Warming
    # yfinance here made sign-in hang for a real user about ten seconds after
    # launch (exactly how long it takes to type a password) and cost three
    # rounds of wrong diagnoses; deferring it to the request path then blew
    # three concurrent chart requests past their deadline and tripped the
    # provider's breaker for five minutes. The lesson took twice: the market
    # fallback is now plain HTTP with no such import at all.

    # Defence in depth for the keep-alive race that broke sign-in: uvicorn's
    # default idle timeout is 5s, which is shorter than a human pausing to
    # type a password. The shell no longer pools connections, so this cannot
    # bite again — but a 5s window on a local-only server buys nothing, and
    # any future client that DOES pool deserves better odds.
    config = uvicorn.Config(app, log_level="warning", access_log=False,
                            timeout_keep_alive=120)
    server = uvicorn.Server(config)
    try:
        server.run(sockets=[sock])
    finally:
        state.recorder.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
