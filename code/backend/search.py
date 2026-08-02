"""The omnibox engine (REQUIREMENTS.md 6.6, Tier 1).

Deterministic intent grammar first, then lexical retrieval (exact > prefix >
fuzzy symbols; trigram news; substring pages), fused with Reciprocal Rank
Fusion (k=60). Exact ticker match always pins first. Tier 2 (semantic
embeddings) arrives with the AI milestone; nothing here needs to change for
it — it will just contribute another ranked list to the same fusion.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import newsstore
from .universe import Universe

RRF_K = 60

# The page registry — the same declarative seam extensions will use (6.11).
PAGES = [
    {"key": "accounts", "title": "Accounts", "words": ["accounts", "brokers", "keys"]},
    {"key": "apis", "title": "APIs", "words": ["apis", "api", "keys", "credentials"]},
    {"key": "ai", "title": "AI", "words": ["ai", "assistant", "chat"]},
    {"key": "positions", "title": "Positions", "words": ["positions", "portfolio", "pnl", "p&l"]},
    {"key": "data", "title": "Data management", "words": ["data", "recording", "storage", "history"]},
    {"key": "settings", "title": "Settings", "words": ["settings", "preferences", "theme"]},
]

NEWS_WORDS = {"news", "headlines", "articles", "article"}


def _sym_row(e: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "symbol", "symbol": e["symbol"], "title": e["symbol"],
        "subtitle": e["name"], "asset_class": e["asset_class"],
        "tradable": bool(e.get("tradable")),
    }


def _news_row(it: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "news", "id": it["id"], "title": it["headline"],
        "subtitle": (it["source"] + " · " + ", ".join(it["symbols"][:4])).strip(" ·"),
        "url": it["url"], "symbols": it["symbols"], "created_at": it["created_at"],
    }


def _page_row(p: dict[str, Any]) -> dict[str, Any]:
    return {"type": "page", "page": p["key"], "title": p["title"], "subtitle": "Page"}


def _pages_match(q: str) -> list[dict[str, Any]]:
    ql = q.lower().strip()
    if not ql:
        return []
    out = []
    for p in PAGES:
        if any(w.startswith(ql) or ql in w for w in p["words"]) or ql in p["title"].lower():
            out.append(_page_row(p))
    return out


def _rrf(lists: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    scores: dict[str, float] = {}
    rows: dict[str, dict[str, Any]] = {}

    def key(r: dict[str, Any]) -> str:
        return f'{r["type"]}:{r.get("symbol") or r.get("id") or r.get("page")}'

    for lst in lists:
        for rank, r in enumerate(lst):
            k = key(r)
            scores[k] = scores.get(k, 0.0) + 1.0 / (RRF_K + rank + 1)
            rows.setdefault(k, r)
    ordered = sorted(rows.values(), key=lambda r: scores[key(r)], reverse=True)
    return ordered


def query(q: str, uni: Universe, con: sqlite3.Connection,
          limit: int = 12, live_news=None) -> dict[str, Any]:
    """Returns {results: [...], intent: {...}|None}. Never raises on user input.

    live_news: optional callable(symbol) -> list[news items]; consulted when a
    symbol-news intent finds nothing locally (FR-SEARCH-4's live fallthrough —
    the rolling store is only as fresh as the last backfill)."""
    q = (q or "").strip()
    if not q:
        return {"results": [], "intent": None}

    tokens = q.split()
    intent: dict[str, Any] | None = None

    # ---- intent grammar: "<TICKER> news" / "news <TICKER>" ----------------
    if len(tokens) >= 2:
        news_toks = [t for t in tokens if t.lower() in NEWS_WORDS]
        other = [t for t in tokens if t.lower() not in NEWS_WORDS]
        if news_toks and other:
            hit = uni.exact(other[0])
            if hit:
                items = newsstore.latest(con, symbols=[hit["symbol"]], limit=8)
                if not items and live_news is not None:
                    items = live_news(hit["symbol"])
                intent = {"kind": "symbol-news", "symbol": hit["symbol"]}
                results = [
                    {"type": "action", "action": "symbol-news", "symbol": hit["symbol"],
                     "title": f'{hit["symbol"]} news',
                     "subtitle": f'Latest headlines for {hit["name"] or hit["symbol"]}'},
                    _sym_row(hit),
                    *[_news_row(it) for it in items],
                ]
                return {"results": results[:limit], "intent": intent}

    # ---- lexical retrieval ------------------------------------------------
    exact = uni.exact(q)
    prefix = [_sym_row(e) for e in uni.prefix(q, limit=8)]
    fuzzy = [_sym_row(e) for (e, _score) in uni.fuzzy(q, limit=10)]
    news = [_news_row(it) for it in newsstore.search(con, q, limit=8)]
    pages = _pages_match(q)

    fused = _rrf([prefix, fuzzy, news, pages])

    # exact ticker always pins first
    if exact:
        row = _sym_row(exact)
        fused = [row] + [r for r in fused
                         if not (r["type"] == "symbol" and r.get("symbol") == exact["symbol"])]

    return {"results": fused[:limit], "intent": intent}
