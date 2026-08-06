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
# "ready" is what the UI keys off: an unbuilt page still answers a search (so
# you can see it is coming) but must not look clickable, because clicking it
# went nowhere at all.
PAGES = [
    {"key": "home", "title": "Home", "words": ["home", "start", "grindstone"], "ready": True},
    {"key": "accounts", "title": "Accounts", "words": ["accounts", "account", "brokers", "keys"],
     "ready": True},
    {"key": "apis", "title": "APIs", "words": ["apis", "api", "credentials"], "ready": False},
    {"key": "ai", "title": "AI", "words": ["ai", "assistant", "chat"], "ready": False},
    {"key": "positions", "title": "Positions", "words": ["positions", "portfolio", "pnl", "p&l"],
     "ready": False},
    {"key": "data", "title": "Data management",
     "words": ["data", "recording", "storage", "history"], "ready": True},
    {"key": "news", "title": "News", "words": ["news", "headlines", "feed"],
     "ready": True},
    {"key": "charts", "title": "Charts", "words": ["charts", "chart", "compare",
     "multichart"], "ready": True},
    # Words stay tight on purpose: ready:True pages pin hard in ranking, and
    # a loose word list would hijack ticker queries (BT is a real ticker).
    {"key": "backtest", "title": "Backtest", "words": ["backtest", "backtests",
     "backtesting", "strategy", "calibration"], "ready": True},
    {"key": "settings", "title": "Settings",
     "words": ["settings", "setting", "preferences", "theme", "web"], "ready": True},
    {"key": "help", "title": "Help", "words": ["help", "guide", "manual", "docs",
     "how", "tutorial"], "ready": True},
]

# Help SECTIONS are searchable destinations of their own (Kade's spec:
# searching "drawing" must land on the drawing section, not just the page).
# Kept in lockstep with HelpPage.tsx's section ids — the gate cross-checks.
HELP_TOPICS = [
    {"section": "getting-started", "title": "Getting started",
     "words": ["start", "profile", "password", "login", "vault", "first"]},
    {"section": "search", "title": "Search & addresses",
     "words": ["omnibox", "address", "gs", "url", "browser", "web"]},
    {"section": "tabs", "title": "Tabs & windows",
     "words": ["tab", "tabs", "window", "tear", "drag", "previous", "new"]},
    {"section": "split-view", "title": "Split view",
     "words": ["split", "divider", "side", "pane", "pair"]},
    {"section": "wheels", "title": "Gesture wheels",
     "words": ["wheel", "wheels", "gesture", "radial", "right-click", "rightclick",
               "lock", "hub"]},
    {"section": "favorites", "title": "Favorites & apps",
     "words": ["favorites", "favorite", "star", "starred", "bookmark",
               "bookmarks", "launcher", "apps", "grid"]},
    {"section": "charts", "title": "Charts",
     "words": ["chart", "candle", "candles", "timeframe", "indicator", "indicators",
               "sma", "ema", "rsi", "volume", "period"]},
    {"section": "drawing", "title": "Drawing tools",
     "words": ["draw", "drawing", "line", "trend", "hline", "vline", "circle",
               "select", "trim", "erase", "annotate"]},
    {"section": "measuring", "title": "Measuring",
     "words": ["measure", "measuring", "inspect", "distance", "ruler", "dimension"]},
    {"section": "multi-charts", "title": "Multi-symbol charts",
     "words": ["compare", "comparison", "isolate", "solo", "normalize", "multi",
               "overlay"]},
    {"section": "data", "title": "Data recording",
     "words": ["record", "recording", "jobs", "retention", "storage", "chain"]},
    {"section": "backtest-help", "title": "Backtesting",
     "words": ["backtest", "backtesting", "preset", "presets", "sweep", "verify",
               "calibrate", "calibration", "condor", "spread", "delta", "dte"]},
    {"section": "settings-help", "title": "Settings explained",
     "words": ["configure", "boost", "engine", "theme", "depth"]},
    {"section": "troubleshooting", "title": "Troubleshooting",
     "words": ["problem", "error", "broken", "backend", "fix", "stuck"]},
]


def _help_row(t: dict[str, Any]) -> dict[str, Any]:
    return {"type": "page", "page": "help", "section": t["section"],
            "title": f'Help · {t["title"]}', "subtitle": "How-to", "ready": True}


def _help_match(q: str) -> list[dict[str, Any]]:
    ql = q.lower().strip()
    if not ql:
        return []
    out = []
    for t in HELP_TOPICS:
        if (any(w.startswith(ql) or ql in w for w in t["words"])
                or ql in t["title"].lower()):
            out.append(_help_row(t))
    return out

NEWS_WORDS = {"news", "headlines", "articles", "article"}
# "SPY Opt" — the options workstation for a symbol, reached the same way its
# news is. A bare word with a space stays a SEARCH by the omnibox's own
# conservative rule, so the destination has to be offered here rather than by
# address parsing; this is the grammar that makes Kade's "SPY Opt" land.
OPT_WORDS = {"opt", "opts", "option", "options", "chain", "chains", "greeks"}


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
    return {"type": "page", "page": p["key"], "title": p["title"],
            "subtitle": "Page" if p.get("ready") else "Arrives in a later milestone",
            "ready": bool(p.get("ready"))}


def normalize(q: str) -> str:
    """Strip a platform address down to what it is asking for, so typing
    "settings.gs" searches for "settings" instead of for a literal string
    nothing will ever match."""
    q = (q or "").strip()
    head = q.split()[0] if q else ""
    if head.lower().endswith(".gs") and " " not in q:
        return q[: len(head) - 3] + q[len(head):]
    return q


def _pages_match(q: str) -> list[dict[str, Any]]:
    ql = q.lower().strip()
    if not ql:
        return []
    out = []
    for p in PAGES:
        if any(w.startswith(ql) or ql in w for w in p["words"]) or ql in p["title"].lower():
            out.append(_page_row(p))
    return out


def _page_exact(q: str) -> tuple[dict[str, Any], bool] | None:
    """The page a query names outright, and whether that claim is STRONG.

    Fusion alone could not surface these at all: every retrieval list
    contributes the same score at rank 0, so a page tied with a fuzzy ticker
    and lost on insertion order — typing "settings" ranked a random symbol
    above the Settings page.

    Strong means the query IS the page's name and the page exists; that beats
    even an exact ticker, because someone typing "data" on this platform
    means the page, and the DATA ticker sits directly underneath. A synonym,
    or a page we have announced but not built, is weak and ranks below the
    instrument — we should not outrank a real symbol with a promise.
    """
    ql = q.lower().strip()
    if not ql:
        return None
    for p in PAGES:
        named = ql == p["key"] or ql == p["title"].lower()
        if named or ql in p["words"]:
            return _page_row(p), bool(named and p.get("ready"))
    return None


def _pin(fused: list[dict[str, Any]], pins: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = {_key(r) for r in pins}
    return pins + [r for r in fused if _key(r) not in keys]


def _key(r: dict[str, Any]) -> str:
    # section distinguishes help topics — without it every Help · X row
    # deduped into one under the shared page key 'help'.
    base = r.get("symbol") or r.get("id") or r.get("page")
    return f'{r["type"]}:{base}:{r.get("section", "")}'


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
    q = normalize(q)
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
    if featured is None and len(tokens) == 1 and len(q) >= 4:
        # A company name usually implies a ticker: "google" should chart
        # GOOGL. SINGLE-TOKEN ONLY — measured on the real 14k universe,
        # multi-word queries score just as high on nonsense ("tesla earnings"
        # -> ZECP 86, "best index funds" -> BOBP 86) as single names do on
        # the right answer (GOOGL 82, TSLA/AAPL/AMZN/NVDA 90), so a score
        # threshold alone cannot tell them apart. A wrong chart above the
        # results is worse than no chart.
        best = uni.fuzzy(q, limit=1, cutoff=80.0)
        if best and best[0][0].get("tradable"):
            featured = best[0][0]

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
    # The page is SECTIONED, not one fused list: platform hits used to fill
    # every slot on page 1 and push the web to page 2, where nobody looks.
    # The boost now decides how many platform rows lead (2 at boost 0, up to
    # 8), and the web always gets its own section underneath.
    inhouse_quota = max(2, min(8, round(3 + boost * 2)))

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

    # Same pinning rule as the dropdown — a query that names a page must not
    # have to out-rank fourteen thousand tickers to appear.
    hit = _page_exact(q)
    exact_sym = uni.exact(q)
    pins: list[dict[str, Any]] = []
    if hit is not None and hit[1]:
        pins.append(hit[0])
    if exact_sym is not None:
        pins.append(_sym_row(exact_sym))
    if hit is not None and not hit[1]:
        pins.append(hit[0])
    inhouse = _pin(_rrf([symbols, news, _pages_match(q), _help_match(q)]), pins)

    # Two sections, kept strictly separate. An earlier version fused the
    # leftover platform rows into the second section, and since platform rows
    # carry the boost they refilled it — the web vanished from page 1 again.
    # Section 1: the best platform hits (page 1 only, size set by the boost).
    # Section 2: the web, and ONLY the web, paginated.
    lead = inhouse[:inhouse_quota] if page_no == 1 else []
    web_ranked = _rrf([web]) if web else []
    start = (page_no - 1) * per_page
    page_web = web_ranked[start:start + per_page]

    return {
        "query": q,
        "page": page_no,
        "pages": max(1, (len(web_ranked) + per_page - 1) // per_page),
        "total": len(inhouse) + len(web_ranked),
        "inhouse": lead,
        "results": page_web,
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
    q = normalize(q)
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

    # ---- intent grammar: "<TICKER> opt" / "opt <TICKER>" ------------------
    # Below the news branch deliberately: "SPY options news" is a news query
    # about options, and the more specific reading should win.
    if len(tokens) >= 2:
        opt_toks = [t for t in tokens if t.lower() in OPT_WORDS]
        other = [t for t in tokens if t.lower() not in OPT_WORDS]
        if opt_toks and other:
            hit = uni.exact(other[0])
            if hit:
                intent = {"kind": "symbol-opt", "symbol": hit["symbol"]}
                results = [
                    {"type": "action", "action": "symbol-opt", "symbol": hit["symbol"],
                     "title": f'{hit["symbol"]} Opt',
                     "subtitle": "Options analytics for the trade drawn on this chart"},
                    _sym_row(hit),
                ]
                return {"results": results[:limit], "intent": intent}

    # ---- lexical retrieval ------------------------------------------------
    exact = uni.exact(q)
    prefix = [_sym_row(e) for e in uni.prefix(q, limit=8)]
    fuzzy = [_sym_row(e) for (e, _score) in uni.fuzzy(q, limit=10)]
    news = [_news_row(it) for it in newsstore.search(con, q, limit=8)]
    pages = _pages_match(q)
    help_rows = _help_match(q)

    # Web hits belong in the dropdown too — a browser bar that only knows its
    # own data is not a browser bar. Fetched by the caller so it can respect
    # settings and stay off the critical path when disabled.
    web_rows = [_web_row(r) for r in (web(q) if web else [])]
    fused = _rrf_weighted([(_rrf([prefix, fuzzy, news, pages, help_rows]), 2.0),
                           (web_rows, 1.0)])

    # Named destinations pin to the top: a page that owns the name, then the
    # exact ticker, then a weaker page claim.
    hit = _page_exact(q)
    pins: list[dict[str, Any]] = []
    if hit is not None and hit[1]:
        pins.append(hit[0])
    if exact:
        pins.append(_sym_row(exact))
    if hit is not None and not hit[1]:
        pins.append(hit[0])

    return {"results": _pin(fused, pins)[:limit], "intent": intent}
