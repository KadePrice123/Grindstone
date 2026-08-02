"""News storage + search. Alpaca (Benzinga) articles land here; the omnibox
and the symbol page read from here; the future AI KB pipeline feeds from here.

FTS5 trigram over headline+summary gives substring matching ("earn" hits
"earnings"); queries under 3 chars can't match trigrams and are routed to
LIKE-on-recent instead. rowids are kept in lockstep between news and news_fts.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any


def upsert(con: sqlite3.Connection, items: list[dict[str, Any]]) -> int:
    """Insert or refresh articles by Alpaca id. Returns rows written."""
    n = 0
    with con:
        for it in items:
            cur = con.execute(
                "INSERT INTO news (id, headline, summary, source, url, symbols, created_at, updated_at)"
                " VALUES (:id, :headline, :summary, :source, :url, :symbols_json, :created_at, :updated_at)"
                " ON CONFLICT(id) DO UPDATE SET headline=excluded.headline,"
                "  summary=excluded.summary, url=excluded.url,"
                "  symbols=excluded.symbols, updated_at=excluded.updated_at",
                {**it, "symbols_json": json.dumps(it["symbols"])},
            )
            # Keep the FTS row in lockstep (rowid == news.id).
            con.execute("DELETE FROM news_fts WHERE rowid=?", (it["id"],))
            con.execute(
                "INSERT INTO news_fts (rowid, headline, summary) VALUES (?,?,?)",
                (it["id"], it["headline"], it["summary"]),
            )
            n += cur.rowcount
    return n


def _row_to_item(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"], "headline": r["headline"], "summary": r["summary"],
        "source": r["source"], "url": r["url"],
        "symbols": json.loads(r["symbols"] or "[]"),
        "created_at": r["created_at"],
    }


def latest(con: sqlite3.Connection, symbols: list[str] | None = None,
           limit: int = 20) -> list[dict[str, Any]]:
    if symbols:
        # symbols column is a JSON array; EXISTS over json_each keeps it exact
        # (a LIKE '%"SPY"%' would also match SPYG).
        marks = ",".join("?" for _ in symbols)
        rows = con.execute(
            "SELECT * FROM news WHERE EXISTS ("
            "  SELECT 1 FROM json_each(news.symbols) WHERE json_each.value IN (" + marks + ")"
            ") ORDER BY created_at DESC LIMIT ?",
            (*[s.upper() for s in symbols], limit),
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT * FROM news ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_item(r) for r in rows]


def _fts_quote(q: str) -> str:
    """FTS5 query syntax treats ",-,OR etc. as operators; a raw user string
    like 'AT&T earnings' would be a syntax error. Quote each token."""
    tokens = [t.replace('"', '""') for t in q.split() if t]
    return " ".join(f'"{t}"' for t in tokens)


def search(con: sqlite3.Connection, q: str, limit: int = 8,
           symbols: list[str] | None = None) -> list[dict[str, Any]]:
    q = q.strip()
    if not q:
        return []
    if len(q) < 3:
        # trigram can't see 1-2 char queries; fall back to LIKE over recents
        like = f"%{q}%"
        rows = con.execute(
            "SELECT * FROM news WHERE headline LIKE ? ORDER BY created_at DESC LIMIT ?",
            (like, limit),
        ).fetchall()
        items = [_row_to_item(r) for r in rows]
    else:
        rows = con.execute(
            "SELECT n.*, bm25(news_fts) AS rank FROM news_fts"
            " JOIN news n ON n.id = news_fts.rowid"
            " WHERE news_fts MATCH ?"
            " ORDER BY rank LIMIT ?",
            (_fts_quote(q), max(limit * 4, 24)),
        ).fetchall()
        items = [_row_to_item(r) for r in rows]
        # blend in recency: newest first among close ranks
        items.sort(key=lambda it: it["created_at"], reverse=True)
        items = items[:limit] if not symbols else items
    if symbols:
        wanted = {s.upper() for s in symbols}
        items = [it for it in items if wanted & {s.upper() for s in it["symbols"]}][:limit]
    return items[:limit]


def backfill(con: sqlite3.Connection, client, page_limit: int = 5) -> int:
    """Incremental backfill: newest-first from where the store left off; on a
    fresh store, walk page_limit pages of history via page_token. REGRESSION
    (review 2026-08-02, high): dropping the token refetched the identical
    first page five times and 'multi-day backfill' meant 50 articles."""
    newest = stats(con)["newest"]
    items, token = client.news(limit=50, start=newest)
    if not newest:
        for _ in range(page_limit):
            if not token:
                break
            more, token = client.news(limit=50, page_token=token)
            items.extend(more)
    return upsert(con, items)


def stats(con: sqlite3.Connection) -> dict[str, Any]:
    n = con.execute("SELECT COUNT(*) FROM news").fetchone()[0]
    newest = con.execute("SELECT MAX(created_at) FROM news").fetchone()[0]
    oldest = con.execute("SELECT MIN(created_at) FROM news").fetchone()[0]
    return {"count": n, "newest": newest, "oldest": oldest}
