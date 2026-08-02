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
import threading
import traceback
from contextlib import AbstractContextManager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import market, newsstore, recorder as recorder_mod, search as search_mod
from . import security
from . import universe as universe_mod
from .brokers import base as brokers_base
from .brokers.alpaca import PAPER_URL, AlpacaAdapter
from .brokers.alpaca_data import AlpacaData
from .db import connect
from .marketdb import connect_market
from .sessions import SessionStore
from .universe import Universe

API_VERSION = "0.1.0"


class State:
    """Process-wide state, injected so tests can build isolated instances."""

    def __init__(self, boot_token: str, db_path=None, market_path=None) -> None:
        self.boot_token = boot_token
        self.sessions = SessionStore()
        self.db_path = db_path
        self.market_path = market_path
        self.universe = Universe()
        self.recorder = None  # set by main.py; tests may leave it None
        self._refresh_thread: threading.Thread | None = None

    def db(self) -> AbstractContextManager[sqlite3.Connection]:
        """`with state.db() as db:` — one transaction, always closed."""
        return connect(self.db_path)

    def market(self) -> sqlite3.Connection:
        """Short-lived market.db connection — caller closes."""
        return connect_market(self.market_path)

    def creds_for(self, user_id: int) -> dict[str, str] | None:
        """Alpaca data creds via any unlocked session — the recorder's and
        background refresher's path into the vault. None while locked."""
        snap = self.sessions.any_for_user(user_id)
        if snap is None:
            return None
        with self.db() as db:
            return market.alpaca_creds_for(db, user_id, snap.dek)

    def kick_market_refresh(self, user_id: int) -> None:
        """After login: sync the symbol universe and backfill news if stale.
        Background thread; failures degrade to an empty index, never a crash."""
        if self._refresh_thread and self._refresh_thread.is_alive():
            return

        def run() -> None:
            try:
                creds = self.creds_for(user_id)
                con = self.market()
                try:
                    status = universe_mod.sync_status(con)
                    if creds and status["stale"]:
                        client = AlpacaData(creds["key_id"], creds["secret_key"])
                        universe_mod.sync_from_alpaca(con, client, PAPER_URL)
                    self.universe.load(con)
                    if creds:
                        newest = newsstore.stats(con)["newest"]
                        client = AlpacaData(creds["key_id"], creds["secret_key"])
                        items, _ = client.news(limit=50, start=newest)
                        if not newest:  # first run: pull a few days back
                            for _ in range(5):
                                more, token = client.news(limit=50)
                                items.extend(more)
                                if not token:
                                    break
                        newsstore.upsert(con, items)
                finally:
                    con.close()
            except Exception:  # noqa: BLE001 — background refresh must not kill the app
                traceback.print_exc()

        self._refresh_thread = threading.Thread(target=run, daemon=True,
                                                name="market-refresh")
        self._refresh_thread.start()


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


# Module level, not inside create_app: with postponed annotations, FastAPI
# resolves type hints in module globals — a factory-local model silently
# degrades to a required query parameter.
class JobIn(BaseModel):
    kind: str
    symbol: str = ""
    timeframe: str = ""
    interval_seconds: int
    retention_days: int = 90


class JobPatch(BaseModel):
    enabled: bool | None = None
    interval_seconds: int | None = None
    retention_days: int | None = None


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
        state.kick_market_refresh(row["id"])
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
        state.kick_market_refresh(row["id"])
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
        # First-run ordering matters: at setup/login there was no account yet,
        # so the market refresh (universe + news backfill) skipped. Now that
        # credentials exist, kick it — otherwise the news store stays empty
        # until the NEXT login (observed live).
        state.kick_market_refresh(s.user_id)
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

    # -------------------------------------------------------------- search
    @app.get("/api/search")
    def omnibox(q: str = "", s=Depends(current_session)) -> dict[str, Any]:
        con = state.market()

        def live_news(symbol: str) -> list[dict[str, Any]]:
            creds = state.creds_for(s.user_id)
            if creds is None:
                return []
            try:
                items, _ = AlpacaData(creds["key_id"], creds["secret_key"]).news(
                    symbols=[symbol], limit=8)
            except brokers_base.BrokerError:
                return []
            newsstore.upsert(con, items)
            return items

        try:
            return search_mod.query(q, state.universe, con, live_news=live_news)
        finally:
            con.close()

    @app.get("/api/symbols/{symbol}/summary")
    def symbol_summary(symbol: str, s=Depends(current_session)) -> dict[str, Any]:
        symbol = symbol.upper()
        entry = state.universe.exact(symbol)
        creds = state.creds_for(s.user_id)
        quote = market.quote_for(symbol, (entry or {}).get("asset_class", "us_equity"), creds)
        con = state.market()
        try:
            news = newsstore.latest(con, symbols=[symbol], limit=12)
        finally:
            con.close()
        return {
            "symbol": symbol,
            "name": (entry or {}).get("name", ""),
            "asset_class": (entry or {}).get("asset_class", "unknown"),
            "quote": quote,
            "news": news,
        }

    @app.get("/api/news")
    def news_feed(symbols: str = "", limit: int = 30,
                  s=Depends(current_session)) -> list[dict[str, Any]]:
        syms = [t for t in symbols.split(",") if t.strip()] or None
        con = state.market()
        try:
            return newsstore.latest(con, symbols=syms, limit=min(limit, 100))
        finally:
            con.close()

    @app.get("/api/universe/status")
    def universe_status(s=Depends(current_session)) -> dict[str, Any]:
        con = state.market()
        try:
            st = universe_mod.sync_status(con)
        finally:
            con.close()
        return {**st, "loaded": state.universe.size}

    @app.post("/api/universe/sync")
    def universe_sync(s=Depends(current_session)) -> dict[str, Any]:
        creds = state.creds_for(s.user_id)
        if creds is None:
            raise HTTPException(409, "add an enabled Alpaca account first — the universe comes from its assets endpoint")
        con = state.market()
        try:
            client = AlpacaData(creds["key_id"], creds["secret_key"])
            n = universe_mod.sync_from_alpaca(con, client, PAPER_URL)
            loaded = state.universe.load(con)
        finally:
            con.close()
        return {"synced": n, "loaded": loaded}

    @app.get("/api/providers/status")
    def providers_status(s=Depends(current_session)) -> dict[str, Any]:
        return market.provider_status(state.creds_for(s.user_id) is not None)

    # ----------------------------------------------------- data management
    @app.get("/api/datamgmt/jobs")
    def jobs_list(s=Depends(current_session)) -> list[dict[str, Any]]:
        con = state.market()
        try:
            rows = con.execute(
                "SELECT * FROM record_jobs WHERE user_id=? ORDER BY id", (s.user_id,)
            ).fetchall()
        finally:
            con.close()
        return [dict(r) for r in rows]

    @app.post("/api/datamgmt/jobs")
    def jobs_create(body: JobIn, s=Depends(current_session)) -> dict[str, Any]:
        symbol = body.symbol.upper().strip()
        entry = state.universe.exact(symbol) if symbol else None
        err = recorder_mod.validate_job(
            body.kind, symbol, body.timeframe, body.interval_seconds,
            body.retention_days, (entry or {}).get("asset_class") if entry else None,
        )
        if err:
            raise HTTPException(422, err)
        if body.kind in ("bars", "chain") and entry is None and symbol:
            raise HTTPException(422, f"{symbol}: unknown symbol — sync the universe first")
        con = state.market()
        try:
            with con:
                cur = con.execute(
                    "INSERT INTO record_jobs (user_id, kind, symbol, timeframe,"
                    " interval_seconds, retention_days) VALUES (?,?,?,?,?,?)",
                    (s.user_id, body.kind, symbol, body.timeframe,
                     body.interval_seconds, body.retention_days),
                )
                job_id = cur.lastrowid
        finally:
            con.close()
        return {"id": job_id, "ok": True}

    @app.patch("/api/datamgmt/jobs/{job_id}")
    def jobs_patch(job_id: int, body: JobPatch, s=Depends(current_session)) -> dict[str, Any]:
        sets, vals = [], []
        if body.enabled is not None:
            sets.append("enabled=?"); vals.append(int(body.enabled))
        if body.interval_seconds is not None:
            if body.interval_seconds < recorder_mod.MIN_INTERVAL:
                raise HTTPException(422, f"interval must be at least {recorder_mod.MIN_INTERVAL}s")
            sets.append("interval_seconds=?"); vals.append(body.interval_seconds)
        if body.retention_days is not None:
            sets.append("retention_days=?"); vals.append(body.retention_days)
        if not sets:
            raise HTTPException(422, "nothing to change")
        con = state.market()
        try:
            with con:
                cur = con.execute(
                    f"UPDATE record_jobs SET {', '.join(sets)} WHERE id=? AND user_id=?",
                    (*vals, job_id, s.user_id),
                )
        finally:
            con.close()
        if cur.rowcount == 0:
            raise HTTPException(404, "no such job")
        return {"ok": True}

    @app.delete("/api/datamgmt/jobs/{job_id}")
    def jobs_delete(job_id: int, s=Depends(current_session)) -> dict[str, Any]:
        con = state.market()
        try:
            with con:
                cur = con.execute(
                    "DELETE FROM record_jobs WHERE id=? AND user_id=?", (job_id, s.user_id)
                )
        finally:
            con.close()
        if cur.rowcount == 0:
            raise HTTPException(404, "no such job")
        return {"ok": True}

    @app.get("/api/datamgmt/usage")
    def datamgmt_usage(s=Depends(current_session)) -> dict[str, Any]:
        if state.recorder is not None:
            return state.recorder.usage()
        con = state.market()
        try:
            return recorder_mod.Recorder(con, lambda _uid: None).usage()
        finally:
            con.close()

    return app
