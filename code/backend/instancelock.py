"""One recorder per data directory, enforced before anything opens a database.

Two recorders against one store is not a tidiness problem, it is four concrete
failures, all of which look like something else:

  * **Doubled provider calls.** Both processes see the same due jobs and both
    run them, halving each other's effective interval and defeating the
    MIN_INTERVAL that exists to protect a shared 200 req/min budget.
  * **Mutually de-scheduled jobs.** `last_run_at` is a single unconditional
    UPDATE, and `is_due` reads that same field, so each process keeps
    convincing the other that work is already done.
  * **A flapping status.** A locked recorder overwrites an unlocked one's "ok"
    every fifteen seconds, and back again.
  * **Killed backtests.** Merely constructing the app marks every run still
    flagged `running` as "interrupted by app restart" — so a second process
    starting destroys the first one's in-flight work.

WHY A SOCKET AND NOT A PID FILE. A PID file goes stale on a hard kill, and
`os.kill(pid, 0)` is not a liveness probe on Windows. An exclusive bind is
released by the kernel when the process dies, however it dies. `main.py`
already records this lesson for the sidecar's own port.

The port is derived from the RESOLVED data directory rather than being fixed,
so a second data directory — a test, a relocated market store — is a different
lock rather than a lockout. The lock is never placed inside `data_dir()`: that
tree is cloud-synced, and a sync client can resurrect a stale lock file.
"""
from __future__ import annotations

import socket
import zlib
from pathlib import Path

from .logs import LOG

#: The dynamic/private range. Derived, not fixed, so two data directories do
#: not contend — and never colliding with the sidecar's own OS-assigned port.
_BASE = 49152
_SPAN = 16384


def port_for(path: Path | str) -> int:
    """Stable per-data-directory port.

    `resolve()` does the normalising: it makes the path absolute, follows
    junctions, and on Windows returns the canonical CASE, so two spellings of
    one directory already agree. An extra `.lower()` was here and has been
    removed — it bought nothing on Windows and was wrong on POSIX, where
    `/data/Foo` and `/data/foo` are two different directories that would then
    have shared one lock."""
    key = str(Path(path).resolve()).encode("utf-8")
    return _BASE + zlib.crc32(key) % _SPAN


class InstanceLock:
    """Held for the process lifetime. Released by the OS if we die badly."""

    def __init__(self, data_dir: Path | str) -> None:
        self._port = port_for(data_dir)
        self._sock: socket.socket | None = None

    @property
    def port(self) -> int:
        return self._port

    def acquire(self) -> bool:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # NOT SO_REUSEADDR. On Windows that flag would let a second process
        # bind the same port, which is precisely what this must prevent.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            s.bind(("127.0.0.1", self._port))
            s.listen(1)
        except OSError:
            s.close()
            return False
        self._sock = s
        return True

    def release(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def __enter__(self) -> "InstanceLock":
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def acquire_or_none(data_dir: Path | str) -> InstanceLock | None:
    """The whole protocol in one call. None means somebody else owns it."""
    lock = InstanceLock(data_dir)
    if lock.acquire():
        LOG.info("recorder lock acquired for %s (port %d)", data_dir, lock.port)
        return lock
    LOG.info("another Grindstone recorder already owns %s — not starting a "
             "second one", data_dir)
    return None
