"""TastyTrade adapter. READ-ONLY BY GATE like alpaca.py: selftest asserts this
module contains no order-placement code — order entry arrives in the trading
milestone deliberately, with its own checks. The expected credential is a
read-scope personal OAuth grant, so the API itself also refuses writes.

Auth (REQUIREMENTS.md 7.2): OAuth2 only. A never-expiring refresh token +
client secret mint 15-minute access tokens at /oauth/token; every request
carries a User-Agent (the API 403s without one). Access tokens are cached
module-wide, keyed by a digest of the refresh token, so the many short-lived
adapter instances (one per request, like AlpacaData) don't spend a token
round-trip each — measured ~0.4s per mint.

Pure `parse_*` functions take already-decoded JSON and return plain dicts;
only `_get`/`_mint` touch the network (the split keeps the gate offline).
All payload shapes below were captured live 2026-08-14 against the real API.
"""
from __future__ import annotations

import hashlib
import threading
import time
from typing import Any

import httpx

from .base import BrokerAdapter, BrokerError

API_URL = "https://api.tastytrade.com"
TOKEN_URL = API_URL + "/oauth/token"
USER_AGENT = "grindstone-desktop/1.0"
# Mint a fresh access token when the cached one has less life left than this.
TOKEN_SLACK = 60.0
ACTIVE_CONTRACT_TTL = 3600.0

CAPABILITIES: dict[str, Any] = {
    "asset_classes": ["us_equity", "us_option", "future", "future_option", "index"],
    "paper": False,  # sandbox is a separate cert host with separate creds
    "news": False,
    "order_entry": False,  # read-only; flips in the trading milestone
}

# access-token cache: digest(refresh token) -> (access_token, expires_at_epoch)
_TOKENS: dict[str, tuple[str, float]] = {}
# active-month contract cache: product code -> (contract symbol, fetched_at)
_ACTIVE: dict[str, tuple[str, float]] = {}
_LOCK = threading.Lock()


def _num(v: Any) -> float | None:
    """TastyTrade sends numbers as strings ("7802.5") or floats; coerce both."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _items(j: Any, context: str) -> list[dict[str, Any]]:
    """Every list endpoint wraps rows as {"data": {"items": [...]}}."""
    if not isinstance(j, dict) or not isinstance(j.get("data"), dict):
        raise BrokerError(f"tastytrade: unrecognized {context} payload")
    items = j["data"].get("items")
    if not isinstance(items, list):
        raise BrokerError(f"tastytrade: unrecognized {context} payload")
    return [i for i in items if isinstance(i, dict)]


def parse_token(j: Any) -> dict[str, Any]:
    """POST /oauth/token response: access_token, expires_in (900), token_type."""
    if not isinstance(j, dict) or not j.get("access_token"):
        raise BrokerError("tastytrade: unrecognized token payload")
    expires = j.get("expires_in")
    return {
        "access_token": str(j["access_token"]),
        "expires_in": float(expires) if isinstance(expires, (int, float)) else 900.0,
    }


def parse_accounts(j: Any) -> list[dict[str, Any]]:
    """GET /customers/me/accounts: items[].account. Only what the app needs —
    never the full account number (last-4 display rule, FR-AUTH-3)."""
    out = []
    for item in _items(j, "accounts"):
        a = item.get("account")
        if not isinstance(a, dict):
            continue
        number = str(a.get("account-number") or "")
        out.append({
            "broker": "tastytrade",
            "account_last4": number[-4:] if number else "",
            "nickname": a.get("nickname") or a.get("account-type-name") or "",
            "margin_or_cash": a.get("margin-or-cash"),
            "futures_approved": bool(a.get("is-futures-approved", False)),
            "options_level": a.get("suitable-options-level"),
            "day_trader": bool(a.get("day-trader-status", False)),
            "closed": bool(a.get("is-closed", False)),
            "authority": item.get("authority-level"),
        })
    return out


def parse_quotes(j: Any) -> list[dict[str, Any]]:
    """GET /market-data/by-type: one normalized quote per item. Serves every
    instrument type the endpoint carries (equity, index, future, future-option,
    equity-option) — verified live: SPX answers with real bid/ask."""
    out = []
    for q in _items(j, "market-data"):
        last = _num(q.get("last"))
        mark = _num(q.get("mark"))
        mid = _num(q.get("mid"))
        prev_close = _num(q.get("prev-close"))
        price = last if last is not None else (mark if mark is not None else mid)
        change = pct = None
        if price is not None and prev_close:
            change = price - prev_close
            pct = 100.0 * change / prev_close
        out.append({
            "symbol": q.get("symbol"),
            "instrument_type": q.get("instrument-type"),
            "price": price,
            "bid": _num(q.get("bid")),
            "ask": _num(q.get("ask")),
            "mark": mark,
            "mid": mid,
            "last": last,
            "day_open": _num(q.get("open")),
            "day_high": _num(q.get("day-high-price")),
            "day_low": _num(q.get("day-low-price")),
            "day_volume": _num(q.get("volume")),
            "prev_close": prev_close,
            "change": change,
            "change_pct": pct,
            "ts": q.get("updated-at"),
        })
    return out


def parse_future_contracts(j: Any) -> list[dict[str, Any]]:
    """GET /instruments/futures?product-code[]=X: every listed contract."""
    out = []
    for c in _items(j, "futures"):
        out.append({
            "symbol": c.get("symbol"),
            "product_code": c.get("product-code"),
            "expiration_date": c.get("expiration-date"),
            "active_month": bool(c.get("active-month", False)),
            "next_active_month": bool(c.get("next-active-month", False)),
            "streamer_symbol": c.get("streamer-symbol"),
            "exchange": c.get("exchange"),
            "tick_size": _num(c.get("tick-size")),
        })
    return out


def parse_nested_future_chain(j: Any) -> dict[str, Any]:
    """GET /futures-option-chains/{code}/nested → the tradeable structure:
    the product's futures and, per option chain, expirations with strikes.
    Settlement type and notional value ride along — wrong settlement produces
    wrong payoff math later (REQUIREMENTS.md 6.4)."""
    if not isinstance(j, dict) or not isinstance(j.get("data"), dict):
        raise BrokerError("tastytrade: unrecognized chain payload")
    d = j["data"]
    futures = [{
        "symbol": f.get("symbol"),
        "root_symbol": f.get("root-symbol"),
        "streamer_symbol": f.get("streamer-symbol"),
        "expiration_date": f.get("expiration-date"),
        "days_to_expiration": f.get("days-to-expiration"),
        "active_month": bool(f.get("active-month", False)),
    } for f in d.get("futures", []) if isinstance(f, dict)]
    chains = []
    for oc in d.get("option-chains", []) or []:
        if not isinstance(oc, dict):
            continue
        expirations = []
        for e in oc.get("expirations", []) or []:
            if not isinstance(e, dict):
                continue
            strikes = [{
                "strike": _num(s.get("strike-price")),
                "call": s.get("call"),
                "put": s.get("put"),
                "call_streamer_symbol": s.get("call-streamer-symbol"),
                "put_streamer_symbol": s.get("put-streamer-symbol"),
            } for s in e.get("strikes", []) or [] if isinstance(s, dict)]
            expirations.append({
                "underlying_symbol": e.get("underlying-symbol"),
                "option_root_symbol": e.get("option-root-symbol"),
                "expiration_date": e.get("expiration-date"),
                "days_to_expiration": e.get("days-to-expiration"),
                "expiration_type": e.get("expiration-type"),
                "settlement_type": e.get("settlement-type"),
                "notional_value": _num(e.get("notional-value")),
                "display_factor": _num(e.get("display-factor")),
                "strikes": strikes,
            })
        chains.append({
            "root_symbol": oc.get("root-symbol"),
            "underlying_symbol": oc.get("underlying-symbol"),
            "exercise_style": oc.get("exercise-style"),
            "expirations": expirations,
        })
    if not futures and not chains:
        raise BrokerError("tastytrade: chain payload had no futures and no chains")
    return {"futures": futures, "chains": chains}


class TastyTradeAdapter(BrokerAdapter):
    name = "tastytrade"
    capabilities = CAPABILITIES

    def __init__(self, client_secret: str, refresh_token: str,
                 kind: str = "data", timeout: float = 15.0) -> None:
        if kind == "paper":
            raise BrokerError(
                "tastytrade: the sandbox is a separate environment with its own "
                "credentials and low fidelity ($1 fills, 15-min quotes) — enroll "
                "this grant as 'live' or 'data' instead")
        if kind not in ("live", "data"):
            raise BrokerError(f"tastytrade: unknown account kind {kind!r}")
        self._secret = client_secret
        self._refresh = refresh_token
        self._kind = kind
        self._timeout = timeout
        self._cache_key = hashlib.sha256(refresh_token.encode()).hexdigest()[:32]

    # ------------------------------------------------------------------ auth
    def _mint(self) -> tuple[str, float]:
        """One access token from the refresh token. Raises BrokerError with a
        re-entry hint when the grant is dead — the UI shows this verbatim."""
        try:
            r = httpx.post(TOKEN_URL, headers={"User-Agent": USER_AGENT},
                           timeout=self._timeout, json={
                               "grant_type": "refresh_token",
                               "refresh_token": self._refresh,
                               "client_secret": self._secret,
                           })
        except httpx.HTTPError as e:
            raise BrokerError(
                f"tastytrade: network error — {e.__class__.__name__}") from e
        if r.status_code != 200:
            # Live shape: 400 {"error_code":"invalid_grant",...}
            raise BrokerError(
                "tastytrade: refresh token rejected — regenerate the grant on "
                "my.tastytrade.com and re-enter it (Accounts → Update token)"
                if r.status_code == 400 else
                f"tastytrade: token mint failed (HTTP {r.status_code})")
        try:
            tok = parse_token(r.json())
        except ValueError as e:
            raise BrokerError("tastytrade: token response was not JSON") from e
        return tok["access_token"], time.time() + tok["expires_in"]

    def _token(self, force: bool = False) -> str:
        with _LOCK:
            cached = _TOKENS.get(self._cache_key)
            if not force and cached and cached[1] - time.time() > TOKEN_SLACK:
                return cached[0]
            token, expires_at = self._mint()
            _TOKENS[self._cache_key] = (token, expires_at)
            return token

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        token = self._token()
        for attempt in (1, 2):
            try:
                r = httpx.get(API_URL + path, params=params, timeout=self._timeout,
                              headers={"User-Agent": USER_AGENT,
                                       "Authorization": f"Bearer {token}"})
            except httpx.HTTPError as e:
                raise BrokerError(
                    f"tastytrade: network error — {e.__class__.__name__}") from e
            if r.status_code == 401 and attempt == 1:
                # Cached token died early (revocation, clock skew): one re-mint.
                token = self._token(force=True)
                continue
            break
        if r.status_code in (401, 403):
            raise BrokerError("tastytrade: not authorized (401/403) — the grant "
                              "may be revoked; re-enter the refresh token")
        if r.status_code == 429:
            raise BrokerError("tastytrade: rate limited (429) — slow down")
        if r.status_code >= 400:
            raise BrokerError(f"tastytrade: HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as e:
            raise BrokerError("tastytrade: response was not JSON") from e

    # ----------------------------------------------------------------- reads
    def test_connection(self) -> dict[str, Any]:
        """Mint + list accounts. Deliberately /customers/me/accounts and NOT
        /customers/me: the latter returns the customer's home address, income,
        and employer — PII the app has no use for and must not touch."""
        try:
            accounts = parse_accounts(self._get("/customers/me/accounts"))
        except BrokerError as e:
            return {"ok": False, "error": str(e)}
        open_accounts = [a for a in accounts if not a["closed"]]
        if not open_accounts:
            return {"ok": False, "error": "tastytrade: grant is valid but no "
                                          "open account is visible on it"}
        first = open_accounts[0]
        return {
            "ok": True, "broker": "tastytrade", "kind": self._kind,
            "accounts": len(open_accounts),
            "account_last4": first["account_last4"],
            "margin_or_cash": first["margin_or_cash"],
            "futures_approved": any(a["futures_approved"] for a in open_accounts),
            "options_level": first["options_level"],
            "detail": f"{len(open_accounts)} account(s), "
                      f"futures {'approved' if first['futures_approved'] else 'not approved'}",
        }

    def quotes(self, by_type: dict[str, list[str]]) -> list[dict[str, Any]]:
        """Snapshot quotes for any mix of instrument types. Keys are the API's
        own type names: equity, index, future, future-option, equity-option."""
        params = {f"{t}[]": syms for t, syms in by_type.items() if syms}
        if not params:
            return []
        return parse_quotes(self._get("/market-data/by-type", params))

    def active_future_contract(self, root: str) -> str:
        """'/ES' (or 'ES') → the active-month contract symbol, e.g. '/ESU6'.
        Cached: the front month changes on the quarterly roll, not per quote."""
        code = root.lstrip("/").upper()
        now = time.time()
        with _LOCK:
            hit = _ACTIVE.get(code)
            if hit and now - hit[1] < ACTIVE_CONTRACT_TTL:
                return hit[0]
        contracts = parse_future_contracts(
            self._get("/instruments/futures", {"product-code[]": code}))
        active = [c for c in contracts if c["active_month"] and c["symbol"]]
        if not active:
            # Fall back to the nearest unexpired contract rather than failing:
            # thinly-listed products may carry no active-month flag.
            dated = sorted((c for c in contracts if c["symbol"] and c["expiration_date"]),
                           key=lambda c: c["expiration_date"])
            if not dated:
                raise BrokerError(f"tastytrade: no listed contracts for {root!r}")
            active = dated[:1]
        symbol = active[0]["symbol"]
        with _LOCK:
            _ACTIVE[code] = (symbol, now)
        return symbol

    def future_snapshot(self, root: str) -> dict[str, Any]:
        """Quote the active-month contract of a futures root, snapshot-shaped
        (same keys parse_stock_snapshot yields, plus the contract symbol)."""
        contract = self.active_future_contract(root)
        rows = self.quotes({"future": [contract]})
        if not rows:
            raise BrokerError(f"tastytrade: no quote for {contract}")
        return {**rows[0], "symbol": root, "contract": contract}

    def index_snapshot(self, symbol: str) -> dict[str, Any]:
        rows = self.quotes({"index": [symbol.upper()]})
        if not rows:
            raise BrokerError(f"tastytrade: no index quote for {symbol}")
        return rows[0]

    def nested_future_chain(self, root: str) -> dict[str, Any]:
        code = root.lstrip("/").upper()
        return parse_nested_future_chain(self._get(f"/futures-option-chains/{code}/nested"))
