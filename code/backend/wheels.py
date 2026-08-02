"""Gesture wheels (FR-SHELL-4): the user's right-click radial menus.

SolidWorks-style mouse-gesture wheels. The SHELL owns the interaction (spawn,
hold-drag, lock); this module owns the DOCUMENT — which wheels exist, what
sits in each segment, and the display config — stored per user in app.db and
edited from the Settings page.

Shape:
    {
      "config": {
        "ticker_display": "percent" | "price",   # what ticker segments show
        "ticker_colors": bool,                   # tint by day direction
        "locked": wheel-id | None                # user-locked default wheel
      },
      "wheels": [
        {"id", "name", "symbol", "builtin"?, "dynamic"?, "segments": [...]}
      ]
    }

Segment vocabulary (the same declarative seam extensions will use, 6.11):
    {"type": "wheel",       "wheel": id,     "label"?}   go to another wheel
    {"type": "nav",         "route": name,   "label"?}   open a platform page
    {"type": "tool",        "tool": name,    "label"?}   run a shell tool
    {"type": "ticker",      "ticker": "SPY"}             open/focus the ticker
    {"type": "placeholder", "label": str}                announced, not built

Position is the index: segment 0 sits at 12 o'clock, the rest clockwise.
The "tabs" wheel is dynamic — its segments are built by the shell from the
open tabs at spawn time and are not stored or editable here.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

DOC_KEY = "gesture_wheels"

# Bumped when the DEFAULTS change shape or content in a way stored docs
# should adopt (v2: the chart wheel family, charts page, SPY->Charts on
# main). A stored doc with an older version is regenerated from defaults —
# honest data loss, taken deliberately over silently mixing old and new.
DOC_VERSION = 2

MAX_WHEELS = 16
MAX_SEGMENTS = 12
MIN_SEGMENTS = 2
NEW_WHEEL_SEGMENTS = 6  # a freshly created wheel starts with 6 empty slots

ROUTES = ("idle", "accounts", "data", "settings", "news", "charts")
TOOLS = ("search",)
# Chart actions a wheel segment can fire at the chart it was spawned over
# (delivered to that view as a chart:action event; pages without a chart
# handler simply ignore them).
CHART_TOOLS = ("pointer", "trend", "hline", "clear",
               "ind:vol", "ind:sma20", "ind:sma50", "ind:ema20", "ind:rsi14",
               "normalize")
SEG_TYPES = ("wheel", "nav", "tool", "ticker", "chart", "placeholder", "empty")
# Dynamic wheels build their segments from live state at spawn time:
#   tabs          the open tabs (paginated past 8)
#   chart-add     open ticker tabs not yet on the spawned-over chart
#   chart-ind     indicator toggles, marked with their current on/off state
#   chart-tickers the chart's symbols, to hide/show
DYNAMIC_KINDS = ("tabs", "chart-add", "chart-ind", "chart-tickers")
BUILTIN_IDS = ("main", "ai", "tabs", "tickers",
               "chart", "chart-add", "chart-ind", "chart-tickers")


def default_doc() -> dict[str, Any]:
    return {
        "version": DOC_VERSION,
        "config": {"ticker_display": "percent", "ticker_colors": True, "locked": None},
        "wheels": [
            {
                # Kade's specified layout: wheel-navs at N/E/W, search at S,
                # home/news/Charts/settings between them (v2: the SPY ticker
                # slot became the multi-chart page, per spec).
                "id": "main", "name": "Main", "symbol": "◆", "builtin": True,
                "segments": [
                    {"type": "wheel", "wheel": "ai", "label": "AI"},            # N
                    {"type": "nav", "route": "idle", "label": "Home"},          # NE
                    {"type": "wheel", "wheel": "tabs", "label": "Tabs"},        # E
                    {"type": "nav", "route": "news", "label": "News"},          # SE
                    {"type": "tool", "tool": "search", "label": "Search"},      # S
                    {"type": "nav", "route": "charts", "label": "Charts"},      # SW
                    {"type": "wheel", "wheel": "tickers", "label": "Tickers"},  # W
                    {"type": "nav", "route": "settings", "label": "Settings"},  # NW
                ],
            },
            {
                # The chart wheel: spawned by right-clicking ANY chart
                # (right-clicking off a chart spawns the default wheel).
                # Editable like any other; its dynamic companions are not.
                "id": "chart", "name": "Chart", "symbol": "📈", "builtin": True,
                "segments": [
                    {"type": "wheel", "wheel": "chart-add", "label": "Add symbol"},   # N
                    {"type": "chart", "tool": "trend", "label": "Trend line"},        # NE
                    {"type": "wheel", "wheel": "chart-ind", "label": "Indicators"},   # E
                    {"type": "chart", "tool": "hline", "label": "H-line"},            # SE
                    {"type": "wheel", "wheel": "main", "label": "Main"},              # S
                    {"type": "chart", "tool": "clear", "label": "Clear drawings"},    # SW
                    {"type": "wheel", "wheel": "chart-tickers", "label": "Hide"},     # W
                    {"type": "chart", "tool": "pointer", "label": "Pointer"},         # NW
                ],
            },
            {"id": "chart-add", "name": "Add symbol", "symbol": "+", "builtin": True,
             "dynamic": "chart-add", "segments": []},
            {"id": "chart-ind", "name": "Indicators", "symbol": "∿", "builtin": True,
             "dynamic": "chart-ind", "segments": []},
            {"id": "chart-tickers", "name": "Show/Hide", "symbol": "◑", "builtin": True,
             "dynamic": "chart-tickers", "segments": []},
            {
                # Placeholder until the AI milestone: visible, honestly dead.
                "id": "ai", "name": "AI", "symbol": "AI", "builtin": True,
                "segments": [
                    {"type": "placeholder", "label": "Chat"},
                    {"type": "placeholder", "label": "Summarize page"},
                    {"type": "placeholder", "label": "News digest"},
                    {"type": "wheel", "wheel": "main", "label": "Main"},
                    {"type": "placeholder", "label": "Explain chart"},
                    {"type": "placeholder", "label": "Indicators"},
                ],
            },
            {
                "id": "tabs", "name": "Tabs", "symbol": "⧉", "builtin": True,
                "dynamic": "tabs", "segments": [],
            },
            {
                "id": "tickers", "name": "Tickers", "symbol": "$", "builtin": True,
                "segments": [
                    {"type": "ticker", "ticker": "SPY"},
                    {"type": "ticker", "ticker": "QQQ"},
                    {"type": "ticker", "ticker": "GLD"},
                    {"type": "wheel", "wheel": "main", "label": "Main"},
                    {"type": "ticker", "ticker": "USO"},
                    {"type": "ticker", "ticker": "VIX"},
                ],
            },
        ],
    }


def _fail(msg: str) -> None:
    raise ValueError(msg)


def validate(doc: Any) -> dict[str, Any]:
    """Normalize and validate a whole wheels document. Raises ValueError with
    the actual reason — a silently 'fixed' config is how a user's edit
    disappears without explanation."""
    if not isinstance(doc, dict):
        _fail("wheels document must be an object")
    config = doc.get("config") or {}
    wheels = doc.get("wheels")
    if not isinstance(config, dict) or not isinstance(wheels, list):
        _fail("wheels document needs 'config' and 'wheels'")
    if not wheels or len(wheels) > MAX_WHEELS:
        _fail(f"between 1 and {MAX_WHEELS} wheels required")

    ids: list[str] = []
    for wh in wheels:
        if not isinstance(wh, dict):
            _fail("each wheel must be an object")
        wid = wh.get("id")
        if (not isinstance(wid, str) or not (1 <= len(wid) <= 32)
                or not wid.replace("-", "_").isidentifier()):
            _fail(f"bad wheel id: {wid!r}")
        if wid in ids:
            _fail(f"duplicate wheel id {wid!r}")
        ids.append(wid)
        name = wh.get("name")
        if not isinstance(name, str) or not (1 <= len(name) <= 24):
            _fail(f"wheel {wid}: name must be 1-24 characters")
        symbol = wh.get("symbol")
        if not isinstance(symbol, str) or not (1 <= len(symbol) <= 4):
            _fail(f"wheel {wid}: symbol must be 1-4 characters")

    for builtin in BUILTIN_IDS:
        if builtin not in ids:
            _fail(f"the built-in wheel {builtin!r} cannot be deleted")

    out_wheels: list[dict[str, Any]] = []
    for wh in wheels:
        wid = wh["id"]
        dynamic = wh.get("dynamic") if wh.get("dynamic") in DYNAMIC_KINDS else (
            wid if wid in DYNAMIC_KINDS else None)
        segments = wh.get("segments") or []
        if dynamic:
            segments = []  # built by the shell at spawn time, never stored
        elif not isinstance(segments, list) or not (
            MIN_SEGMENTS <= len(segments) <= MAX_SEGMENTS
        ):
            _fail(f"wheel {wid}: {MIN_SEGMENTS}-{MAX_SEGMENTS} segments required")

        out_segments: list[dict[str, Any]] = []
        for i, seg in enumerate(segments):
            if not isinstance(seg, dict):
                _fail(f"wheel {wid} segment #{i + 1}: must be an object")
            stype = seg.get("type")
            if stype not in SEG_TYPES:
                _fail(f"wheel {wid} segment #{i + 1}: unknown type {stype!r}")
            label = seg.get("label", "")
            if not isinstance(label, str) or len(label) > 20:
                _fail(f"wheel {wid} segment #{i + 1}: label must be ≤20 characters")
            clean: dict[str, Any] = {"type": stype, "label": label}
            if stype == "wheel":
                target = seg.get("wheel")
                if target not in ids:
                    _fail(f"wheel {wid} segment #{i + 1}: unknown target wheel {target!r}")
                clean["wheel"] = target
            elif stype == "nav":
                route = seg.get("route")
                if route not in ROUTES:
                    _fail(f"wheel {wid} segment #{i + 1}: unknown route {route!r}")
                clean["route"] = route
            elif stype == "tool":
                tool = seg.get("tool")
                if tool not in TOOLS:
                    _fail(f"wheel {wid} segment #{i + 1}: unknown tool {tool!r}")
                clean["tool"] = tool
            elif stype == "chart":
                tool = seg.get("tool")
                if tool not in CHART_TOOLS:
                    _fail(f"wheel {wid} segment #{i + 1}: unknown chart tool {tool!r}")
                clean["tool"] = tool
            elif stype == "ticker":
                ticker = seg.get("ticker")
                if (not isinstance(ticker, str) or not (1 <= len(ticker) <= 8)
                        or not ticker.replace(".", "").isalnum()):
                    _fail(f"wheel {wid} segment #{i + 1}: bad ticker {ticker!r}")
                clean["ticker"] = ticker.upper()
            out_segments.append(clean)

        out_wheels.append({
            "id": wid,
            "name": wh["name"],
            "symbol": wh["symbol"],
            "builtin": wid in BUILTIN_IDS,
            **({"dynamic": dynamic} if dynamic else {}),
            "segments": out_segments,
        })

    display = config.get("ticker_display", "percent")
    if display not in ("percent", "price"):
        _fail(f"ticker_display must be percent|price, got {display!r}")
    colors = bool(config.get("ticker_colors", True))
    locked = config.get("locked")
    if locked is not None and locked not in ids:
        _fail(f"locked wheel {locked!r} does not exist")

    return {
        "version": DOC_VERSION,
        "config": {"ticker_display": display, "ticker_colors": colors, "locked": locked},
        "wheels": out_wheels,
    }


def get(db: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    row = db.execute(
        "SELECT value FROM user_settings WHERE user_id=? AND key=?",
        (user_id, DOC_KEY),
    ).fetchone()
    if row is None:
        return default_doc()
    try:
        stored = json.loads(row["value"])
        # An older-version doc predates wheels the defaults now require;
        # regenerate rather than mixing generations half-validly.
        if stored.get("version") != DOC_VERSION:
            return default_doc()
        return validate(stored)
    except (ValueError, TypeError):
        return default_doc()  # a corrupt doc falls back, never crashes


def put(db: sqlite3.Connection, user_id: int, doc: Any) -> dict[str, Any]:
    clean = validate(doc)  # raises ValueError with the reason
    db.execute(
        "INSERT INTO user_settings (user_id, key, value) VALUES (?,?,?)"
        " ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value",
        (user_id, DOC_KEY, json.dumps(clean)),
    )
    return clean
