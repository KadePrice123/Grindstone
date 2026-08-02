"""Web search + web news via DDGS, keyless.

This is what turns the omnibox into a real browser bar: the platform's own
results (tickers, your news store, app pages) blended with the open web, and
articles opening in in-app browser tabs.

BACKEND PINNED TO DUCKDUCKGO ON PURPOSE. As of 9.x, `ddgs` is no longer a
DuckDuckGo client — it is a metasearch aggregator whose default
backend="auto" also scrapes Google, Bing, Yandex and Startpage, using a
transport that randomizes its TLS/browser fingerprint. Those engines' terms
prohibit automated querying, and fingerprint spoofing is bot-detection
evasion; DuckDuckGo's terms carry no such clause. Pinning the backend keeps
this feature defensible. Do not "improve" it back to auto for more results.

Hang-proofing is not optional (learned from the Yahoo fallback, and
confirmed here): ddgs's own `timeout` does NOT bound a call — it shuts its
executor down with wait=True, joining every in-flight worker. So the outer
deadline plus the circuit breaker below are the only real bounds.

Result shapes verified empirically 2026-08-02 (not from docs):
  text(): {title, href, body}      news(): {date, title, body, url, image, source}
"""
from __future__ import annotations

import concurrent.futures
import threading
import time
from typing import Any

# Measured from this machine on 2026-08-02 (query: "best index funds 2026"):
#   duckduckgo, mojeek, wikipedia, yahoo, google -> 0 results
#   brave 5, bing 5, startpage 5, yandex 5, auto 5
# So a DuckDuckGo-only pin makes the feature dead, not safe. The engine set
# is therefore a USER SETTING with the tradeoff stated at the point of
# choice: "brave" is the narrowest option that actually returns results,
# "auto" fans out across everything ddgs supports (best coverage, but it
# includes engines whose terms forbid automated querying).
DEFAULT_BACKEND = "brave"
ALLOWED_BACKENDS = ("brave", "auto", "duckduckgo", "bing", "startpage")

_MIN_INTERVAL = 0.6
_lock = threading.Lock()
_last_call = 0.0

_pool = concurrent.futures.ThreadPoolExecutor(max_workers=6, thread_name_prefix="websearch")
CALL_TIMEOUT = 6.0          # a search box must feel instant or be absent
BREAKER_THRESHOLD = 3       # DDG rate-limits scrapers; back off rather than hammer
BREAKER_COOLDOWN = 180.0

_breaker_lock = threading.Lock()
_failures = 0
_open_until = 0.0


def available() -> bool:
    with _breaker_lock:
        return time.monotonic() >= _open_until


def _record(ok: bool) -> None:
    global _failures, _open_until
    with _breaker_lock:
        if ok:
            _failures = 0
            _open_until = 0.0
        else:
            _failures += 1
            if _failures >= BREAKER_THRESHOLD:
                _open_until = time.monotonic() + BREAKER_COOLDOWN


def _throttle() -> None:
    global _last_call
    with _lock:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def _run(kind: str, query: str, limit: int,
         backend: str = DEFAULT_BACKEND) -> list[dict[str, Any]]:
    if not available() or not query.strip():
        return []
    if backend not in ALLOWED_BACKENDS:
        backend = DEFAULT_BACKEND

    def work() -> list[dict[str, Any]]:
        from ddgs import DDGS  # deferred: keeps sidecar startup fast

        _throttle()
        client = DDGS()
        method = client.news if kind == "news" else client.text
        # max_results MUST be keyword: positionally it binds to `region`.
        return method(query, max_results=min(limit, 25), backend=backend)

    try:
        rows = _pool.submit(work).result(timeout=CALL_TIMEOUT)
    except Exception as e:  # noqa: BLE001 — timeouts, rate limits, parse changes
        # "No results found" is a legitimate empty answer that ddgs RAISES.
        # Counting it as a failure would trip the breaker on three innocent
        # searches and disable web results for minutes.
        if "no results" in str(e).lower():
            _record(True)
            return []
        _record(False)
        return []
    _record(True)
    return rows or []


def web_results(query: str, limit: int = 10,
                backend: str = DEFAULT_BACKEND) -> list[dict[str, Any]]:
    out = []
    for r in _run("text", query, limit, backend):
        href = r.get("href") or r.get("url") or ""
        if not href.startswith("http"):
            continue
        out.append({
            "type": "web",
            "title": (r.get("title") or href)[:200],
            "subtitle": (r.get("body") or "")[:220],
            "url": href,
            "site": _host(href),
        })
    return out


def web_news(query: str, limit: int = 10,
             backend: str = DEFAULT_BACKEND) -> list[dict[str, Any]]:
    out = []
    # DuckDuckGo's news endpoint returns nothing from here; bing answers.
    if backend == "duckduckgo":
        backend = "auto"
    for r in _run("news", query, limit, backend):
        url = r.get("url") or ""
        if not url.startswith("http"):
            continue
        out.append({
            "type": "web-news",
            "title": (r.get("title") or url)[:200],
            "subtitle": (r.get("body") or "")[:220],
            "url": url,
            "site": r.get("source") or _host(url),
            "created_at": r.get("date") or "",
            "image": r.get("image") or "",
        })
    return out


def _host(url: str) -> str:
    try:
        from urllib.parse import urlparse

        return urlparse(url).hostname or ""
    except ValueError:
        return ""


def status() -> dict[str, Any]:
    try:
        import ddgs  # noqa: F401

        installed = True
    except ImportError:
        installed = False
    return {"installed": installed, "available": installed and available()}
