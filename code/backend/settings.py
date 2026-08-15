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
        "group": "Search",
        "kind": "bool", "default": True,
        "label": "Search the web",
        "help": "Blend DuckDuckGo results into search. Off = platform data only.",
    },
    "web_news_enabled": {
        "group": "Search",
        "kind": "bool", "default": True,
        "label": "Web news in results",
        "help": "Include web news articles alongside your broker's news feed.",
    },
    "web_search_engine": {
        "group": "Search",
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
        "group": "Search",
        "kind": "float", "default": 1.0, "min": 0.0, "max": 4.0, "step": 0.25,
        "label": "In-house result boost",
        "help": "How strongly tickers, your news store and app pages outrank web "
                "results. 0 = no preference, 4 = platform results dominate.",
    },
    "open_web_in_app": {
        "group": "Appearance",
        "kind": "bool", "default": True,
        "label": "Open links in the app",
        "help": "Web pages open as in-app browser tabs instead of your OS browser.",
    },
    "theme": {
        "group": "Appearance",
        "kind": "choice", "default": "dark", "choices": ["dark", "light"],
        "label": "Theme",
        "help": "Applies to the whole app.",
    },
    "chart_candles": {
        "group": "Charts",
        "kind": "choice", "default": "all",
        "choices": ["all", "5000", "2000", "1000", "500", "200"],
        "label": "Candles per chart",
        "help": "How much history charts load. 'all' pulls everything the "
                "source has for the ticker; a number caps it (faster on "
                "intraday timeframes).",
    },
    "autorecord_favorites": {
        "group": "Data",
        "kind": "bool", "default": False,
        "label": "Record my favourite symbols automatically",
        "help": "Starring a symbol starts recording its daily bars and option "
                "chains; un-starring STOPS the recording but never deletes "
                "what was already recorded — chain history cannot be "
                "re-fetched once the provider's window moves past it. Futures "
                "roots record their option chain when a TastyTrade account is "
                "enrolled (bars have no source); indices cannot be recorded "
                "yet — those stay starred and say why. "
                "Off by default because it spends API budget without asking.",
    },
    "deep_history": {
        "group": "Data",
        "kind": "bool", "default": False,
        "label": "Keep full history for every symbol I open",
        "help": "Opening a symbol's chart starts keeping its daily history "
                "permanently, as far back as the sources reach — your broker "
                "for the years it covers, the keyless daily feed for the "
                "decades before that, each stretch labelled with where it "
                "came from. Come back a week later and only the missing days "
                "are fetched, not the whole history again. Your starred "
                "symbols are filled the moment you turn this on. The fill "
                "runs in the background: charts never wait for it, and this "
                "store is never used to price a backtest.",
    },
    "backfill_enabled": {
        "group": "Data",
        "kind": "bool", "default": False,
        "label": "Fill in missing history in the background",
        "help": "Works through the days coverage says are missing for the "
                "symbols you record, oldest first, paced so it never starves "
                "the charts you are looking at. Resumable: it recomputes what "
                "is left every run, so stopping it costs nothing. A day the "
                "provider positively reports as empty is recorded as such and "
                "never asked for again.",
    },
    "onclick_chain_backfill": {
        "group": "Data",
        "kind": "bool", "default": False,
        "label": "Also fill in option history from OnclickMedia",
        "help": "Alpaca sells no historical option snapshots at all, so past "
                "chains can only come from OnclickMedia — a free, "
                "unauthenticated source with a rolling ~180-day window. "
                "Anything older than that is simply not obtainable and is "
                "reported rather than retried. Paced at one request every few "
                "seconds because the source is free; a symbol it does not "
                "carry (it has never carried SPX, SPXW or XSP) is left "
                "retryable rather than being written off. Separate from the "
                "main backfill switch because it uses a third party rather "
                "than your own broker key.",
    },
    "backfill_years": {
        "group": "Data",
        "kind": "choice", "default": "2",
        "choices": ["1", "2", "5", "10", "max"],
        "label": "How far back to fill",
        "help": "How much history the backfill tries to reach. Alpaca's free "
                "tier serves 2016 onward for equities; OnclickMedia's window "
                "is a rolling 180 days, so anything older simply is not "
                "obtainable from it and is reported rather than retried.",
    },
    "options_cache_minutes": {
        "group": "Data",
        "kind": "float", "default": 15.0, "min": 0.0, "max": 1440.0, "step": 5.0,
        "label": "Keep option chains for (minutes)",
        "help": "How long a fetched chain window stays usable before it is "
                "pulled again. Dragging a leg inside a window already fetched "
                "costs nothing while it is still fresh, which is what stops "
                "every nudge becoming a request. 0 = always fetch live.",
    },
    "equity_cache_days": {
        "group": "Data",
        "kind": "choice", "default": "30",
        "choices": ["off", "7", "30", "90", "365", "forever"],
        "label": "Keep chart data for",
        "help": "Bars fetched when you open a chart are kept this long, so the "
                "chart still draws when the provider is unreachable. A CHOICE "
                "rather than a number on purpose: a free-typed retention with a "
                "minimum of 0 reads as 'keep nothing', which is a data-deleting "
                "setting one keystroke away from a sensible one. This never "
                "touches your recorded data, which the recorder's own retention "
                "governs.",
    },
    # PROPORTIONAL MATCHING for the Opt page's history series. A flat ±3 days
    # is a reasonable window on a 21-DTE trade and an absurdly tight one on a
    # 300-DTE trade: long-dated expirations are quarterly, so a fixed window
    # finds a match on a handful of days a year and the line comes out as a
    # few points pretending to be a series. These scale the window with the
    # tenor. Both are FLOORED by the flat tolerances, so short-dated matching
    # is bit-for-bit what it was — only the long end loosens.
    "hist_dte_pct": {
        "group": "Charts",
        "kind": "float", "default": 3.0, "min": 0.0, "max": 25.0, "step": 0.5,
        "label": "History match: DTE grace (% of tenor)",
        "help": "How far the history chart may stray from the contract's "
                "days-to-expiry when it looks for a comparable day. 3% of a "
                "300-DTE trade is ±9 days — fine, because a 291-day and a "
                "300-day option are the same trade. Never tighter than ±3 "
                "days, so short-dated matching is unaffected. 0 = flat ±3 "
                "days everywhere (the old behaviour).",
    },
    "hist_strike_pct": {
        "group": "Charts",
        "kind": "float", "default": 0.0, "min": 0.0, "max": 10.0, "step": 0.25,
        "label": "History match: strike grace (% of strike)",
        "help": "Same idea for the strike, and OFF by default because it cuts "
                "the other way: widening it pulls neighbouring strikes into a "
                "series whose whole promise is 'the actual strike'. Useful "
                "when the exact strike was not listed historically, or on a "
                "wide strike grid. Never tighter than ±$1.",
    },
    "backtest_options_db": {
        "group": "Backtesting",
        "kind": "path", "default": "",
        "label": "Backtest options database",
        "help": "Full path to spy_options.db (multi-GB, never ships with the "
                "app). Empty = look beside the project folder, the workspace "
                "layout this machine already uses.",
    },
    "backtest_bars_db": {
        "group": "Backtesting",
        "kind": "path", "default": "",
        "label": "Backtest bars database",
        "help": "Full path to spy_bars.db. Empty = look beside the project "
                "folder.",
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
    if spec["kind"] == "path":
        # A filesystem path typed by the user; bounded, never validated here —
        # existence is the backtest status endpoint's job, honestly reported.
        return str(value)[:1024] if isinstance(value, str) else spec["default"]
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


#: Card order on the Settings page. A setting whose group is missing here
#: lands in "Other" rather than vanishing — the page must never silently drop
#: a control just because nobody classified it.
GROUP_ORDER = ["Search", "Charts", "Data", "Backtesting", "Appearance", "Other"]


def schema() -> list[dict[str, Any]]:
    """What the settings page renders itself from."""
    return [{"key": k, "group": v.get("group", "Other"), **v} for k, v in SPEC.items()]
