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
from .providers import websearch
from .universe import Universe

RRF_K = 60

# The page registry — the same declarative seam extensions will use (6.11).
PAGES = [
    {"key": "accounts", "title": "Accounts", "words": ["accounts", "brokers", "keys"]},
    {"key": "apis", "title": "APIs", "words": ["apis", "api", "keys", "credentials"]},
    {"key": "ai", "title": "AI", "words": ["ai", "assistant", "chat"]},
    {"key": "positions", "title": "Positions", "words": ["positions", "portfolio", "pnl", "p&l"]},
    {"key": "data", "title": "Data management", "words": ["data", "recording", "storage", "history"]},
    {"key": "settings", "title": "Settings",
     "words": ["settings", "preferences", "theme", "search", "web"]},
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


def _key(r: dict[str, Any]) -> str:
    return f'{r["type"]}:{r.get("symbol") or r.get("id") or r.get("page")}'


def _rrf(lists: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    scores: dict[str, float] = {}
    rows: dict[str, dict[str, Any]] = {}
    for lst in lists:
        for rank, r in enumerate(lst):
            k = _key(r)
            scores[k] = scores.get(k, 0.0) + 1.0 / (RRF_K + rank + 1)
            rows.setdefault(k, r)
    return sorted(rows.values(), key=lambda r: scores[_key(r)], reverse=True)


def _rrf_weighted(groups: list[tuple[list[dict[str, Any]], float]]) -> list[dict[str, Any]]:
    """Fuse already-ranked groups, each with a weight. This is how the
    in-house boost works: platform results and web results are ranked
    separately, then the platform group's contribution is multiplied by
    (1 + boost) so the user's preference is one number, not a heuristic."""
    scores: dict[str, float] = {}
    rows: dict[str, dict[str, Any]] = {}
    for lst, weight in groups:
        for rank, r in enumerate(lst):
            k = _key(r)
            scores[k] = scores.get(k, 0.0) + weight / (RRF_K + rank + 1)
            rows.setdefault(k, r)
    return sorted(rows.values(), key=lambda r: scores[_key(r)], reverse=True)


def _web_row(r: dict[str, Any]) -> dict[str, Any]:
    return {**r, "id": r["url"]}


def page(q: str, uni: Universe, con: sqlite3.Connection, page_no: int = 1,
         per_page: int = 10, prefs: dict[str, Any] | None = None) -> dict[str, Any]:
    """Full results page (the Google-style landing when you press Enter
    without picking a suggestion). Same retrieval as the dropdown, but deep:
    symbols fused with a wider news sweep, then paginated."""
    q = (q or "").strip()
    per_page = max(1, min(per_page, 50))
    page_no = max(1, page_no)
    if not q:
        return {"query": q, "page": 1, "pages": 0, "total": 0, "results": [],
                "featured": None}

    tokens = q.split()
    news_toks = [t for t in tokens if t.lower() in NEWS_WORDS]
    other = [t for t in tokens if t.lower() not in NEWS_WORDS]
    scope = uni.exact(other[0]) if (news_toks and other) else None

    # A ticker mentioned anywhere in the query gets a chart above the results.
    featured = uni.exact(q) or scope
    if featured is None:
        for t in tokens:
            hit = uni.exact(t)
            if hit:
                featured = hit
                break

    symbols: list[dict[str, Any]] = []
    if not news_toks:
        seen: set[str] = set()
        for e in uni.prefix(q, limit=20) + [e for (e, _s) in uni.fuzzy(q, limit=25)]:
            if e["symbol"] in seen:
                continue
            seen.add(e["symbol"])
            symbols.append(_sym_row(e))

    if scope is not None:
        news = [_news_row(it) for it in
                newsstore.latest(con, symbols=[scope["symbol"]], limit=60)]
    else:
        news = [_news_row(it) for it in newsstore.search(con, q, limit=60)]
        if featured is not None:
            extra = [_news_row(it) for it in
                     newsstore.latest(con, symbols=[featured["symbol"]], limit=30)]
            have = {r["id"] for r in news}
            news += [r for r in extra if r["id"] not in have]

    prefs = prefs or {}
    boost = float(prefs.get("inhouse_boost", 1.0))

    # The open web, fetched only on page 1 (deeper pages page through what we
    # already ranked — a fresh scrape per page would be slow and unstable).
    engine = prefs.get("web_search_engine", websearch.DEFAULT_BACKEND)
    web: list[dict[str, Any]] = []
    if page_no == 1:
        if prefs.get("web_search_enabled", True):
            web += [_web_row(r) for r in websearch.web_results(q, limit=12, backend=engine)]
        if prefs.get("web_news_enabled", True) and (news_toks or scope or featured):
            subject = scope["symbol"] if scope else (featured["symbol"] if featured else q)
            term = f"{subject} stock news" if (scope or featured) else q
            web += [_web_row(r) for r in websearch.web_news(term, limit=10, backend=engine)]

    # In-house lists are fused normally, then boosted as a group: the user's
    # own data and the platform's pages outrank the open web by however much
    # they chose in settings (0 = no preference).
    inhouse = _rrf([symbols, news, _pages_match(q)])
    fused = _rrf_weighted([(inhouse, 1.0 + boost), (web, 1.0)])

    total = len(fused)
    start = (page_no - 1) * per_page
    return {
        "query": q,
        "page": page_no,
        "pages": max(1, (total + per_page - 1) // per_page),
        "total": total,
        "results": fused[start:start + per_page],
        "featured": {"symbol": featured["symbol"], "name": featured["name"],
                     "asset_class": featured["asset_class"]} if featured else None,
        "web": {"used": bool(web), **websearch.status()},
    }


def query(q: str, uni: Universe, con: sqlite3.Connection,
          limit: int = 12, live_news=None, web=None) -> dict[str, Any]:
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

    # Web hits belong in the dropdown too — a browser bar that only knows its
    # own data is not a browser bar. Fetched by the caller so it can respect
    # settings and stay off the critical path when disabled.
    web_rows = [_web_row(r) for r in (web(q) if web else [])]
    fused = _rrf_weighted([(_rrf([prefix, fuzzy, news, pages]), 2.0), (web_rows, 1.0)])

    # exact ticker always pins first
    if exact:
        row = _sym_row(exact)
        fused = [row] + [r for r in fused
                         if not (r["type"] == "symbol" and r.get("symbol") == exact["symbol"])]

    return {"results": fused[:limit], "intent": intent}
