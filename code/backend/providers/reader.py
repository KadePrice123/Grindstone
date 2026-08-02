"""Readable article extraction (trafilatura), for news whose feed carries no body.

Measured 2026-08-02 against the live Alpaca/Benzinga feed: 44/50 articles
ship real content, and the remainder are stubs (trading halts, analyst-rating
one-liners) whose publisher page still has text — trafilatura pulled
376-2439 chars from them. So: use the feed's content when it exists, extract
when it does not, and never show the user an empty page.

Same hang-proofing as every other outbound provider (a network call on the
request path must be bounded and must give up on a sick dependency), and the
same lesson applies: NEVER import this module's heavy dependency eagerly at
startup — a big import holds the GIL and freezes the whole server.
"""
from __future__ import annotations

import concurrent.futures
import re
import threading
import time
from typing import Any

_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="reader")
CALL_TIMEOUT = 12.0
BREAKER_THRESHOLD = 3
BREAKER_COOLDOWN = 120.0

_lock = threading.Lock()
_failures = 0
_open_until = 0.0

_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANKS = re.compile(r"\n{3,}")


def available() -> bool:
    with _lock:
        return time.monotonic() >= _open_until


def _record(ok: bool) -> None:
    global _failures, _open_until
    with _lock:
        if ok:
            _failures = 0
            _open_until = 0.0
        else:
            _failures += 1
            if _failures >= BREAKER_THRESHOLD:
                _open_until = time.monotonic() + BREAKER_COOLDOWN


def html_to_text(html: str) -> str:
    """Feed content arrives as HTML; render it to readable plain text without
    pulling in a parser (and without trusting it as markup we would inject)."""
    if not html:
        return ""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|h[1-6]|li|tr)>", "\n\n", text)
    text = _TAGS.sub("", text)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                         ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"),
                         ("&rsquo;", "’"), ("&lsquo;", "‘"), ("&mdash;", "—"),
                         ("&ndash;", "–"), ("&hellip;", "…")):
        text = text.replace(entity, char)
    text = _WS.sub(" ", text)
    return _BLANKS.sub("\n\n", text).strip()


def extract(url: str) -> dict[str, Any] | None:
    """Fetch and extract a readable article. None when unavailable — callers
    fall back to opening the page in a browser tab."""
    if not url.startswith("http") or not available():
        return None

    def work() -> dict[str, Any] | None:
        import trafilatura  # deferred: heavy, and eager imports froze the server

        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return None
        text = trafilatura.extract(downloaded, include_comments=False,
                                   include_tables=True, favor_precision=True)
        if not text or len(text) < 200:
            return None
        meta = trafilatura.extract_metadata(downloaded)
        return {
            "text": text,
            "title": getattr(meta, "title", None) or "",
            "author": getattr(meta, "author", None) or "",
            "site": getattr(meta, "sitename", None) or "",
            "date": getattr(meta, "date", None) or "",
        }

    try:
        out = _pool.submit(work).result(timeout=CALL_TIMEOUT)
    except Exception:  # noqa: BLE001 — network, parse changes, timeouts
        _record(False)
        return None
    _record(True)
    return out


def status() -> dict[str, Any]:
    try:
        import trafilatura  # noqa: F401

        installed = True
    except ImportError:
        installed = False
    return {"installed": installed, "available": installed and available()}
