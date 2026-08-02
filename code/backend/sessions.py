"""In-memory bearer sessions. Opaque 256-bit tokens, sliding idle expiry.

No JWT on purpose: nothing off-machine verifies claims, and revocation must be
instant (auto-lock). Tokens die with the process; the DEK lives only inside a
session entry while a user is unlocked (REQUIREMENTS.md 6.7).
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

IDLE_SECONDS_DEFAULT = 15 * 60


@dataclass
class Session:
    user_id: int
    username: str
    dek: bytearray
    last_seen: float = field(default_factory=time.monotonic)


class SessionStore:
    def __init__(self, idle_seconds: int = IDLE_SECONDS_DEFAULT) -> None:
        self._by_token: dict[str, Session] = {}
        self.idle_seconds = idle_seconds

    def create(self, user_id: int, username: str, dek: bytes) -> str:
        token = secrets.token_urlsafe(32)
        self._by_token[token] = Session(user_id, username, bytearray(dek))
        return token

    def get(self, token: str | None) -> Session | None:
        if not token:
            return None
        s = self._by_token.get(token)
        if s is None:
            return None
        now = time.monotonic()
        if now - s.last_seen > self.idle_seconds:
            self.revoke(token)
            return None
        s.last_seen = now
        return s

    def revoke(self, token: str) -> None:
        s = self._by_token.pop(token, None)
        if s is not None:
            for i in range(len(s.dek)):
                s.dek[i] = 0

    def revoke_user(self, user_id: int) -> int:
        tokens = [t for t, s in self._by_token.items() if s.user_id == user_id]
        for t in tokens:
            self.revoke(t)
        return len(tokens)

    def revoke_all(self) -> None:
        for t in list(self._by_token):
            self.revoke(t)
