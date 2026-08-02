"""Alpaca adapter. M1: auth check + account summary. READ-ONLY BY GATE:
selftest asserts this module contains no order-placement code — extending it
with order endpoints is a deliberate, gated act in the trading milestone.

Pure `parse_*` functions take already-decoded JSON and return plain dicts;
only `_get` touches the network (the split is what makes the gate offline).
"""
from __future__ import annotations

from typing import Any

import httpx

from .base import BrokerAdapter, BrokerError

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"

CAPABILITIES: dict[str, Any] = {
    "asset_classes": ["us_equity", "us_option", "crypto"],
    "paper": True,
    "news": True,
    "order_entry": False,  # M1 is read-only; flips in the trading milestone
}


def parse_account(j: dict[str, Any]) -> dict[str, Any]:
    """Normalize GET /v2/account. Tolerates missing fields; never raises on
    absent optionals — a partial account still renders."""
    if not isinstance(j, dict) or "status" not in j:
        raise BrokerError("alpaca: unrecognized account payload")

    def num(key: str) -> float | None:
        v = j.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    number = str(j.get("account_number") or "")
    return {
        "broker": "alpaca",
        "status": j.get("status"),
        "currency": j.get("currency") or "USD",
        "equity": num("equity"),
        "cash": num("cash"),
        "buying_power": num("buying_power"),
        "options_buying_power": num("options_buying_power"),
        "options_level": j.get("options_approved_level"),
        "pattern_day_trader": bool(j.get("pattern_day_trader", False)),
        "daytrade_count": j.get("daytrade_count"),
        "account_last4": number[-4:] if number else "",
        "blocked": bool(j.get("trading_blocked") or j.get("account_blocked")),
    }


class AlpacaAdapter(BrokerAdapter):
    name = "alpaca"
    capabilities = CAPABILITIES

    def __init__(self, key_id: str, secret_key: str, kind: str = "paper",
                 timeout: float = 10.0) -> None:
        if kind not in ("live", "paper", "data"):
            raise BrokerError(f"alpaca: unknown account kind {kind!r}")
        self._base = LIVE_URL if kind == "live" else PAPER_URL
        self._kind = kind
        self._headers = {
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret_key,
        }
        self._timeout = timeout

    def _get(self, path: str) -> Any:
        try:
            r = httpx.get(self._base + path, headers=self._headers,
                          timeout=self._timeout)
        except httpx.HTTPError as e:
            raise BrokerError(f"alpaca: network error — {e.__class__.__name__}") from e
        if r.status_code in (401, 403):
            raise BrokerError("alpaca: keys rejected (401/403) — check key id/secret and paper vs live")
        if r.status_code == 429:
            raise BrokerError("alpaca: rate limited (429) — 200 req/min cap")
        if r.status_code >= 400:
            raise BrokerError(f"alpaca: HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as e:  # a 200 carrying HTML (captive portal, proxy)
            raise BrokerError("alpaca: response was not JSON") from e

    def test_connection(self) -> dict[str, Any]:
        # A data-only account has no /v2/account; probe the data host instead.
        if self._kind == "data":
            try:
                r = httpx.get(
                    DATA_URL + "/v1beta1/news", params={"limit": 1},
                    headers=self._headers, timeout=self._timeout,
                )
            except httpx.HTTPError as e:
                return {"ok": False, "error": f"alpaca data: network error — {e.__class__.__name__}"}
            if r.status_code != 200:
                return {"ok": False, "error": f"alpaca data: HTTP {r.status_code}"}
            return {"ok": True, "broker": "alpaca", "kind": "data",
                    "detail": "news endpoint reachable"}
        try:
            acct = parse_account(self._get("/v2/account"))
        except BrokerError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "kind": self._kind, **acct}
