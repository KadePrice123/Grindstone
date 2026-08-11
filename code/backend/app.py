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

import datetime as dt
import hmac
import json
import sqlite3
import threading
import time
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import autorecord as autorecord_mod
from . import backfill as backfill_mod
from . import backtests as backtests_mod
from . import coverage as coverage_mod
from . import datajobs as datajobs_mod
from . import autostart as autostart_mod
from . import notepad as notepad_mod
from . import keyprobe as keyprobe_mod
from . import syskey as syskey_mod
from . import chartobjects as chartobjects_mod
from . import options as options_mod
from . import opthist as opthist_mod
from . import favorites as favorites_mod
from . import market, newsstore, recorder as recorder_mod, search as search_mod
from . import security
from . import settings as settings_mod
from . import wheels as wheels_mod
from . import universe as universe_mod
from .providers import reader, websearch
from .logs import LOG
from .brokers import base as brokers_base
from .brokers.alpaca import PAPER_URL, AlpacaAdapter
from .brokers.alpaca_data import AlpacaData
from .db import connect
from . import marketdb
from .marketdb import connect_market
from .sessions import SessionStore
from .universe import Universe

API_VERSION = "0.2.0"


class State:
    """Process-wide state, injected so tests can build isolated instances."""

    def __init__(self, boot_token: str, db_path=None, market_path=None) -> None:
        self.boot_token = boot_token
        self.sessions = SessionStore()
        self.db_path = db_path
        self.market_path = market_path
        self.universe = Universe()
        self.recorder = None  # set by main.py; tests may leave it None
        #: Held for the process lifetime when this process owns recording.
        #: Kept on State so the socket is not garbage-collected, which would
        #: silently release the lock and let a second recorder start.
        self.recorder_lock = None
        self.backtests = None  # BacktestManager, created by create_app
        # One owner for both background data jobs, so "is an import already
        # running" has a single answer. Two importers writing the same
        # (d, exp, cp, strike) rows would interleave two files into one chain
        # with no error anywhere.
        self.datajobs = datajobs_mod.DataJobs()
        self._refresh_thread: threading.Thread | None = None
        self._refresh_lock = threading.Lock()
        self._live_news_last: dict[str, float] = {}

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

    def system_creds(self) -> dict[str, str] | None:
        """The unattended recorder's credentials, or None.

        A SEPARATE accessor rather than a fallback inside `creds_for`, on
        purpose. `creds_for` has eight interactive callers — quotes, charts,
        chains, news — and quietly answering them with the system key would
        swap every one of them onto a paper account's entitlements (IEX and
        indicative only) with no UI event and no way for the user to notice.
        Only the recorder asks this.

        Reaches no vault: no DEK, no session, no `security` import. A
        background process that could unwrap the DEK would hold the keys to
        live trading credentials."""
        data = syskey_mod.load()
        if not data:
            return None
        return {"key_id": data["key_id"], "secret_key": data["secret_key"]}

    def live_news_allowed(self, symbol: str, cooldown: float = 60.0) -> bool:
        """Rate-gate for the omnibox live-news fallthrough: without this,
        every keystroke of '<TICKER> news' for an unknown-news symbol was an
        uncached Alpaca request — an amplifier against the shared 200/min
        budget (review 2026-08-02). One live attempt per symbol per minute."""
        now = time.monotonic()
        last = self._live_news_last.get(symbol.upper(), -1e12)
        if now - last < cooldown:
            return False
        self._live_news_last[symbol.upper()] = now
        return True

    def kick_market_refresh(self, user_id: int) -> None:
        """After login/account-add: sync the symbol universe and backfill news
        if stale. Background thread; failures degrade to an empty index,
        never a crash. Lock closes the check-then-act gap — two concurrent
        logins used to double-spawn the sync."""

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
                        client = AlpacaData(creds["key_id"], creds["secret_key"])
                        newsstore.backfill(con, client)
                finally:
                    con.close()
                LOG.info("market refresh done — universe %d, news %s",
                         self.universe.size, "refreshed" if creds else "skipped (no creds)")
            except brokers_base.BrokerError as exc:
                # An expected, actionable condition — rejected or missing keys,
                # a broker outage. A full stack trace here reads as a crash to
                # anyone who just ran the installer (the gate boots the app, so
                # every keyless clone printed one), and buries the one line that
                # actually tells them what to do. Unexpected faults still get
                # the traceback below.
                LOG.warning("market refresh skipped — %s", exc)
            except Exception:  # noqa: BLE001 — background refresh must not kill the app
                LOG.exception("market refresh failed")

        with self._refresh_lock:
            if self._refresh_thread and self._refresh_thread.is_alive():
                return
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


class PresetIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    spec: dict[str, Any]


class PresetPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    spec: dict[str, Any] | None = None


class SpecIn(BaseModel):
    spec: dict[str, Any]


class RunIn(BaseModel):
    kind: str = "run"                      # 'run' | 'calibration'
    preset_id: int | None = None           # spec comes from a saved preset...
    spec: dict[str, Any] | None = None     # ...or inline from the editor
    name: str = Field(default="", max_length=64)
    start: str | None = None               # ISO dates narrowing the window
    end: str | None = None


class BtDataIn(BaseModel):
    underlying: str = Field(default="SPY", min_length=1, max_length=12)


class SystemKeyIn(BaseModel):
    key_id: str = Field(min_length=8, max_length=256)
    secret_key: str = Field(min_length=8, max_length=512)
    #: Set only after the user has been shown, in words, that the key can place
    #: real orders. Never defaulted true, and never inferred.
    accept_live_risk: bool = False


class NotepadAddIn(BaseModel):
    payload: dict[str, Any]
    label: str = Field(default="", max_length=80)


class NotepadEditIn(BaseModel):
    payload: dict[str, Any] | None = None
    label: str | None = Field(default=None, max_length=80)


class ImportIn(BaseModel):
    """A PATH, not a payload. The IPC bridge hardcodes application/json with a
    30s deadline, the sidecar is one uvicorn process whose event loop would
    parse a base64 body inline, and python-multipart is not installed — a
    FastAPI UploadFile route raises at IMPORT time and takes the whole sidecar
    down at boot rather than failing one request. Settings already points the
    app at a multi-GB database by path for the same reason."""
    path: str = Field(min_length=1, max_length=4096)
    kind: str = Field(default="option_chain", max_length=32)
    underlying: str = Field(default="SPY", min_length=1, max_length=12)


class PullIn(BaseModel):
    symbols: list[str] = Field(default_factory=list, max_length=20)
    start: str = Field(default="", max_length=10)
    end: str = Field(default="", max_length=10)
    seconds_between: float = Field(default=6.0, ge=5.0, le=10.0)


# ------------------------------------------------------------------ factory
def _backfill_range(years: str) -> tuple[dt.date, dt.date]:
    """How far back to try. 'max' is 2016 because that is where Alpaca's free
    equity history begins — claiming to reach further would just manufacture
    a decade of `failed` periods that can never become `have`."""
    end = dt.date.today()
    if years == "max":
        return dt.date(2016, 1, 1), end
    try:
        n = int(years)
    except ValueError:
        n = 2
    return end - dt.timedelta(days=365 * max(1, n)), end


def create_app(state: State) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    if state.backtests is None:
        # Boot-time (single-threaded) so requests never race to create it; it
        # also marks any run orphaned by the previous process as errored.
        state.backtests = backtests_mod.BacktestManager(state.market_path)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        # The renderer says "backend error"; THIS says why. Diagnosing blind
        # twice was enough.
        LOG.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"detail": "internal error — see data/logs/backend.log"},
                            status_code=500)

    @app.middleware("http")
    async def require_app_token(request: Request, call_next):
        # THE one deliberate exemption (2026-08-03): backtest report pages
        # open in a hardened browser tab, which cannot send custom headers.
        # The route guards itself with a single-use 60s key minted over the
        # authed API and carried in the query string — Electron main is the
        # only holder of both the key and the port, so the exposure is one
        # GET of one HTML file on loopback, once.
        if (request.method == "GET"
                and request.url.path.startswith("/api/backtests/report/")):
            return await call_next(request)
        supplied = request.headers.get("x-app-token", "")
        if not hmac.compare_digest(supplied, state.boot_token):
            return JSONResponse({"detail": "missing or bad app token"}, status_code=401)
        # Request-level logging: "did the request arrive, and how long did it
        # take" is the first question of every incident so far, and inferring
        # it cost several rounds.
        t0 = time.monotonic()
        response = await call_next(request)
        ms = (time.monotonic() - t0) * 1000
        if ms > 400 or request.url.path.startswith("/api/auth/"):
            LOG.info("%s %s -> %s in %.0fms", request.method, request.url.path,
                     response.status_code, ms)
        return response

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

    @app.get("/api/auth/me")
    def auth_me(s=Depends(current_session)) -> dict[str, Any]:
        return {"username": s.username}

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
        with state.db() as db:
            prefs = settings_mod.get_all(db, s.user_id)
        con = state.market()

        def web(text: str) -> list[dict[str, Any]]:
            # Only for queries that look like web searches: a 2-char ticker
            # prefix should not spend a network round-trip.
            if not prefs.get("web_search_enabled", True) or len(text.strip()) < 4:
                return []
            return websearch.web_results(
                text, limit=5,
                backend=prefs.get("web_search_engine", websearch.DEFAULT_BACKEND))

        def live_news(symbol: str) -> list[dict[str, Any]]:
            if not state.live_news_allowed(symbol):
                return []
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
            res = search_mod.query(q, state.universe, con, live_news=live_news, web=web)
        finally:
            con.close()
        # An empty answer must explain itself (observed: a fresh install has
        # no synced universe, so every ticker search returned silent nothing).
        if q.strip() and not res["results"]:
            if state.universe.size <= len(universe_mod.SUPPLEMENT) + 2:
                res["results"] = [{
                    "type": "page", "page": "accounts",
                    "title": "Add an Alpaca account to enable symbol & news search",
                    "subtitle": "The ticker universe syncs from your account — Accounts page",
                }]
            else:
                res["empty"] = True
        return res

    @app.get("/api/search/page")
    def search_page(q: str = "", page: int = 1, per_page: int = 10,
                    s=Depends(current_session)) -> dict[str, Any]:
        with state.db() as db:
            prefs = settings_mod.get_all(db, s.user_id)
        con = state.market()
        try:
            return search_mod.page(q, state.universe, con, page_no=page,
                                   per_page=per_page, prefs=prefs)
        finally:
            con.close()

    # ------------------------------------------------------------ settings
    @app.get("/api/settings")
    def settings_get(s=Depends(current_session)) -> dict[str, Any]:
        with state.db() as db:
            values = settings_mod.get_all(db, s.user_id)
        return {"values": values, "schema": settings_mod.schema(),
                "web": websearch.status()}

    @app.put("/api/settings")
    def settings_put(body: dict[str, Any], s=Depends(current_session)) -> dict[str, Any]:
        # The BEFORE value, read inside the same handler: turning auto-record
        # on has to enroll the favourites you ALREADY have, not only the ones
        # you star afterwards. A setting that silently applies to future
        # actions only is a setting the user believes is running when it is
        # not — they flip it, see nothing happen, and reasonably conclude it
        # is broken.
        try:
            with state.db() as db:
                was_on = bool(settings_mod.get_all(db, s.user_id)
                              .get(autorecord_mod.SETTING, False))
                values = settings_mod.put(db, s.user_id, body)
                now_on = bool(values.get(autorecord_mod.SETTING, False))
                favs = favorites_mod.list_(db, s.user_id) if now_on and not was_on else []
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

        out: dict[str, Any] = {"values": values}
        # OFF -> ON only. Re-running this on every settings save would be
        # wasted work, and re-enabling jobs the user had deliberately paused
        # in the Data page would quietly overrule them.
        if now_on and not was_on:
            symbols = [str(f.get("key", "")) for f in favs
                       if f.get("kind") == "symbol"]

            def classify(sym: str) -> tuple[str | None, bool]:
                entry = state.universe.exact(sym)
                return (entry or {}).get("asset_class"), entry is not None

            con = state.market()
            try:
                out["autorecord"] = autorecord_mod.sync_all(
                    con, s.user_id, symbols, classify, True)
            finally:
                con.close()
            LOG.info("autorecord enabled: %d started, %d skipped",
                     len(out["autorecord"]["started"]),
                     len(out["autorecord"]["skipped"]))
        return out

    # ------------------------------------------------------------ favorites
    @app.get("/api/pages")
    def pages_list(s=Depends(current_session)) -> dict[str, Any]:
        """The provider-app registry, for the launcher. One source of truth:
        search_mod.PAGES already knows every page and whether it is built —
        a second hardcoded list in the renderer is exactly how the old home
        grid drifted."""
        return {"pages": [
            {"key": p["key"], "title": p["title"], "ready": bool(p.get("ready"))}
            for p in search_mod.PAGES
        ]}

    @app.get("/api/favorites")
    def favorites_list(s=Depends(current_session)) -> dict[str, Any]:
        with state.db() as db:
            return {"favorites": favorites_mod.list_(db, s.user_id)}

    @app.post("/api/favorites")
    def favorites_add(body: dict[str, Any],
                      s=Depends(current_session)) -> dict[str, Any]:
        icon = body.get("icon") or ""
        # 'web' favorites capture the site's favicon (the tab image) at star
        # time; the shell passes the URL it observed via page-favicon-updated.
        if not icon and body.get("kind") == "web" and body.get("icon_url"):
            icon = favorites_mod.fetch_icon(body.get("icon_url"))
        try:
            with state.db() as db:
                fav = favorites_mod.add(db, s.user_id, body.get("kind"),
                                        body.get("key"), body.get("label"), icon)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

        # AUTO-RECORD (DS-17). Deliberately AFTER the favourite is committed
        # and deliberately non-fatal: the star is the thing the user asked
        # for and it succeeded. A symbol that cannot be recorded comes back
        # with a reason attached — "starred, but not recordable, and why"
        # (DS-18) — rather than a silent no-op or a refused favourite.
        rec: dict[str, Any] = {"recording": False, "reason": "", "jobs": []}
        if fav.get("kind") == "symbol":
            with state.db() as db:
                on = bool(settings_mod.get_all(db, s.user_id)
                          .get(autorecord_mod.SETTING, False))
            if on:
                sym = str(fav.get("key", "")).upper()
                entry = state.universe.exact(sym)
                con = state.market()
                try:
                    rec = autorecord_mod.on_favorite_added(
                        con, s.user_id, sym,
                        (entry or {}).get("asset_class"), entry is not None, True)
                finally:
                    con.close()
        return {"favorite": fav, "autorecord": rec}

    @app.delete("/api/favorites/{fav_id}")
    def favorites_delete(fav_id: int, s=Depends(current_session)) -> dict[str, Any]:
        # Read the row BEFORE deleting it: afterwards there is no symbol left
        # to stop recording, and the job would keep running for a favourite
        # that no longer exists.
        with state.db() as db:
            doomed = next((f for f in favorites_mod.list_(db, s.user_id)
                           if f["id"] == fav_id), None)
            removed = favorites_mod.remove(db, s.user_id, fav_id)
            on = bool(settings_mod.get_all(db, s.user_id)
                      .get(autorecord_mod.SETTING, False))
        if not removed:
            raise HTTPException(404, "no such favorite")

        # Un-star STOPS recording and KEEPS the data (DS-17). Deleting months
        # of chain history as a side effect of un-starring a shortcut would
        # be unrecoverable — the provider windows have moved past it.
        stop: dict[str, Any] = {"stopped": [], "kept_data": True}
        if on and doomed and doomed.get("kind") == "symbol":
            con = state.market()
            try:
                stop = autorecord_mod.on_favorite_removed(
                    con, s.user_id, str(doomed.get("key", "")), True)
            finally:
                con.close()
        return {"ok": True, "autorecord": stop}

    # -------------------------------------------------------- chart objects
    @app.get("/api/chart-objects")
    def chart_objects_get(key: str = "", s=Depends(current_session)) -> dict[str, Any]:
        """One chart's drawings. The key is a QUERY parameter, not a path
        segment: it is the engine's bucket name ("SPY|1Day|$"), and '|' is not
        a legal unescaped path character."""
        try:
            with state.db() as db:
                return {"key": key, "doc": chartobjects_mod.get(db, s.user_id, key)}
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

    @app.put("/api/chart-objects")
    def chart_objects_put(body: dict[str, Any],
                          s=Depends(current_session)) -> dict[str, Any]:
        try:
            with state.db() as db:
                doc = chartobjects_mod.put(db, s.user_id, body.get("key"),
                                           body.get("doc"))
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        return {"key": body.get("key"), "doc": doc}

    @app.get("/api/chart-objects/keys")
    def chart_objects_keys(s=Depends(current_session)) -> dict[str, Any]:
        with state.db() as db:
            return {"charts": chartobjects_mod.list_keys(db, s.user_id)}

    # ------------------------------------------------------- gesture wheels
    @app.get("/api/wheels")
    def wheels_get(s=Depends(current_session)) -> dict[str, Any]:
        with state.db() as db:
            return wheels_mod.get(db, s.user_id)

    @app.put("/api/wheels")
    def wheels_put(body: dict[str, Any], s=Depends(current_session)) -> dict[str, Any]:
        try:
            with state.db() as db:
                return wheels_mod.put(db, s.user_id, body)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

    @app.get("/api/quotes")
    def quotes_batch(symbols: str = "", s=Depends(current_session)) -> dict[str, Any]:
        """A small batch of quotes for the wheel's ticker segments — fetched
        once per wheel spawn, in parallel, under one deadline. The wheel's
        colors deliberately do NOT update while it is open (spec: no
        flashing), so a single snapshot per spawn is the whole contract."""
        import concurrent.futures

        syms = [t.strip().upper() for t in symbols.split(",") if t.strip()][:12]
        if not syms:
            return {"quotes": {}}
        creds = state.creds_for(s.user_id)

        def one(sym: str) -> tuple[str, dict[str, Any]]:
            entry = state.universe.exact(sym)
            q = market.quote_for(sym, (entry or {}).get("asset_class", "us_equity"), creds)
            return sym, {
                "available": bool(q.get("available")),
                "price": q.get("price"),
                "change_pct": q.get("change_pct"),
            }

        out: dict[str, Any] = {}
        # One deadline for the whole batch: the wheel is already on screen and
        # numbers fade in — a slow provider must cost at most this, not hang.
        # NOT a `with` block: exiting one joins every worker, which would make
        # this endpoint wait out a stuck provider despite the 6s wait() below.
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=6,
                                                     thread_name_prefix="quotes")
        try:
            futs = [pool.submit(one, sym) for sym in syms]
            done, _pending = concurrent.futures.wait(futs, timeout=6.0)
            for f in done:
                try:
                    sym, q = f.result()
                    out[sym] = q
                except Exception:  # noqa: BLE001 — a bad symbol stays absent
                    pass
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        for sym in syms:
            out.setdefault(sym, {"available": False, "price": None, "change_pct": None})
        return {"quotes": out}

    @app.get("/api/symbols/{symbol}/options")
    def symbol_options(symbol: str, exp_from: str, exp_to: str,
                       strike_from: float, strike_to: float, right: str = "",
                       s=Depends(current_session)) -> dict[str, Any]:
        """Contracts inside one leg's acceptance window — FILTERING only, no
        order surface. Bounds are required because the unfiltered chain is
        ~10k rows: a caller that wants to browse widens the window, it does
        not omit it. No creds / provider failure is available=False with the
        reason, never a 500 — the panel renders that state."""
        with state.db() as db:
            ttl = float(settings_mod.get_all(db, s.user_id)
                        .get("options_cache_minutes", 15.0))
        try:
            # The universe knows what an index or a future is; unknown symbols
            # come back None and pass straight through, which is the
            # setup-recording precedent — do not hard-require the universe.
            known = state.universe.exact(symbol)
            answer = options_mod.fetch(
                state.creds_for(s.user_id), symbol,
                exp_from, exp_to, strike_from, strike_to,
                right.upper() if right else None,
                con=state.market(), ttl_minutes=ttl,
                asset_class=(known or {}).get("asset_class"))
            # A REFUSAL IS A 200 HERE, by design — the panel renders the
            # reason instead of throwing. That also makes it invisible in the
            # access log: "GET /options -> 200" reads identically whether the
            # user got a chain or got told they have no key. Log the reason so
            # the next report of "it says I have no key" is one grep, not an
            # afternoon of inference.
            if not answer.get("available"):
                LOG.info("chain %s unavailable: %s", symbol,
                         answer.get("reason") or "(no reason given)")
            return answer
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

    @app.get("/api/symbols/{symbol}/options/history")
    def symbol_option_history(symbol: str, expiration: str, strike: float,
                              right: str, s=Depends(current_session)) -> dict[str, Any]:
        """One contract's archived daily rows — price/spread through its life.
        Comes from the imported archive, never the live feed: Alpaca sells no
        historical option quotes, so absence is answered with the reason."""
        if right.upper() not in ("C", "P"):
            raise HTTPException(422, "right must be C or P")
        try:
            dt_check = expiration  # validated inside; ValueError -> 422
            return opthist_mod.history(symbol, dt_check, strike, right.upper())
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

    @app.get("/api/symbols/{symbol}/options/serieshistory")
    def symbol_option_series(symbol: str, right: str, dte: int,
                             delta: float | None = None,
                             strike: float | None = None,
                             dte_tol: int = 3, delta_tol: float = 0.08,
                             strike_tol: float = 1.0,
                             s=Depends(current_session)) -> dict[str, Any]:
        """The constant-shape series: ~this DTE at ~this |delta| (strike is
        the fallback shape when no delta is known), priced day by day across
        the archive — the history a TRADE has, not the history one contract
        has."""
        if right.upper() not in ("C", "P"):
            raise HTTPException(422, "right must be C or P")
        try:
            return opthist_mod.series_history(
                symbol, right.upper(), dte,
                delta=delta, strike=strike,
                dte_tol=max(0, min(30, dte_tol)),
                delta_tol=max(0.01, min(0.25, delta_tol)),
                strike_tol=max(0.0, min(50.0, strike_tol)))
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

    @app.get("/api/symbols/{symbol}/options/fanchart")
    def symbol_option_fanchart(symbol: str, expiration: str, strike: float,
                               right: str, s=Depends(current_session)) -> dict[str, Any]:
        """The fan chart: this contract's spread path against the archive-wide
        percentiles of similar (same |delta| bucket) contracts at each DTE."""
        if right.upper() not in ("C", "P"):
            raise HTTPException(422, "right must be C or P")
        try:
            return opthist_mod.fanchart(symbol, expiration, strike, right.upper())
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

    @app.get("/api/symbols/{symbol}/bars")
    def symbol_bars(symbol: str, timeframe: str = "1Day", limit: int = 0,
                    s=Depends(current_session)) -> dict[str, Any]:
        """Chart data, in the order the code actually tries them: the live
        provider, then this app's own bar cache, then the user's recorded
        bars, then Yahoo daily as the keyless fallback. The response always
        names its source — a chart that lies about where its candles came from
        is worse than no chart, and a cached one also says how old it is.

        (This docstring used to claim "recorded bars first, topped up from the
        live provider". The code has never done that. It is written from the
        code now.)

        A live fetch is written through to bar_cache, which is what makes the
        chart still draw when the provider is unreachable — and what gives the
        'Keep chart data for' setting something to govern.

        DEPTH: limit=0 (the default) means "as configured" — the user's
        chart_candles setting, whose own default is ALL available history.
        An explicit limit still wins (the search page's featured card wants
        90, never everything)."""
        symbol = symbol.upper()
        if timeframe not in recorder_mod.TIMEFRAMES:
            raise HTTPException(422, f"timeframe must be one of {', '.join(recorder_mod.TIMEFRAMES)}")

        if limit <= 0:
            with state.db() as db:
                pref = settings_mod.get_all(db, s.user_id).get("chart_candles", "all")
            want_all = pref == "all"
            limit = 10_000 if want_all else max(10, min(int(pref), 5000))
        else:
            want_all = False
            limit = max(10, min(limit, 5000))

        entry = state.universe.exact(symbol)
        if entry and entry["asset_class"] in ("index", "future"):
            return {"symbol": symbol, "timeframe": timeframe, "bars": [],
                    "source": "none",
                    "reason": f"no connected source carries {entry['asset_class']} bars yet"}

        creds = state.creds_for(s.user_id)
        if creds:
            # Fetch windows sized to fill the requested depth; "all" reaches
            # back as far as the feed does (Alpaca IEX starts 2016; a single
            # request tops out at 10k bars, which covers 39 years of dailies
            # and ~3 weeks of 1-minute bars — an honest ceiling, not a bug).
            span = ({"1Min": 30, "5Min": 90, "15Min": 250, "1Hour": 1000, "1Day": 4000}
                    if want_all else
                    {"1Min": 3, "5Min": 10, "15Min": 30, "1Hour": 120, "1Day": 1500})[timeframe]
            start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=span)).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            try:
                bars = AlpacaData(creds["key_id"], creds["secret_key"]).stock_bars(
                    symbol, timeframe, start=start, limit=min(limit, 10000))
                if bars:
                    # Write through HERE, not later: this branch returns, so a
                    # cache filled anywhere below would never be written on the
                    # happy path and would only ever be empty when needed.
                    try:
                        with state.db() as db:
                            keep = settings_mod.get_all(db, s.user_id).get(
                                "equity_cache_days", "30")
                        con = state.market()
                        try:
                            marketdb.bar_cache_store(
                                con, symbol, timeframe, bars,
                                marketdb.cache_keep_days(keep))
                        finally:
                            con.close()
                    except Exception:  # noqa: BLE001
                        # A cache write must never cost the user their chart.
                        LOG.info("bar cache write failed for %s", symbol,
                                 exc_info=True)
                    return {"symbol": symbol, "timeframe": timeframe,
                            "bars": bars[-limit:], "source": "alpaca (IEX)"}
            except brokers_base.BrokerError as e:
                LOG.info("bars via alpaca failed for %s: %s", symbol, e)

        # This app's own cache of a previous live fetch. Ahead of rec_bars
        # because it is the same provider's data, just older.
        con = state.market()
        try:
            cached, fetched_at = marketdb.bar_cache_read(con, symbol, timeframe, limit)
        finally:
            con.close()
        if cached:
            return {"symbol": symbol, "timeframe": timeframe, "bars": cached,
                    "source": f"cached (fetched {fetched_at})"}

        # Recorded store — whatever the user's own jobs captured.
        con = state.market()
        try:
            rows = con.execute(
                "SELECT ts, open, high, low, close, volume FROM rec_bars"
                " WHERE symbol=? AND timeframe=? ORDER BY ts DESC LIMIT ?",
                (symbol, timeframe, limit)).fetchall()
        finally:
            con.close()
        if rows:
            bars = [{"ts": r["ts"], "open": r["open"], "high": r["high"],
                     "low": r["low"], "close": r["close"], "volume": r["volume"]}
                    for r in reversed(rows)]
            return {"symbol": symbol, "timeframe": timeframe, "bars": bars,
                    "source": "your recorded data"}

        if timeframe == "1Day" and market.YahooProvider is not None:
            try:
                # Period sized to the ask: 'max' is the whole listing history.
                period = ("max" if want_all else
                          "1y" if limit <= 251 else
                          "5y" if limit <= 1250 else "max")
                bars = market.YahooProvider().daily_bars(symbol, period=period)
                if bars:
                    return {"symbol": symbol, "timeframe": timeframe,
                            "bars": bars[-limit:], "source": "yahoo (delayed)"}
            except Exception:  # noqa: BLE001
                LOG.info("yahoo bars failed for %s", symbol, exc_info=True)

        return {"symbol": symbol, "timeframe": timeframe, "bars": [],
                "source": "none",
                "reason": "no data source available — add an Alpaca account, "
                          "or record bars from Data management"}

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

    @app.get("/api/article")
    def article(id: int | None = None, url: str = "",
                s=Depends(current_session)) -> dict[str, Any]:
        """Readable article text. Feed body first (it is already local and
        exact), page extraction second, and an honest 'read it on the site'
        when neither works — never a blank page."""
        con = state.market()
        try:
            item = newsstore.get(con, id) if id is not None else None
            if item is None and not url:
                raise HTTPException(404, "no such article")

            target = url or (item or {}).get("url", "")
            body = reader.html_to_text((item or {}).get("content", ""))

            if len(body) < 400 and target:
                got = reader.extract(target)
                if got and len(got["text"]) > len(body):
                    body = got["text"]
                    if item is not None:
                        # Cache it: the next read is instant and offline.
                        newsstore.set_content(con, item["id"], body)

            return {
                "id": (item or {}).get("id"),
                "headline": (item or {}).get("headline") or "",
                "source": (item or {}).get("source") or "",
                "symbols": (item or {}).get("symbols") or [],
                "created_at": (item or {}).get("created_at") or "",
                "url": target,
                "text": body,
                "readable": len(body) >= 400,
                "reason": "" if len(body) >= 400 else
                          "This item has no article body — open the original to read it.",
            }
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
            if not 1 <= body.retention_days <= 3650:
                raise HTTPException(422, "retention must be 1..3650 days")
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

    @app.get("/api/notepad")
    def notepad_list(s=Depends(current_session)) -> list[dict[str, Any]]:
        with state.db() as db:
            return notepad_mod.list_entries(db, s.user_id)

    @app.get("/api/notepad/summaries")
    def notepad_summaries(s=Depends(current_session)) -> list[dict[str, Any]]:
        """What the post wheel builds its segments from — never payloads."""
        with state.db() as db:
            return notepad_mod.summaries(db, s.user_id)

    @app.post("/api/notepad")
    def notepad_add(body: NotepadAddIn, s=Depends(current_session)) -> dict[str, Any]:
        try:
            with state.db() as db:
                return notepad_mod.add(db, s.user_id, body.payload, body.label)
        except notepad_mod.NotepadError as e:
            raise HTTPException(422, str(e)) from None

    @app.patch("/api/notepad/{entry_id}")
    def notepad_edit(entry_id: str, body: NotepadEditIn,
                     s=Depends(current_session)) -> dict[str, Any]:
        try:
            with state.db() as db:
                return notepad_mod.edit(db, s.user_id, entry_id,
                                        body.payload, body.label)
        except notepad_mod.NotepadError as e:
            raise HTTPException(422, str(e)) from None

    @app.delete("/api/notepad/{entry_id}")
    def notepad_remove(entry_id: str, s=Depends(current_session)) -> dict[str, Any]:
        with state.db() as db:
            return {"removed": notepad_mod.remove(db, s.user_id, entry_id)}

    @app.get("/api/syskey")
    def syskey_status(s=Depends(current_session)) -> dict[str, Any]:
        return syskey_mod.status()

    @app.post("/api/syskey")
    def syskey_enrol(body: SystemKeyIn, s=Depends(current_session)) -> dict[str, Any]:
        """Probe the key, then seal it — in that order, and never the reverse.

        The probe is what makes this more than a text box: a key is stored for
        unattended use only when it has been shown it cannot move real money,
        or when the user has explicitly accepted that it can."""
        if not syskey_mod.available():
            raise HTTPException(422, "unattended recording needs Windows on this build")
        p = keyprobe_mod.probe(body.key_id, body.secret_key)
        v = p["verdict"]
        if v == keyprobe_mod.UNDETERMINED:
            raise HTTPException(422, p["detail"])
        if v == keyprobe_mod.LIVE_CAPABLE and not body.accept_live_risk:
            # Not stored. The caller must come back having shown the user the
            # sentence, so consent is to the consequence rather than to a
            # checkbox they met before the fact.
            raise HTTPException(409, p["detail"] + " Confirm to store it anyway.")
        syskey_mod.store(body.key_id, body.secret_key, v,
                         dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         user_id=s.user_id)
        return {**syskey_mod.status(), "probe": p}

    @app.post("/api/syskey/probe")
    def syskey_probe(body: SystemKeyIn, s=Depends(current_session)) -> dict[str, Any]:
        """What would happen — stores nothing. Lets the UI show the verdict
        before asking the user to commit to it."""
        return keyprobe_mod.probe(body.key_id, body.secret_key)

    @app.get("/api/datamgmt/coverage")
    def coverage_report(symbol: str = "", kind: str = "bars",
                        timeframe: str = "1Day",
                        s=Depends(current_session)) -> dict[str, Any]:
        """What we have, what is genuinely absent, and how much is left.

        Reports `remaining` from COVERAGE, not from a truncated plan: a run
        capped at MAX_CHUNKS still has to state the real size of the job, or
        the progress a user sees is a number that means nothing."""
        sym = symbol.upper().strip()
        if not sym:
            raise HTTPException(422, "symbol required")
        con = state.market()
        try:
            with state.db() as db:
                years = settings_mod.get_all(db, s.user_id).get("backfill_years", "2")
            start, end = _backfill_range(str(years))
            out = coverage_mod.summary(con, kind, sym, timeframe)
            out["window"] = {"start": start.isoformat(), "end": end.isoformat()}
            out["remaining"] = backfill_mod.remaining(
                con, "alpaca-iex", kind, sym, timeframe, start, end)
            return out
        finally:
            con.close()

    @app.get("/api/datamgmt/backfill")
    def backfill_plan(symbol: str = "", kind: str = "bars",
                      timeframe: str = "1Day",
                      s=Depends(current_session)) -> dict[str, Any]:
        """The work list, WITHOUT running it. A backfill that cannot be
        inspected before it spends the API budget is one the user has to
        trust blindly."""
        sym = symbol.upper().strip()
        if not sym:
            raise HTTPException(422, "symbol required")
        con = state.market()
        try:
            with state.db() as db:
                cfg = settings_mod.get_all(db, s.user_id)
            start, end = _backfill_range(str(cfg.get("backfill_years", "2")))
            chunks = backfill_mod.plan(con, "alpaca-iex", kind, sym, timeframe,
                                       start, end)
            total = backfill_mod.remaining(con, "alpaca-iex", kind, sym,
                                           timeframe, start, end)
            planned_days = sum(len(c.days) for c in chunks)
            return {
                "enabled": bool(cfg.get("backfill_enabled", False)),
                "symbol": sym, "kind": kind, "timeframe": timeframe,
                "chunks": [c.as_dict() for c in chunks],
                "planned_days": planned_days,
                "remaining_days": total,
                # NO SILENT CAPS. If one run cannot cover the whole gap, the
                # UI must be able to say so rather than showing a plan that
                # looks complete.
                "truncated": planned_days < total,
            }
        finally:
            con.close()

    @app.get("/api/datamgmt/autostart")
    def autostart_status(s=Depends(current_session)) -> dict[str, Any]:
        return autostart_mod.status()

    @app.post("/api/datamgmt/autostart")
    def autostart_on(s=Depends(current_session)) -> dict[str, Any]:
        try:
            return autostart_mod.register()
        except RuntimeError as e:
            raise HTTPException(422, str(e)) from None

    @app.delete("/api/datamgmt/autostart")
    def autostart_off(s=Depends(current_session)) -> dict[str, Any]:
        return autostart_mod.unregister()

    @app.delete("/api/syskey")
    def syskey_remove(s=Depends(current_session)) -> dict[str, Any]:
        syskey_mod.remove()
        return syskey_mod.status()

    @app.post("/api/datamgmt/import")
    def data_import(body: ImportIn, s=Depends(current_session)) -> dict[str, Any]:
        """Start a file import. Returns immediately; poll for the outcome."""
        if body.kind not in ("option_chain", "bars"):
            raise HTTPException(422, "kind must be 'option_chain' or 'bars' — "
                                     "it is declared, never guessed")
        if not Path(body.path).is_file():
            raise HTTPException(422, f"no such file: {body.path}")
        if not state.datajobs.start_import(body.path, body.kind, body.underlying):
            raise HTTPException(409, "an import is already running")
        return state.datajobs.import_status

    @app.get("/api/datamgmt/import")
    def data_import_status(s=Depends(current_session)) -> dict[str, Any]:
        return state.datajobs.import_status

    @app.get("/api/datamgmt/pull")
    def chain_pull_status(s=Depends(current_session)) -> dict[str, Any]:
        return state.datajobs.pull_status

    @app.post("/api/datamgmt/pull")
    def chain_pull_start(body: PullIn, s=Depends(current_session)) -> dict[str, Any]:
        ok, note = state.datajobs.start_pull(
            body.symbols, body.start, body.end, body.seconds_between)
        if not ok:
            raise HTTPException(422, note)
        return state.datajobs.pull_status

    @app.delete("/api/datamgmt/pull")
    def chain_pull_stop(s=Depends(current_session)) -> dict[str, Any]:
        state.datajobs.stop_pull()
        return state.datajobs.pull_status

    @app.get("/api/datamgmt/usage")
    def datamgmt_usage(s=Depends(current_session)) -> dict[str, Any]:
        if state.recorder is not None:
            return state.recorder.usage()
        con = state.market()
        try:
            return recorder_mod.Recorder(con, lambda _uid: None).usage()
        finally:
            con.close()

    # ----------------------------------------------------------- backtests
    def _bt_paths(user_id: int, underlying: str = "SPY") -> dict[str, str]:
        with state.db() as db:
            return backtests_mod.engine_paths(
                settings_mod.get_all(db, user_id), underlying)

    @app.get("/api/backtests/status")
    def bt_status(underlying: str = "SPY",
                  s=Depends(current_session)) -> dict[str, Any]:
        with state.db() as db:
            status = backtests_mod.data_status(
                settings_mod.get_all(db, s.user_id), underlying)
        status["active"] = state.backtests.active()
        status["sync"] = state.backtests.sync_status()
        return status

    @app.post("/api/backtests/data/sync")
    def bt_data_sync(body: BtDataIn, s=Depends(current_session)) -> dict[str, Any]:
        started = state.backtests.sync_now(body.underlying)
        return {"ok": True, "started": started}

    @app.post("/api/backtests/data/setup-recording")
    def bt_setup_recording(body: BtDataIn,
                           s=Depends(current_session)) -> dict[str, Any]:
        """One click wires the recorder into the backtest store: an hourly
        chain snapshot plus daily bars for the underlying, long retention.
        The sync (before every 'recorded' run, or manual) does the rest —
        the user never assembles a database by hand."""
        symbol = body.underlying.upper().strip()
        # Deliberately NOT gated on the universe: a fresh install has not
        # synced it yet (no creds), and that is exactly when this button is
        # clicked. A wrong symbol just makes jobs that report their failure
        # every tick — visible, fixable, harmless.
        entry = state.universe.exact(symbol)
        wanted = [
            # kind, timeframe, interval, retention_days
            ("chain", "", 3600, 1825),
            ("bars", "1Day", 86400, 1825),
        ]
        con = state.market()
        created = []
        try:
            with con:
                for kind, timeframe, interval, retention in wanted:
                    have = con.execute(
                        "SELECT id FROM record_jobs WHERE user_id=? AND kind=?"
                        " AND symbol=? AND timeframe=?",
                        (s.user_id, kind, symbol, timeframe),
                    ).fetchone()
                    if have:
                        continue
                    err = recorder_mod.validate_job(
                        kind, symbol, timeframe, interval, retention,
                        (entry or {}).get("asset_class") if entry else None)
                    if err:
                        raise HTTPException(422, err)
                    con.execute(
                        "INSERT INTO record_jobs (user_id, kind, symbol, timeframe,"
                        " interval_seconds, retention_days) VALUES (?,?,?,?,?,?)",
                        (s.user_id, kind, symbol, timeframe, interval, retention),
                    )
                    created.append(kind)
        finally:
            con.close()
        return {"ok": True, "created": created,
                "note": "jobs record while you are signed in with a data-capable"
                        " account; runs sync the recorded snapshots automatically"}

    @app.get("/api/backtests/vocab")
    def bt_vocab(s=Depends(current_session)) -> dict[str, Any]:
        return backtests_mod.vocab()

    @app.post("/api/backtests/validate")
    def bt_validate(body: SpecIn, s=Depends(current_session)) -> dict[str, Any]:
        try:
            return backtests_mod.validate_spec(body.spec)
        except (backtests_mod.RuleError, ValueError) as exc:
            # SpecError subclasses ValueError; both carry the engine's own
            # error text (unknown keys/variables plus what IS available).
            return {"ok": False, "error": str(exc)}

    @app.get("/api/backtests/presets")
    def bt_presets(s=Depends(current_session)) -> list[dict[str, Any]]:
        with state.db() as db:
            backtests_mod.seed_presets(db, s.user_id)
            rows = db.execute(
                "SELECT id, name, spec, builtin, calibration, created_at, updated_at"
                " FROM backtest_presets WHERE user_id=? ORDER BY builtin DESC, id",
                (s.user_id,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["spec"] = json.loads(d["spec"])
            out.append(d)
        return out

    @app.post("/api/backtests/presets")
    def bt_preset_create(body: PresetIn, s=Depends(current_session)) -> dict[str, Any]:
        try:
            backtests_mod.validate_spec(body.spec)
        except (backtests_mod.RuleError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from None
        with state.db() as db:
            try:
                cur = db.execute(
                    "INSERT INTO backtest_presets (user_id, name, spec) VALUES (?,?,?)",
                    (s.user_id, body.name, json.dumps(body.spec)),
                )
            except sqlite3.IntegrityError:
                raise HTTPException(409, f"a preset named {body.name!r} exists") from None
            return {"id": cur.lastrowid, "ok": True}

    @app.patch("/api/backtests/presets/{preset_id}")
    def bt_preset_patch(preset_id: int, body: PresetPatch,
                        s=Depends(current_session)) -> dict[str, Any]:
        if body.spec is not None:
            try:
                backtests_mod.validate_spec(body.spec)
            except (backtests_mod.RuleError, ValueError) as exc:
                raise HTTPException(422, str(exc)) from None
        with state.db() as db:
            row = db.execute(
                "SELECT builtin FROM backtest_presets WHERE id=? AND user_id=?",
                (preset_id, s.user_id),
            ).fetchone()
            if row is None:
                raise HTTPException(404, "no such preset")
            if row["builtin"]:
                raise HTTPException(409, "built-in preset — duplicate it to edit")
            sets, vals = [], []
            if body.name is not None:
                sets.append("name=?"); vals.append(body.name)
            if body.spec is not None:
                sets.append("spec=?"); vals.append(json.dumps(body.spec))
            if not sets:
                raise HTTPException(422, "nothing to change")
            sets.append("updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')")
            try:
                db.execute(
                    f"UPDATE backtest_presets SET {', '.join(sets)}"
                    " WHERE id=? AND user_id=?", (*vals, preset_id, s.user_id))
            except sqlite3.IntegrityError:
                raise HTTPException(409, f"a preset named {body.name!r} exists") from None
        return {"ok": True}

    @app.delete("/api/backtests/presets/{preset_id}")
    def bt_preset_delete(preset_id: int, s=Depends(current_session)) -> dict[str, Any]:
        with state.db() as db:
            row = db.execute(
                "SELECT builtin FROM backtest_presets WHERE id=? AND user_id=?",
                (preset_id, s.user_id),
            ).fetchone()
            if row is None:
                raise HTTPException(404, "no such preset")
            if row["builtin"]:
                raise HTTPException(409, "built-in preset — it stays as the reference")
            db.execute("DELETE FROM backtest_presets WHERE id=? AND user_id=?",
                       (preset_id, s.user_id))
        return {"ok": True}

    @app.post("/api/backtests/runs")
    def bt_run_start(body: RunIn, s=Depends(current_session)) -> dict[str, Any]:
        if body.kind not in ("run", "calibration"):
            raise HTTPException(422, f"unknown kind {body.kind!r}")
        spec = None
        name = body.name
        for label, value in (("start", body.start), ("end", body.end)):
            if value is not None:
                try:
                    dt.date.fromisoformat(value)
                except ValueError:
                    raise HTTPException(
                        422, f"{label} must be an ISO date (YYYY-MM-DD), got {value!r}"
                    ) from None
        if body.kind == "run":
            if body.preset_id is not None:
                with state.db() as db:
                    row = db.execute(
                        "SELECT name, spec FROM backtest_presets WHERE id=? AND user_id=?",
                        (body.preset_id, s.user_id),
                    ).fetchone()
                if row is None:
                    raise HTTPException(404, "no such preset")
                spec = json.loads(row["spec"])
                name = name or row["name"]
            elif body.spec is not None:
                spec = body.spec
            else:
                raise HTTPException(422, "give a preset_id or an inline spec")
            try:
                v = backtests_mod.validate_spec(spec)
            except (backtests_mod.RuleError, ValueError) as exc:
                raise HTTPException(422, str(exc)) from None
            name = name or v["name"]
        else:
            name = name or "engine calibration"
            if not backtests_mod.data_status()["calibration"]["references"]:
                raise HTTPException(422, "no calibration references found")
        # The data source follows the strategy's underlying; the honesty
        # check is data_status's, so the page and this refusal always agree.
        underlying = str((spec or {}).get("underlying", "SPY"))
        with state.db() as db:
            st = backtests_mod.data_status(
                settings_mod.get_all(db, s.user_id), underlying)
        if not st["can_run"]:
            raise HTTPException(422, st["reason"] or "no chain data available")
        if body.kind == "calibration" and st["source"] == "recorded":
            raise HTTPException(
                422, "calibration needs the 2013+ reference chain database — "
                     "recorded data cannot reproduce the known trades")
        paths = _bt_paths(s.user_id, underlying)
        try:
            return state.backtests.start(
                s.user_id, body.kind, spec, name,
                body.preset_id if body.kind == "run" else None,
                body.start, body.end, paths)
        except backtests_mod.RunActive:
            raise HTTPException(409, "a backtest is already running") from None
        except backtests_mod.RunnerUnavailable:
            raise HTTPException(
                501, "backtests are not available in the packaged build yet"
            ) from None
        except RuntimeError as exc:
            raise HTTPException(500, str(exc)) from None

    @app.get("/api/backtests/runs")
    def bt_runs(s=Depends(current_session)) -> list[dict[str, Any]]:
        con = state.market()
        try:
            # calib is IN the list on purpose: the Verify-engine scorecard
            # renders from here, and omitting it silently blanked that card
            # (review 2026-08-03). trades/daily stay detail-only — those are
            # the bulky columns.
            rows = con.execute(
                "SELECT id, preset_id, name, kind, status, started_at,"
                " finished_at, error, summary, calib, report_files"
                " FROM backtest_runs"
                " WHERE user_id=? ORDER BY id DESC LIMIT 100", (s.user_id,)
            ).fetchall()
        finally:
            con.close()
        active = state.backtests.active()
        out = []
        for r in rows:
            d = dict(r)
            d["summary"] = json.loads(d["summary"])
            d["calib"] = json.loads(d["calib"])
            d["report_files"] = json.loads(d["report_files"])
            if active and active["id"] == d["id"]:
                d["progress"] = active["progress"]
            out.append(d)
        return out

    @app.get("/api/backtests/runs/{run_id}")
    def bt_run_get(run_id: int, s=Depends(current_session)) -> dict[str, Any]:
        con = state.market()
        try:
            row = con.execute(
                "SELECT id, preset_id, name, kind, status, started_at, finished_at,"
                " error, summary, calib, report_files, spec FROM backtest_runs"
                " WHERE id=? AND user_id=?", (run_id, s.user_id)
            ).fetchone()
        finally:
            con.close()
        if row is None:
            raise HTTPException(404, "no such run")
        d = dict(row)
        for k in ("summary", "calib", "report_files"):
            d[k] = json.loads(d[k])
        d["spec"] = json.loads(d["spec"]) if d["spec"] else None
        active = state.backtests.active()
        if active and active["id"] == run_id:
            d["progress"] = active["progress"]
        return d

    @app.get("/api/backtests/runs/{run_id}/result")
    def bt_run_result(run_id: int, s=Depends(current_session)) -> dict[str, Any]:
        con = state.market()
        try:
            row = con.execute(
                "SELECT trades, daily FROM backtest_runs WHERE id=? AND user_id=?",
                (run_id, s.user_id),
            ).fetchone()
        finally:
            con.close()
        if row is None:
            raise HTTPException(404, "no such run")
        return {"trades": json.loads(row["trades"]), "daily": json.loads(row["daily"])}

    @app.post("/api/backtests/runs/{run_id}/cancel")
    def bt_run_cancel(run_id: int, s=Depends(current_session)) -> dict[str, Any]:
        # Cancel and delete are SEPARATE verbs on purpose: one endpoint doing
        # cancel-else-delete meant a Cancel click racing a finishing run
        # silently destroyed its results (review 2026-08-03).
        if not state.backtests.cancel(run_id):
            raise HTTPException(409, "run is not active — it already finished")
        return {"ok": True, "cancelled": True}

    @app.delete("/api/backtests/runs/{run_id}")
    def bt_run_delete(run_id: int, s=Depends(current_session)) -> dict[str, Any]:
        if state.backtests.is_active(run_id):
            raise HTTPException(409, "run is active — cancel it first")
        if not state.backtests.delete_run(run_id, s.user_id):
            raise HTTPException(404, "no such run")
        return {"ok": True}

    @app.post("/api/backtests/runs/{run_id}/report-key")
    def bt_report_key(run_id: int, body: dict[str, Any] | None = None,
                      s=Depends(current_session)) -> dict[str, Any]:
        con = state.market()
        try:
            row = con.execute(
                "SELECT report_files FROM backtest_runs WHERE id=? AND user_id=?",
                (run_id, s.user_id),
            ).fetchone()
        finally:
            con.close()
        if row is None:
            raise HTTPException(404, "no such run")
        files = json.loads(row["report_files"])
        if not files:
            raise HTTPException(404, "run has no report")
        wanted = (body or {}).get("file") or files[0]
        if wanted not in files:  # whitelist — never a path from the client
            raise HTTPException(404, "no such report file")
        # 'report_key', never 'token': the IPC proxy scrubs token-shaped keys
        # from every response body before the renderer sees it.
        return {"report_key": state.backtests.mint_report_key(run_id, wanted)}

    @app.get("/api/backtests/report/{run_id}")
    def bt_report(run_id: int, k: str = "") -> FileResponse:
        # No session dependency and no app token (middleware exemption): the
        # single-use key IS the whole authorization, by design minted seconds
        # earlier by Electron main over the authed API.
        filename = state.backtests.consume_report_key(run_id, k) if k else None
        if filename is None:
            raise HTTPException(403, "report key missing, expired, or used")
        path = Path(backtests_mod.engine_paths()["out_root"]) / str(run_id) / filename
        if not path.is_file():
            raise HTTPException(404, "report file is gone")
        return FileResponse(path, media_type="text/html")

    return app
