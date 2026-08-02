"""FastAPI application factory.

Hardening per REQUIREMENTS.md 6.7: loopback-only (enforced by the caller's
bind), TrustedHost against DNS rebinding, NO CORS headers (default-deny — the
renderer talks to us through Electron main's IPC proxy, never from a browser
context), and a two-layer token model:

  X-App-Token: <boot token>   proves the caller is our shell  (every route)
  Authorization: Bearer <t>   proves which user is unlocked    (user routes)

The boot token comes from the GRINDSTONE_BOOT_TOKEN env var set by the shell
at spawn. Both checks apply to /api/auth/* too — token issuance is not exempt
(the MCP-SDK rebinding CVE was exactly an unguarded mount).
"""
from __future__ import annotations

import hmac
import sqlite3
from contextlib import AbstractContextManager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import security
from .brokers import base as brokers_base
from .brokers.alpaca import AlpacaAdapter
from .db import connect
from .sessions import SessionStore

API_VERSION = "0.1.0"


class State:
    """Process-wide state, injected so tests can build isolated instances."""

    def __init__(self, boot_token: str, db_path=None) -> None:
        self.boot_token = boot_token
        self.sessions = SessionStore()
        self.db_path = db_path

    def db(self) -> AbstractContextManager[sqlite3.Connection]:
        """`with state.db() as db:` — one transaction, always closed."""
        return connect(self.db_path)


# ------------------------------------------------------------------ schemas
class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=1024)


class AccountIn(BaseModel):
    broker: str
    kind: str
    nickname: str = Field(min_length=1, max_length=64)
    credentials: dict[str, str] = Field(default_factory=dict)


class TestIn(BaseModel):
    broker: str
    kind: str
    credentials: dict[str, str] = Field(default_factory=dict)


# ------------------------------------------------------------------ factory
def create_app(state: State) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.middleware("http")
    async def require_app_token(request: Request, call_next):
        supplied = request.headers.get("x-app-token", "")
        if not hmac.compare_digest(supplied, state.boot_token):
            return JSONResponse({"detail": "missing or bad app token"}, status_code=401)
        return await call_next(request)

    def current_session(request: Request):
        auth = request.headers.get("authorization", "")
        token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else None
        s = state.sessions.get(token)
        if s is None:
            raise HTTPException(status_code=401, detail="locked")
        return s

    # -------------------------------------------------------------- health
    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "version": API_VERSION}

    # ---------------------------------------------------------------- auth
    @app.get("/api/auth/status")
    def auth_status() -> dict[str, Any]:
        with state.db() as db:
            n = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        return {"initialized": n > 0}

    @app.post("/api/auth/setup")
    def auth_setup(body: Credentials) -> dict[str, Any]:
        with state.db() as db:
            if db.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
                raise HTTPException(409, "already initialized — use login")
            kdf_salt = security.new_salt()
            dek = security.new_dek()
            kek = security.derive_kek(body.password, kdf_salt)
            db.execute(
                "INSERT INTO users (username, pw_hash, kdf_salt, wrapped_dek) VALUES (?,?,?,?)",
                (body.username, security.hash_password(body.password), kdf_salt,
                 security.wrap_dek(kek, dek, body.username)),
            )
            row = db.execute("SELECT id FROM users WHERE username=?", (body.username,)).fetchone()
        token = state.sessions.create(row["id"], body.username, dek)
        return {"token": token, "username": body.username}

    @app.post("/api/auth/login")
    def auth_login(body: Credentials) -> dict[str, Any]:
        with state.db() as db:
            row = db.execute(
                "SELECT id, username, pw_hash, kdf_salt, wrapped_dek FROM users WHERE username=?",
                (body.username,),
            ).fetchone()
        if row is None or not security.verify_password(row["pw_hash"], body.password):
            raise HTTPException(401, "bad username or password")
        kek = security.derive_kek(body.password, row["kdf_salt"])
        try:
            dek = security.unwrap_dek(kek, row["wrapped_dek"], row["username"])
        except security.BadPassword:
            raise HTTPException(401, "credential vault failed to unlock") from None
        token = state.sessions.create(row["id"], row["username"], dek)
        return {"token": token, "username": row["username"]}

    @app.post("/api/auth/logout")
    def auth_logout(request: Request) -> dict[str, Any]:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            state.sessions.revoke(auth.removeprefix("Bearer ").strip())
        return {"ok": True}

    @app.post("/api/auth/lock")
    def auth_lock(s=Depends(current_session)) -> dict[str, Any]:
        n = state.sessions.revoke_user(s.user_id)
        return {"ok": True, "revoked": n}

    # ------------------------------------------------------------ accounts
    def _adapter(broker: str, kind: str, creds: dict[str, str]):
        if kind not in ("live", "paper", "data"):
            raise HTTPException(422, f"unknown kind {kind!r}")
        if broker == "alpaca":
            missing = [f for f in ("key_id", "secret_key") if not creds.get(f)]
            if missing:
                raise HTTPException(422, f"alpaca needs {', '.join(missing)}")
            return AlpacaAdapter(creds["key_id"], creds["secret_key"], kind)
        return None

    @app.get("/api/accounts")
    def accounts_list(s=Depends(current_session)) -> list[dict[str, Any]]:
        with state.db() as db:
            rows = db.execute(
                "SELECT id, broker, kind, nickname, enabled, created_at"
                " FROM accounts WHERE user_id=? ORDER BY id",
                (s.user_id,),
            ).fetchall()
            # Separate query, not group_concat: a hint is the tail of a
            # user-supplied credential and may contain ':' or ',', which
            # string-packing would silently corrupt.
            hint_rows = db.execute(
                "SELECT s.account_id, s.field, s.hint FROM secrets s"
                " JOIN accounts a ON a.id = s.account_id WHERE a.user_id=?",
                (s.user_id,),
            ).fetchall()
        hints: dict[int, dict[str, str]] = {}
        for h in hint_rows:
            if h["hint"]:
                hints.setdefault(h["account_id"], {})[h["field"]] = h["hint"]
        return [
            {
                "id": r["id"], "broker": r["broker"], "kind": r["kind"],
                "nickname": r["nickname"], "enabled": bool(r["enabled"]),
                "created_at": r["created_at"], "key_hints": hints.get(r["id"], {}),
            }
            for r in rows
        ]

    @app.post("/api/accounts")
    def accounts_create(body: AccountIn, s=Depends(current_session)) -> dict[str, Any]:
        if body.broker not in brokers_base.BROKERS:
            raise HTTPException(422, f"unknown broker {body.broker!r}")
        if body.kind not in ("live", "paper", "data"):
            raise HTTPException(422, f"unknown kind {body.kind!r}")
        spec = brokers_base.CREDENTIAL_FIELDS[body.broker]
        missing = [f for f in spec["fields"] if not body.credentials.get(f)]
        if missing:
            raise HTTPException(422, f"{body.broker} needs {', '.join(missing)}")
        with state.db() as db:
            cur = db.execute(
                "INSERT INTO accounts (user_id, broker, kind, nickname) VALUES (?,?,?,?)",
                (s.user_id, body.broker, body.kind, body.nickname),
            )
            account_id = cur.lastrowid
            for f in spec["fields"]:
                value = body.credentials[f]
                hint = value[-4:] if f == spec["hint_last4"] and len(value) >= 8 else ""
                db.execute(
                    "INSERT INTO secrets (account_id, field, blob, hint) VALUES (?,?,?,?)",
                    (account_id, f,
                     security.encrypt_secret(s.dek, value, s.user_id, account_id, f),
                     hint),
                )
        return {"id": account_id, "ok": True}

    @app.delete("/api/accounts/{account_id}")
    def accounts_delete(account_id: int, s=Depends(current_session)) -> dict[str, Any]:
        with state.db() as db:
            cur = db.execute(
                "DELETE FROM accounts WHERE id=? AND user_id=?", (account_id, s.user_id)
            )
        if cur.rowcount == 0:
            raise HTTPException(404, "no such account")
        return {"ok": True}

    @app.post("/api/accounts/test")
    def accounts_test_new(body: TestIn, s=Depends(current_session)) -> dict[str, Any]:
        """Test credentials BEFORE saving them (nothing persisted)."""
        adapter = _adapter(body.broker, body.kind, body.credentials)
        if adapter is None:
            return brokers_base.not_supported(body.broker)
        return adapter.test_connection()

    @app.post("/api/accounts/{account_id}/test")
    def accounts_test_saved(account_id: int, s=Depends(current_session)) -> dict[str, Any]:
        with state.db() as db:
            acct = db.execute(
                "SELECT id, broker, kind FROM accounts WHERE id=? AND user_id=?",
                (account_id, s.user_id),
            ).fetchone()
            if acct is None:
                raise HTTPException(404, "no such account")
            rows = db.execute(
                "SELECT field, blob FROM secrets WHERE account_id=?", (account_id,)
            ).fetchall()
        creds = {
            r["field"]: security.decrypt_secret(
                s.dek, r["blob"], s.user_id, account_id, r["field"]
            )
            for r in rows
        }
        adapter = _adapter(acct["broker"], acct["kind"], creds)
        if adapter is None:
            return brokers_base.not_supported(acct["broker"])
        return adapter.test_connection()

    return app
