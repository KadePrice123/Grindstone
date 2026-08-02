"""Per-user settings, stored in app.db beside the account rows.

Small and typed on purpose: every setting declares its default, its kind, and
its bounds, so the API can validate without a schema library and the UI can
render itself from the same declaration.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

SPEC: dict[str, dict[str, Any]] = {
    "web_search_enabled": {
        "kind": "bool", "default": True,
        "label": "Search the web",
        "help": "Blend DuckDuckGo results into search. Off = platform data only.",
    },
    "web_news_enabled": {
        "kind": "bool", "default": True,
        "label": "Web news in results",
        "help": "Include web news articles alongside your broker's news feed.",
    },
    "web_search_engine": {
        "kind": "choice", "default": "brave",
        "choices": ["brave", "auto", "bing", "startpage", "duckduckgo"],
        "label": "Web search engine",
        "help": "Measured here: DuckDuckGo currently returns nothing, so it is "
                "listed only for completeness. 'brave' is the narrowest option "
                "that works; 'auto' fans out across every engine ddgs supports "
                "for the best coverage, including some whose terms discourage "
                "automated queries.",
    },
    "inhouse_boost": {
        "kind": "float", "default": 1.0, "min": 0.0, "max": 4.0, "step": 0.25,
        "label": "In-house result boost",
        "help": "How strongly tickers, your news store and app pages outrank web "
                "results. 0 = no preference, 4 = platform results dominate.",
    },
    "open_web_in_app": {
        "kind": "bool", "default": True,
        "label": "Open links in the app",
        "help": "Web pages open as in-app browser tabs instead of your OS browser.",
    },
    "theme": {
        "kind": "choice", "default": "dark", "choices": ["dark", "light"],
        "label": "Theme",
        "help": "Applies to the whole app.",
    },
    # Hidden state blobs: not rendered by the generic settings page (their
    # owning UI edits them), but stored/validated through the same door.
    "multi_chart": {
        "kind": "json", "default": {"symbols": ["SPY"], "normalize": True,
                                    "timeframe": "1Day", "hidden": []},
        "label": "Multi-chart state", "help": "", "hidden": True,
    },
}


def _coerce(key: str, value: Any) -> Any:
    spec = SPEC[key]
    if spec["kind"] == "bool":
        return bool(value)
    if spec["kind"] == "float":
        v = float(value)
        return max(spec["min"], min(spec["max"], v))
    if spec["kind"] == "choice":
        return value if value in spec["choices"] else spec["default"]
    if spec["kind"] == "json":
        # An object, bounded — this is UI state, not a data store.
        if not isinstance(value, dict) or len(json.dumps(value)) > 8192:
            return spec["default"]
        return value
    return value


def defaults() -> dict[str, Any]:
    return {k: v["default"] for k, v in SPEC.items()}


def get_all(db: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    out = defaults()
    rows = db.execute(
        "SELECT key, value FROM user_settings WHERE user_id=?", (user_id,)
    ).fetchall()
    for r in rows:
        if r["key"] in SPEC:
            try:
                out[r["key"]] = _coerce(r["key"], json.loads(r["value"]))
            except (ValueError, TypeError):
                pass  # a corrupt row falls back to the default, never crashes
    return out


def put(db: sqlite3.Connection, user_id: int, updates: dict[str, Any]) -> dict[str, Any]:
    unknown = [k for k in updates if k not in SPEC]
    if unknown:
        raise ValueError(f"unknown setting(s): {', '.join(unknown)}")
    for k, v in updates.items():
        db.execute(
            "INSERT INTO user_settings (user_id, key, value) VALUES (?,?,?)"
            " ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value",
            (user_id, k, json.dumps(_coerce(k, v))),
        )
    return get_all(db, user_id)


def schema() -> list[dict[str, Any]]:
    """What the settings page renders itself from."""
    return [{"key": k, **v} for k, v in SPEC.items()]
