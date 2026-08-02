"""In-memory bearer sessions. Opaque 256-bit tokens, sliding idle expiry.

No JWT on purpose: nothing off-machine verifies claims, and revocation must be
instant (auto-lock). Tokens die with the process; the DEK lives only inside a
session entry while a user is unlocked (REQUIREMENTS.md 6.7).

Concurrency contract — load-bearing, do not "simplify":
FastAPI runs our sync endpoints in a threadpool, so a lock/logout can land
mid-request. Callers therefore receive an immutable SNAPSHOT holding a private
copy of the DEK; the store's own buffer is never handed out. Without this, a
concurrent revoke() zeroed the buffer an in-flight request was about to
encrypt with, sealing broker keys under an all-zero (publicly known) key —
silent corruption *and* a disclosure hole for a stolen DB.
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field

IDLE_SECONDS_DEFAULT = 15 * 60


@dataclass(frozen=True)
class SessionSnapshot:
    """What request handlers get: a private, immutable view."""

    user_id: int
    username: str
    dek: bytes


@dataclass
class _Entry:
    user_id: int
    username: str
    dek: bytearray  # never leaves the store
    last_seen: float = field(default_factory=time.monotonic)


class SessionStore:
    def __init__(self, idle_seconds: int = IDLE_SECONDS_DEFAULT) -> None:
        self._by_token: dict[str, _Entry] = {}
        self._lock = threading.Lock()
        self.idle_seconds = idle_seconds

    def create(self, user_id: int, username: str, dek: bytes) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._by_token[token] = _Entry(user_id, username, bytearray(dek))
        return token

    def get(self, token: str | None) -> SessionSnapshot | None:
        if not token:
            return None
        with self._lock:
            e = self._by_token.get(token)
            if e is None:
                return None
            now = time.monotonic()
            if now - e.last_seen > self.idle_seconds:
                self._drop(token)
                return None
            e.last_seen = now
            return SessionSnapshot(e.user_id, e.username, bytes(e.dek))

    def _drop(self, token: str) -> None:
        """Caller must hold the lock. Wiping is safe here precisely because
        callers only ever hold copies (best-effort — Python may still have
        made copies we cannot reach; we never claim more)."""
        e = self._by_token.pop(token, None)
        if e is not None:
            for i in range(len(e.dek)):
                e.dek[i] = 0

    def revoke(self, token: str) -> None:
        with self._lock:
            self._drop(token)

    def revoke_user(self, user_id: int) -> int:
        with self._lock:
            tokens = [t for t, e in self._by_token.items() if e.user_id == user_id]
            for t in tokens:
                self._drop(t)
            return len(tokens)

    def revoke_all(self) -> None:
        with self._lock:
            for t in list(self._by_token):
                self._drop(t)

    def any_for_user(self, user_id: int) -> SessionSnapshot | None:
        """Newest unlocked session for a user — how background work (the data
        recorder) borrows vault access. Returns a snapshot copy like get();
        None means the user is locked and the caller must skip, not stall."""
        with self._lock:
            best: _Entry | None = None
            for e in self._by_token.values():
                if e.user_id == user_id and (best is None or e.last_seen > best.last_seen):
                    best = e
            if best is None:
                return None
            now = time.monotonic()
            if now - best.last_seen > self.idle_seconds:
                return None
            return SessionSnapshot(best.user_id, best.username, bytes(best.dek))

    def _peek_buffer(self, token: str) -> bytearray | None:
        """Test-only accessor so the gate can assert the wipe actually happens."""
        e = self._by_token.get(token)
        return e.dek if e else None
