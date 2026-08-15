"""User favorites — the one store behind the home grid, the wheel's
Favorites wheel, and the omnibox star.

A favorite is a saved destination:
    kind 'symbol'  key 'SPY'                       the ticker's page
    kind 'page'    key 'backtest.gs',
                       'help.gs?s=drawing'         a platform page (deep links
                                                   are first-class addresses)
    kind 'web'     key 'https://example.com/'      a real website

`icon` is what the tile/segment shows above the short name:
    'web'     a data: URI of the site's favicon, captured at star time (the
              "tab image" in Kade's spec) — empty if the site had none, and
              the renderer falls back to a letter tile
    'page'    empty — the renderer owns the page glyphs (icons.tsx) and maps
              them from the page key inside `key`
    'symbol'  empty — the ticker text IS the identity, the same rule the
              wheel's ticker segments follow

The star toggles by (kind, key): POST is an idempotent upsert (starring an
already-starred page returns the existing row), DELETE removes by id.
"""
from __future__ import annotations

import base64
import ipaddress
import socket
import sqlite3
from typing import Any

import httpx

KINDS = ("symbol", "page", "web")
MAX_FAVORITES = 48
MAX_KEY = 300
MAX_LABEL = 24
# 64 KB of icon bytes -> ~88 KB of base64; the column cap leaves headroom so
# a fetch that passed the byte cap can never fail the store cap.
MAX_ICON_BYTES = 64 * 1024
MAX_ICON_CHARS = 128 * 1024
ICON_TIMEOUT = 5.0
MAX_REDIRECTS = 3


def _fail(msg: str) -> None:
    raise ValueError(msg)


def validate_new(kind: Any, key: Any, label: Any, icon: Any) -> dict[str, Any]:
    """Normalize one favorite-to-be. Raises ValueError with the actual reason
    (the same honesty rule as wheels.validate: a silently 'fixed' favorite is
    a user action that half-happened)."""
    if kind not in KINDS:
        _fail(f"kind must be one of {'|'.join(KINDS)}, got {kind!r}")
    if not isinstance(key, str) or not (1 <= len(key) <= MAX_KEY):
        _fail(f"key must be 1-{MAX_KEY} characters")
    if not isinstance(label, str) or not (1 <= len(label) <= MAX_LABEL):
        _fail(f"label must be 1-{MAX_LABEL} characters")
    if not isinstance(icon, str) or len(icon) > MAX_ICON_CHARS:
        _fail("icon must be a string within the size cap")
    if icon and not icon.startswith("data:image/"):
        _fail("icon must be a data:image/* URI")

    if kind == "symbol":
        sym = key.upper()
        # A leading '/' is a futures root (/ES) — quotable through TastyTrade
        # since 2026-08-14, so starrable. The rule is mirrored in the
        # renderer's favorites.ts; change both together.
        core = sym[1:] if sym.startswith("/") else sym
        if not (1 <= len(core) <= 8) or not core.replace(".", "").isalnum():
            _fail(f"bad symbol {key!r}")
        key = sym
    elif kind == "page":
        # A platform address: name.gs with an optional query. The renderer
        # classifies before it stars, so this only has to reject nonsense.
        head = key.split("?", 1)[0]
        if not head.endswith(".gs") or not head[:-3].replace("-", "").isalnum():
            _fail(f"page key must be a .gs address, got {key!r}")
        key = head.lower() + (("?" + key.split("?", 1)[1]) if "?" in key else "")
    else:  # web
        if not key.startswith(("http://", "https://")):
            _fail("web key must be an http(s) URL")

    return {"kind": kind, "key": key, "label": label, "icon": icon}


def list_(db: sqlite3.Connection, user_id: int) -> list[dict[str, Any]]:
    rows = db.execute(
        "SELECT id, kind, key, label, icon, pos FROM favorites"
        " WHERE user_id=? ORDER BY pos, id",
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def add(db: sqlite3.Connection, user_id: int, kind: Any, key: Any,
        label: Any, icon: Any) -> dict[str, Any]:
    clean = validate_new(kind, key, label, icon)
    existing = db.execute(
        "SELECT id, kind, key, label, icon, pos FROM favorites"
        " WHERE user_id=? AND kind=? AND key=?",
        (user_id, clean["kind"], clean["key"]),
    ).fetchone()
    if existing is not None:
        # Idempotent star — except that an icon we did not have before is an
        # upgrade: the favicon often lands a beat AFTER the user clicked, so
        # a re-star (or a later capture) is how an empty tile gets its image.
        if clean["icon"] and not existing["icon"]:
            db.execute("UPDATE favorites SET icon=? WHERE id=?",
                       (clean["icon"], existing["id"]))
            return {**dict(existing), "icon": clean["icon"]}
        return dict(existing)
    count = db.execute(
        "SELECT COUNT(*) FROM favorites WHERE user_id=?", (user_id,)
    ).fetchone()[0]
    if count >= MAX_FAVORITES:
        _fail(f"at most {MAX_FAVORITES} favorites")
    pos = db.execute(
        "SELECT COALESCE(MAX(pos), 0) + 1 FROM favorites WHERE user_id=?",
        (user_id,),
    ).fetchone()[0]
    cur = db.execute(
        "INSERT INTO favorites (user_id, kind, key, label, icon, pos)"
        " VALUES (?,?,?,?,?,?)",
        (user_id, clean["kind"], clean["key"], clean["label"], clean["icon"], pos),
    )
    return {"id": cur.lastrowid, **clean, "pos": pos}


def remove(db: sqlite3.Connection, user_id: int, fav_id: int) -> bool:
    cur = db.execute(
        "DELETE FROM favorites WHERE user_id=? AND id=?", (user_id, fav_id)
    )
    return cur.rowcount > 0


# ------------------------------------------------------------- icon capture

def _blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _blocked_host(host: str) -> bool:
    """The sidecar holds credentials and sits inside the LAN, so it must not
    be steerable into fetching from itself or the local network.

    Names are RESOLVED, not just string-matched: a name that has always
    pointed at loopback (localtest.me and friends) is the ordinary case, not
    exotic — an earlier version only pattern-matched 'localhost'/.local and
    let it straight through. Unresolvable fails CLOSED: no icon is a letter
    tile, which is the documented degradation anyway.

    This is not a DNS-rebinding defense — that TOCTOU is out of scope for a
    desktop app fetching favicons — but every address a name actually
    answers with is checked.
    """
    h = host.lower().rstrip(".").strip("[]")
    if not h or h == "localhost" or h.endswith((".local", ".internal", ".localhost")):
        return True
    try:
        return _blocked_ip(ipaddress.ip_address(h))
    except ValueError:
        pass  # a name: resolve it
    try:
        infos = socket.getaddrinfo(h, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return True
    if not infos:
        return True
    for info in infos:
        try:
            if _blocked_ip(ipaddress.ip_address(info[4][0])):
                return True
        except ValueError:
            return True
    return False


def fetch_icon(url: Any) -> str:
    """Download a favicon and return it as a data: URI, or '' on any failure —
    a missing tab image degrades to a letter tile, it never blocks the star.

    Redirects are followed MANUALLY so every hop is checked. With
    follow_redirects=True the host guard governed only the first hop, and a
    page could point its <link rel=icon> at an attacker host that answered
    302 http://127.0.0.1:.../ — the credentialed sidecar would issue it.
    """
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return ""
    try:
        target = httpx.URL(url)
        with httpx.Client(timeout=httpx.Timeout(ICON_TIMEOUT, connect=2.0),
                          follow_redirects=False) as client:
            for _hop in range(MAX_REDIRECTS + 1):
                if target.scheme not in ("http", "https"):
                    return ""
                if _blocked_host(target.host or ""):
                    return ""
                with client.stream("GET", target) as resp:
                    if resp.is_redirect:
                        loc = resp.headers.get("location")
                        if not loc:
                            return ""
                        target = target.join(loc)  # resolves relative hops
                        continue
                    if resp.status_code != 200:
                        return ""
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip()
                    # Lenient on purpose: .ico is frequently served as
                    # octet-stream. What we must reject is an HTML 404 body.
                    if ctype.startswith(("text/", "application/json")):
                        return ""
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in resp.iter_bytes():
                        size += len(chunk)
                        if size > MAX_ICON_BYTES:
                            return ""  # oversized: not a favicon
                        chunks.append(chunk)
                data = b"".join(chunks)
                if not data:
                    return ""
                if not ctype.startswith("image/"):
                    ctype = "image/x-icon"
                return f"data:{ctype};base64,{base64.b64encode(data).decode('ascii')}"
        return ""  # too many redirects
    except Exception:  # noqa: BLE001 — any network failure degrades to no icon
        return ""
