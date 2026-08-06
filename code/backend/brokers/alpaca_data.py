"""Alpaca MARKET DATA client (data.alpaca.markets). Read-only by nature and
by gate: like alpaca.py, this module must never grow order code.

Pure `parse_*` functions take decoded JSON and return plain dicts; only the
`AlpacaData._get` method touches the network. Field names follow the v2/v1beta1
docs; parsers tolerate missing optionals because the free indicative feed
omits fields the OPRA feed carries.
"""
from __future__ import annotations

from typing import Any, Iterator

import httpx

from .base import BrokerError

DATA_URL = "https://data.alpaca.markets"


# ------------------------------------------------------------------- parsers
def parse_assets(j: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(j, list):
        raise BrokerError("alpaca: assets payload is not a list")
    out = []
    for a in j:
        sym = a.get("symbol")
        if not sym:
            continue
        out.append({
            "symbol": sym,
            "name": (a.get("name") or "").strip(),
            "exchange": a.get("exchange") or "",
            "asset_class": a.get("class") or "us_equity",
            "tradable": bool(a.get("tradable", False)),
        })
    return out


def parse_news_item(n: dict[str, Any]) -> dict[str, Any]:
    if "id" not in n or "headline" not in n:
        raise BrokerError("alpaca: unrecognized news payload")
    return {
        "id": int(n["id"]),
        "headline": n.get("headline") or "",
        "summary": n.get("summary") or "",
        "source": n.get("source") or "",
        "url": n.get("url") or "",
        "symbols": [s for s in (n.get("symbols") or []) if isinstance(s, str)],
        "created_at": n.get("created_at") or "",
        "updated_at": n.get("updated_at") or n.get("created_at") or "",
        # HTML body when the feed carries one. Measured: ~44/50 articles do;
        # the rest are stubs (trading halts, rating one-liners) and get
        # extracted from their page on demand instead.
        "content": n.get("content") or "",
    }


def parse_stock_snapshot(symbol: str, j: dict[str, Any]) -> dict[str, Any]:
    """v2/stocks/snapshots response entry: latestTrade/latestQuote/dailyBar/
    prevDailyBar. Any block may be absent (free tier, off-hours)."""
    if not isinstance(j, dict):
        raise BrokerError("alpaca: unrecognized snapshot payload")
    trade = j.get("latestTrade") or {}
    quote = j.get("latestQuote") or {}
    daily = j.get("dailyBar") or {}
    prev = j.get("prevDailyBar") or {}
    price = trade.get("p")
    prev_close = prev.get("c")
    change = pct = None
    if isinstance(price, (int, float)) and isinstance(prev_close, (int, float)) and prev_close:
        change = price - prev_close
        pct = 100.0 * change / prev_close
    return {
        "symbol": symbol,
        "price": price,
        "bid": quote.get("bp"),
        "ask": quote.get("ap"),
        "ts": trade.get("t") or quote.get("t"),
        "day_open": daily.get("o"),
        "day_high": daily.get("h"),
        "day_low": daily.get("l"),
        "day_volume": daily.get("v"),
        "prev_close": prev_close,
        "change": change,
        "change_pct": pct,
    }


def parse_bars(symbol: str, j: dict[str, Any]) -> list[dict[str, Any]]:
    bars = j.get("bars")
    if bars is None:
        return []
    if isinstance(bars, dict):  # multi-symbol shape {sym: [...]}
        bars = bars.get(symbol, [])
    out = []
    for b in bars:
        out.append({
            "ts": b.get("t"), "open": b.get("o"), "high": b.get("h"),
            "low": b.get("l"), "close": b.get("c"), "volume": b.get("v"),
        })
    return out


def parse_chain_snapshot(underlying: str, j: dict[str, Any]) -> list[dict[str, Any]]:
    """v1beta1/options/snapshots/{underlying} response: {"snapshots":
    {occ_symbol: {latestQuote, latestTrade, impliedVolatility, greeks}}}.
    Greeks/IV may be absent on the indicative feed — kept as None, labeled
    upstream, never fabricated."""
    snaps = j.get("snapshots")
    if not isinstance(snaps, dict):
        raise BrokerError("alpaca: unrecognized chain payload")
    # OCC roots never contain dots: BRK.B options carry root "BRKB", and
    # corporate-action-adjusted series get a digit suffix ("BRKB1"). A naive
    # startswith(underlying) filtered ENTIRE chains of dotted-class shares to
    # zero rows (review 2026-08-02).
    clean_root = underlying.replace(".", "").upper()
    out = []
    for occ, s in snaps.items():
        parsed = parse_occ(occ)
        if parsed is None:
            continue
        root = parsed["root"].upper().rstrip("0123456789")
        if root != clean_root:
            continue
        quote = s.get("latestQuote") or {}
        trade = s.get("latestTrade") or {}
        greeks = s.get("greeks") or {}
        out.append({
            "occ_symbol": occ,
            "expiration": parsed["expiration"],
            "strike": parsed["strike"],
            "right": parsed["right"],
            "bid": quote.get("bp"), "ask": quote.get("ap"),
            "last": trade.get("p"),
            "iv": s.get("impliedVolatility"),
            "delta": greeks.get("delta"), "gamma": greeks.get("gamma"),
            "theta": greeks.get("theta"), "vega": greeks.get("vega"),
            "rho": greeks.get("rho"),
        })
    return out


def parse_occ(occ: str) -> dict[str, Any] | None:
    """AAPL241220C00150000 -> expiration 2024-12-20, right C, strike 150.0.
    Root may be 1-6 chars; date is 6 digits; strike is 8 digits in 1/1000."""
    if len(occ) < 16:
        return None
    tail = occ[-15:]
    date, right, strike_raw = tail[:6], tail[6], tail[7:]
    if right not in ("C", "P") or not date.isdigit() or not strike_raw.isdigit():
        return None
    return {
        "root": occ[:-15],
        "expiration": f"20{date[0:2]}-{date[2:4]}-{date[4:6]}",
        "right": right,
        "strike": int(strike_raw) / 1000.0,
    }


# -------------------------------------------------------------------- client
class AlpacaData:
    def __init__(self, key_id: str, secret_key: str, timeout: float = 15.0) -> None:
        self._headers = {
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret_key,
        }
        self._timeout = timeout

    def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        try:
            r = httpx.get(url, params=params or {}, headers=self._headers,
                          timeout=self._timeout)
        except httpx.HTTPError as e:
            raise BrokerError(f"alpaca data: network error — {e.__class__.__name__}") from e
        if r.status_code >= 400:
            # Carry Alpaca's own words. 401 and 403 mean different things -
            # rejected keys versus a key that works but is not entitled to THIS
            # feed - and lumping them sent a real debugging session looking at
            # the wrong half. The body is Alpaca's message, never our secret.
            detail = ""
            try:
                body = r.json()
                if isinstance(body, dict):
                    detail = str(body.get("message") or body.get("msg") or "")[:160]
            except ValueError:
                detail = r.text[:160]
            tail = f" - {detail}" if detail else ""
            if r.status_code == 401:
                raise BrokerError(f"alpaca data: keys rejected (401){tail}")
            if r.status_code == 403:
                raise BrokerError(
                    f"alpaca data: key not entitled to this data (403){tail}")
            if r.status_code == 429:
                raise BrokerError(f"alpaca data: rate limited (429){tail}")
            raise BrokerError(f"alpaca data: HTTP {r.status_code}{tail}")
        try:
            return r.json()
        except ValueError as e:
            raise BrokerError("alpaca data: response was not JSON") from e

    # Trading-API host, but reference data (no order surface).
    def iter_assets(self, base_url: str) -> Iterator[list[dict[str, Any]]]:
        j = self._get(base_url + "/v2/assets",
                      {"status": "active", "asset_class": "us_equity"})
        yield parse_assets(j)
        j = self._get(base_url + "/v2/assets",
                      {"status": "active", "asset_class": "crypto"})
        yield parse_assets(j)

    def news(self, symbols: list[str] | None = None, limit: int = 50,
             start: str | None = None, page_token: str | None = None,
             contentless: bool = False) -> tuple[list[dict], str | None]:
        # include_content=true is the whole point of a news reader; asking for
        # false is why articles opened empty. exclude_contentless drops the
        # headline-only stubs (verified: 50/50 keepers when enabled).
        params: dict[str, Any] = {
            "limit": min(limit, 50), "include_content": "true",
            "exclude_contentless": "false" if contentless else "true",
            "sort": "desc",
        }
        if symbols:
            params["symbols"] = ",".join(symbols)
        if start:
            params["start"] = start
        if page_token:
            params["page_token"] = page_token
        j = self._get(DATA_URL + "/v1beta1/news", params)
        items = [parse_news_item(n) for n in j.get("news", [])]
        return items, j.get("next_page_token")

    def stock_snapshot(self, symbol: str) -> dict[str, Any]:
        j = self._get(DATA_URL + f"/v2/stocks/{symbol}/snapshot", {"feed": "iex"})
        return parse_stock_snapshot(symbol, j)

    def stock_bars(self, symbol: str, timeframe: str, start: str,
                   limit: int = 1000) -> list[dict[str, Any]]:
        # Canonical multi-symbol form (research-verified 2026-08-02).
        # `end` is deliberately OMITTED: on the free tier an explicit end
        # inside the last 15 minutes is a 403 ("subscription does not permit
        # querying recent SIP data"); omitted, it silently clamps and 200s.
        # feed=iex additionally allows recent data on the free tier.
        j = self._get(DATA_URL + "/v2/stocks/bars",
                      {"symbols": symbol, "timeframe": timeframe, "start": start,
                       "limit": limit, "adjustment": "raw", "feed": "iex",
                       "sort": "asc"})
        return parse_bars(symbol, j)

    def chain_snapshot(self, underlying: str, feed: str = "indicative",
                       limit: int = 1000, *,
                       exp_gte: str | None = None, exp_lte: str | None = None,
                       strike_gte: float | None = None, strike_lte: float | None = None,
                       right: str | None = None) -> list[dict[str, Any]]:
        # Greeks + impliedVolatility ARE served on the free indicative feed
        # (live-verified), but are legitimately absent for 0DTE/zero-bid
        # contracts — parse_chain_snapshot keeps them optional.
        #
        # The filters ride to the SERVER (expiration_date_gte/lte,
        # strike_price_gte/lte, type): a leg's acceptance window is a few
        # hundred contracts, one page, instead of the ~10k-row full chain the
        # recorder pulls. Callers must still filter the PARSED rows —
        # options.filter_contracts — both as a guard against a provider
        # ignoring a param and because that pure function is the one the gate
        # can test offline.
        out: list[dict[str, Any]] = []
        token: str | None = None
        for _ in range(30):  # SPY carries >10k contracts; 30 pages = 30k cap
            params: dict[str, Any] = {"feed": feed, "limit": limit}
            if exp_gte:
                params["expiration_date_gte"] = exp_gte
            if exp_lte:
                params["expiration_date_lte"] = exp_lte
            if strike_gte is not None:
                params["strike_price_gte"] = strike_gte
            if strike_lte is not None:
                params["strike_price_lte"] = strike_lte
            if right in ("C", "P"):
                params["type"] = "call" if right == "C" else "put"
            if token:
                params["page_token"] = token
            j = self._get(DATA_URL + f"/v1beta1/options/snapshots/{underlying}", params)
            out.extend(parse_chain_snapshot(underlying, j))
            token = j.get("next_page_token")
            if not token:
                break
        return out
