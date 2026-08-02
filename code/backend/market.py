"""Quote/provider service: which source answers for which instrument.

Provider policy (REQUIREMENTS.md 6.9 mid-mark policy, narrowed to M-search
scope): an enabled Alpaca account answers for us_equity; the keyless Yahoo
fallback answers when no data-capable account exists; index/futures have no
connected source yet and say so instead of faking a number.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import security
from .brokers.alpaca_data import AlpacaData
from .brokers.base import BrokerError

try:  # optional keyless fallback; absence must not break the app
    from .providers.yahoo import YahooProvider
except ImportError:  # pragma: no cover
    YahooProvider = None  # type: ignore[assignment]

_KIND_PREFERENCE = {"data": 0, "paper": 1, "live": 2}


def alpaca_creds_for(db: sqlite3.Connection, user_id: int, dek: bytes) -> dict[str, str] | None:
    """Decrypt the best Alpaca account's keys for this user. Preference:
    data > paper > live — quotes should burn the data account's rate limit,
    not the trading account's."""
    rows = db.execute(
        "SELECT id, kind FROM accounts WHERE user_id=? AND broker='alpaca' AND enabled=1",
        (user_id,),
    ).fetchall()
    if not rows:
        return None
    best = sorted(rows, key=lambda r: _KIND_PREFERENCE.get(r["kind"], 9))[0]
    secrets = db.execute(
        "SELECT field, blob FROM secrets WHERE account_id=?", (best["id"],)
    ).fetchall()
    creds = {
        r["field"]: security.decrypt_secret(dek, r["blob"], user_id, best["id"], r["field"])
        for r in secrets
    }
    if "key_id" not in creds or "secret_key" not in creds:
        return None
    return {"key_id": creds["key_id"], "secret_key": creds["secret_key"]}


def quote_for(symbol: str, asset_class: str,
              creds: dict[str, str] | None) -> dict[str, Any]:
    """One quote, honestly labeled with its source. Never raises."""
    if asset_class in ("index", "future"):
        return {"symbol": symbol, "available": False,
                "reason": f"no connected source carries {asset_class} quotes yet"}
    if creds is not None:
        try:
            q = AlpacaData(creds["key_id"], creds["secret_key"]).stock_snapshot(symbol)
            return {**q, "available": q.get("price") is not None,
                    "source": "alpaca (IEX)"}
        except BrokerError as e:
            # fall through to yahoo with the alpaca error noted
            alpaca_err = str(e)
    else:
        alpaca_err = None
    if YahooProvider is not None:
        try:
            q = YahooProvider().quote(symbol)
            if q is not None:
                return {**q, "available": q.get("price") is not None,
                        "source": "yahoo (delayed)"}
        except Exception:  # noqa: BLE001 — fallback of a fallback stays quiet
            pass
    return {"symbol": symbol, "available": False,
            "reason": alpaca_err or "no data source configured — add an Alpaca account or rely on Yahoo fallback"}


def provider_status(has_alpaca: bool) -> dict[str, Any]:
    return {
        "equities": "alpaca (IEX)" if has_alpaca else
                    ("yahoo (delayed)" if YahooProvider is not None else "none"),
        "options_chains": "alpaca (indicative)" if has_alpaca else "none",
        "news": "alpaca (Benzinga)" if has_alpaca else "none",
        "futures": "none — arrives with the TastyTrade adapter",
        "index": "none — arrives with the TastyTrade adapter",
        "yahoo_fallback": YahooProvider is not None,
    }
