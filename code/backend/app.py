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
import sqlite3
import threading
import time
from contextlib import AbstractContextManager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

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


# ------------------------------------------------------------------ factory
def create_app(state: State) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        # The renderer says "backend error"; THIS says why. Diagnosing blind
        # twice was enough.
        LOG.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"detail": "internal error — see data/logs/backend.log"},
                            status_code=500)

    @app.middleware("http")
    async def require_app_token(request: Request, call_next):
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
        try:
            with state.db() as db:
                values = settings_mod.put(db, s.user_id, body)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        return {"values": values}

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

    @app.get("/api/symbols/{symbol}/bars")
    def symbol_bars(symbol: str, timeframe: str = "1Day", limit: int = 0,
                    s=Depends(current_session)) -> dict[str, Any]:
        """Chart data. Recorded bars first (the user's own store), topped up
        from the live provider; Yahoo daily as the keyless fallback. The
        response always names its source — a chart that lies about where its
        candles came from is worse than no chart.

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
                    return {"symbol": symbol, "timeframe": timeframe,
                            "bars": bars[-limit:], "source": "alpaca (IEX)"}
            except brokers_base.BrokerError as e:
                LOG.info("bars via alpaca failed for %s: %s", symbol, e)

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
