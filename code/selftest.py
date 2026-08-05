"""Offline verification gate for the dashboard project (M0 scope).

Grows with each milestone; the sentinel count must be bumped whenever a check
is added so a crash mid-run can never look like a pass.
Run: python selftest.py   (from code/)
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

CODE = Path(__file__).resolve().parent
ROOT = CODE.parent

CHECKS: list[tuple[str, callable]] = []


def check(name):
    def wrap(fn):
        CHECKS.append((name, fn))
        return fn
    return wrap


@check("branding.json parses and is complete")
def _branding():
    b = json.loads((CODE / "assets/branding/branding.json").read_text(encoding="utf-8"))
    for key in ("productName", "shortName", "assistantName", "logo", "pageIcons", "theme"):
        assert key in b, f"branding.json missing {key!r}"
    for mode in ("dark", "light"):
        for token in ("bg", "surface", "border", "text", "gain", "loss"):
            assert token in b["theme"][mode], f"theme.{mode} missing {token!r}"


@check("every declared page icon exists and is valid themable SVG")
def _icons():
    b = json.loads((CODE / "assets/branding/branding.json").read_text(encoding="utf-8"))
    themable = list(b["pageIcons"].values()) + [b["logo"]["main"]]
    baked = [b["logo"]["dark"], b["logo"]["light"]]
    for rel in themable + baked:
        p = CODE / rel
        assert p.exists(), f"missing asset: {rel}"
        svg = p.read_text(encoding="utf-8")
        ET.fromstring(svg)  # well-formed XML
        assert "viewBox" in svg, f"{rel}: no viewBox"
        if rel in themable:
            assert "currentColor" in svg, f"{rel}: not theme-aware (no currentColor)"
    # The baked variants exist precisely because currentColor can't reach them;
    # dark variant must not draw in near-black, light must not draw in near-white.
    dark = (CODE / b["logo"]["dark"]).read_text(encoding="utf-8")
    assert "#E8" in dark or "#F" in dark.upper() or "white" in dark, \
        "logo-dark.svg has no light-colored stroke — invisible on dark background"


@check("REQUIREMENTS.md present with all load-bearing sections")
def _requirements():
    doc = (CODE / "docs/REQUIREMENTS.md").read_text(encoding="utf-8")
    for heading in (
        "## 1. Vision", "## 4. Functional requirements", "## 5. Non-functional",
        "## 6. Technical decisions", "## 7. Constraints", "## 9. Verification gate",
        "## 10. Roadmap",
    ):
        assert heading in doc, f"REQUIREMENTS.md missing section {heading!r}"
    assert "<!-- FILLED AFTER RESEARCH" not in doc, "REQUIREMENTS.md still has unfilled research stubs"


@check(".gitignore blocks env/, data/ and *.env")
def _gitignore():
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for rule in ("/env/", "/data/", "*.env"):
        assert rule in gi, f".gitignore missing {rule!r}"


@check("no credential-shaped strings in any tracked file")
def _secrets():
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    # Alpaca-style key ids / secrets and generic long token assignments.
    pattern = re.compile(
        r"(PK[A-Z0-9]{16,})|(APCA[-_]API[-_]SECRET[-_]KEY\s*[=:]\s*\S{20,})"
        r"|([A-Za-z_]*(SECRET|TOKEN|PASSWORD)[A-Za-z_]*\s*[=:]\s*['\"]?[A-Za-z0-9+/]{24,})"
    )
    for rel in out:
        p = ROOT / rel
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        # No self-exemption: this file's own fixtures must also be free of
        # credential-shaped strings, so the gate agrees with tools/doctor.py
        # instead of hiding a decoy the workspace scanner would flag.
        m = pattern.search(text)
        assert not m, f"credential-shaped string in tracked file {rel}: {m.group(0)[:12]}..."


# Removed 2026-08-05 with .env.example itself: the app never read env/, so a
# tracked template for it was documentation of a path that does not exist.
# Nothing is lost by dropping this check — _secrets() above scans EVERY tracked
# file for credential-shaped strings, which is the guarantee that mattered.
# Keeping a check whose subject is gone would be the no-op-that-reports-ok
# failure this suite exists to prevent.


# ----------------------------------------------------------------- M1 checks

@check("security: envelope encryption round-trip and tamper detection")
def _crypto():
    sys.path.insert(0, str(CODE))
    from backend import security as sec

    pw_hash = sec.hash_password("correct horse battery")
    assert sec.verify_password(pw_hash, "correct horse battery")
    assert not sec.verify_password(pw_hash, "wrong password 123")

    salt, dek = sec.new_salt(), sec.new_dek()
    kek = sec.derive_kek("correct horse battery", salt)
    wrapped = sec.wrap_dek(kek, dek, "kade")
    assert sec.unwrap_dek(kek, wrapped, "kade") == dek

    bad_kek = sec.derive_kek("wrong password 123", salt)
    try:
        sec.unwrap_dek(bad_kek, wrapped, "kade")
        raise AssertionError("wrong-password KEK unwrapped the DEK")
    except sec.BadPassword:
        pass

    blob = sec.encrypt_secret(dek, "fixture-plaintext-0003", 1, 7, "key_id")
    assert sec.decrypt_secret(dek, blob, 1, 7, "key_id") == "fixture-plaintext-0003"
    for (u, a, f) in ((2, 7, "key_id"), (1, 8, "key_id"), (1, 7, "secret_key")):
        try:  # any AAD component change must break decryption (row-swap defense)
            sec.decrypt_secret(dek, blob, u, a, f)
            raise AssertionError(f"AAD swap not detected for {(u, a, f)}")
        except sec.BadPassword:
            pass


@check("api: full offline auth+accounts flow, stolen-DB file holds no plaintext")
def _api_flow():
    import tempfile

    sys.path.insert(0, str(CODE))
    from fastapi.testclient import TestClient

    from backend.app import State, create_app

    # Fixtures deliberately do NOT imitate a real key's shape: doctor.py and
    # the scan below cannot tell a decoy from the real thing, and a tracked
    # file that trips the credential scanner is exactly what the rule forbids.
    fixture_id = "FIXTURE-ACCOUNT-ID-0001"
    fixture_value = "fixture-value-not-a-credential-0002"

    with tempfile.TemporaryDirectory() as tmp:
        state = State("boot-token-for-tests", db_path=Path(tmp) / "app.db")
        # base_url matters: TrustedHost (correctly) rejects TestClient's
        # default Host "testserver" — present the loopback host we allow.
        client = TestClient(create_app(state), base_url="http://127.0.0.1")
        B = {"X-App-Token": "boot-token-for-tests"}

        assert client.get("/api/health").status_code == 401, "app token not enforced"
        assert client.get("/api/health", headers=B).json()["ok"] is True

        r = client.post("/api/auth/setup", headers=B,
                        json={"username": "t", "password": "longenough1"})
        token = r.json()["token"]
        A = {**B, "Authorization": f"Bearer {token}"}

        assert client.post("/api/auth/login", headers=B,
                           json={"username": "t", "password": "wrongwrong1"}).status_code == 401
        assert client.post("/api/auth/setup", headers=B,
                           json={"username": "u2", "password": "longenough1"}).status_code == 409

        r = client.post("/api/accounts", headers=A, json={
            "broker": "alpaca", "kind": "paper", "nickname": "T",
            "credentials": {"key_id": fixture_id, "secret_key": fixture_value}})
        assert r.status_code == 200, r.text
        listed = client.get("/api/accounts", headers=A).json()
        assert listed[0]["key_hints"]["key_id"] == fixture_id[-4:]
        assert fixture_value not in json.dumps(listed)

        raw = (Path(tmp) / "app.db").read_bytes()
        assert fixture_id.encode() not in raw, "plaintext key id in DB file"
        assert fixture_value.encode() not in raw, "plaintext secret in DB file"

        # unsupported broker degrades honestly, offline
        r = client.post("/api/accounts/test", headers=A,
                        json={"broker": "fidelity", "kind": "live", "credentials": {}})
        assert r.json()["ok"] is False and "Fidelity" in r.json()["error"]

        # a bad kind is a 422, never an uncaught 500 (review 2026-08-01)
        r = client.post("/api/accounts/test", headers=A,
                        json={"broker": "alpaca", "kind": "nonsense", "credentials":
                              {"key_id": "x", "secret_key": "y"}})
        assert r.status_code == 422, f"bad kind returned {r.status_code}"

        # hints survive punctuation in the credential tail (no string packing)
        odd = "PKODD,KEY:WITH,PUNCT"
        r = client.post("/api/accounts", headers=A, json={
            "broker": "alpaca", "kind": "paper", "nickname": "Odd",
            "credentials": {"key_id": odd, "secret_key": fixture_value}})
        assert r.status_code == 200
        listed = client.get("/api/accounts", headers=A).json()
        assert listed[-1]["key_hints"]["key_id"] == odd[-4:], listed[-1]["key_hints"]

        # lock revokes every session for the user
        assert client.post("/api/auth/lock", headers=A).json()["ok"] is True
        assert client.get("/api/accounts", headers=A).status_code == 401


@check("alpaca: parsers handle real, partial, and garbage payloads")
def _alpaca_parsers():
    sys.path.insert(0, str(CODE))
    from backend.brokers.alpaca import parse_account
    from backend.brokers.base import BrokerError

    full = parse_account({
        "status": "ACTIVE", "currency": "USD", "equity": "500000", "cash": "500000",
        "buying_power": "1000000", "options_buying_power": "500000",
        "options_approved_level": 3, "pattern_day_trader": False,
        "daytrade_count": 0, "account_number": "PA3ABCDF6YY",
        "trading_blocked": False, "account_blocked": False,
    })
    assert full["equity"] == 500000.0 and full["account_last4"] == "F6YY"
    assert full["options_level"] == 3 and full["blocked"] is False

    partial = parse_account({"status": "ACTIVE"})
    assert partial["equity"] is None and partial["account_last4"] == ""

    weird = parse_account({"status": "ACTIVE", "equity": "not-a-number"})
    assert weird["equity"] is None

    for garbage in ({}, {"foo": 1}):
        try:
            parse_account(garbage)
            raise AssertionError(f"garbage accepted: {garbage}")
        except BrokerError:
            pass


@check("alpaca module is read-only: no order or position-mutation code")
def _alpaca_readonly():
    src = (CODE / "backend" / "brokers" / "alpaca.py").read_text(encoding="utf-8")
    for banned in ("/v2/orders", "httpx.post", "httpx.put", "httpx.delete",
                   "client.post", ".exercise"):
        assert banned not in src, (
            f"alpaca.py contains {banned!r} — order entry must arrive via the "
            "trading milestone deliberately, with its own gate checks")


@check("sessions: idle expiry, revocation wipe, and no shared-buffer race")
def _sessions():
    sys.path.insert(0, str(CODE))
    from backend.sessions import SessionStore

    # Negative idle window: every session is already stale, so this asserts
    # the expiry path deterministically instead of racing Windows' ~15ms
    # monotonic clock resolution.
    s = SessionStore(idle_seconds=-1)
    tok = s.create(1, "t", b"\x01" * 32)
    assert s.get(tok) is None, "stale session survived"

    s2 = SessionStore()
    t2 = s2.create(1, "t", b"\x02" * 32)
    buf = s2._peek_buffer(t2)
    s2.revoke(t2)
    assert bytes(buf) == b"\x00" * 32, "DEK not wiped on revoke"

    # REGRESSION (review 2026-08-01, high): a snapshot handed to an in-flight
    # request must be immune to a concurrent revoke. Before the fix, revoke
    # zeroed the very buffer the request was about to encrypt with, sealing
    # broker keys under an all-zero (publicly known) key.
    s3 = SessionStore()
    t3 = s3.create(1, "t", b"\x03" * 32)
    snap = s3.get(t3)
    s3.revoke(t3)
    assert snap is not None and snap.dek == b"\x03" * 32, \
        "revoke corrupted an in-flight session's key"
    assert isinstance(snap.dek, bytes), "snapshot must expose immutable bytes"


@check("db: no WAL sidecar and connections are closed (synced-folder safety)")
def _db_hygiene():
    import tempfile

    sys.path.insert(0, str(CODE))
    from backend.db import connect

    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "t.db"
        with connect(p) as con:
            con.execute(
                "INSERT INTO users (username, pw_hash, kdf_salt, wrapped_dek)"
                " VALUES ('a','h',x'00',x'00')"
            )
        leftovers = [f.name for f in Path(tmp).iterdir() if f.name != "t.db"]
        assert not leftovers, f"journal/WAL files left beside a synced DB: {leftovers}"
        with connect(p) as con:
            assert con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


@check("secret hints never expose secret material")
def _hints():
    sys.path.insert(0, str(CODE))
    from backend.brokers.base import CREDENTIAL_FIELDS

    secret_named = ("secret", "token", "password", "private")
    for broker, spec in CREDENTIAL_FIELDS.items():
        h = spec["hint_last4"]
        if h is None:
            continue
        assert h in spec["fields"], f"{broker}: hint field {h!r} is not a credential field"
        assert not any(w in h.lower() for w in secret_named), (
            f"{broker}: hint_last4={h!r} would store the tail of a secret in "
            "plaintext in a cloud-synced DB"
        )


@check("ipc proxy: route-parsed token handling, query strings pass, scrub default")
def _ipc_invariants():
    src = (CODE / "app" / "src" / "main" / "api.ts").read_text(encoding="utf-8")
    # Token handling must key on the PARSED pathname ('route'), never the raw
    # renderer string — '/api/auth/login?x=1' must hit the capture branch
    # (review 2026-08-01, high).
    assert "AUTH_CAPTURE_PATHS.has(route)" in src, \
        "token capture keys on the raw path — query-string variants dodge it"
    assert "AUTH_CLEAR_PATHS.has(route)" in src
    # Query strings must be FORWARDED, not rejected: rejecting them killed
    # /api/search?q=... in production while every test bypassed the proxy
    # (2026-08-02). The request must carry pathname + search.
    assert "url.pathname + url.search" in src, \
        "proxy does not forward query strings — omnibox search cannot work"
    # Connection pooling is banned here: undici pooling plus uvicorn's idle
    # close made requests vanish. Fresh connection per request (2026-08-02).
    assert "agent: false" in src, \
        "proxy must not pool connections to the sidecar"
    assert "url.pathname + url.search !== path" in src, \
        "non-canonical paths are no longer rejected"
    # Redirects must never be followed (a 307 could silently change which
    # route runs). node:http does not auto-follow, which is one reason it
    # replaced fetch here; assert we are still on it.
    assert "node:http" in src and "http.request(" in src, \
        "proxy no longer uses node:http — redirects may be auto-followed"
    # Scrubbing must be unconditional, not an allowlist of known routes.
    assert "function scrub(" in src and "scrub(payload)" in src, \
        "response bodies are not token-scrubbed by default"


@check("sidecar cannot outlive the shell (orphan watchdog wired both ends)")
def _orphan_watchdog():
    # REGRESSION: force-killing the shell left two stale python.exe processes
    # holding the database (observed 2026-08-01). Both halves must stay wired:
    # the shell keeps stdin as a pipe, the sidecar exits on EOF.
    main_py = (CODE / "backend" / "main.py").read_text(encoding="utf-8")
    assert "_die_with_parent" in main_py and "sys.stdin.buffer.read()" in main_py, \
        "sidecar lost its parent-death watchdog"
    sidecar_ts = (CODE / "app" / "src" / "main" / "sidecar.ts").read_text(encoding="utf-8")
    assert "'pipe', 'pipe', 'pipe'" in sidecar_ts, \
        "shell no longer holds the sidecar's stdin — the watchdog can never fire"


@check("search: exact pin, intent grammar, fuzzy typo, scoping, pages")
def _search_engine():
    import tempfile

    sys.path.insert(0, str(CODE))
    from backend import newsstore, search as search_mod
    from backend.marketdb import connect_market
    from backend.universe import Universe

    with tempfile.TemporaryDirectory() as tmp:
        con = connect_market(Path(tmp) / "m.db")
        con.executemany(
            "INSERT INTO assets (symbol, name, exchange, asset_class, tradable) VALUES (?,?,?,?,1)",
            [("SPY", "SPDR S&P 500 ETF Trust", "ARCA", "us_equity"),
             ("SPYG", "SPDR Portfolio S&P 500 Growth ETF", "ARCA", "us_equity"),
             ("AAPL", "Apple Inc. Common Stock", "NASDAQ", "us_equity"),
             ("TSLA", "Tesla, Inc. Common Stock", "NASDAQ", "us_equity"),
             # Real tickers that collide with our own page names. Without
             # these the page-ranking assertions below cannot fail, because
             # nothing would compete with the page for the top slot.
             ("DATA", "Tableau Software Inc.", "NYSE", "us_equity"),
             ("HOME", "At Home Group Inc.", "NYSE", "us_equity"),
             ("AI", "C3.ai, Inc.", "NYSE", "us_equity")])
        con.commit()
        newsstore.upsert(con, [
            {"id": 1, "headline": "Apple reports record earnings", "summary": "",
             "source": "bz", "url": "u1", "symbols": ["AAPL"],
             "created_at": "2026-08-01T12:00:00Z", "updated_at": "2026-08-01T12:00:00Z"},
            {"id": 2, "headline": "Markets rally broadly", "summary": "",
             "source": "bz", "url": "u2", "symbols": ["SPY", "SPYG"],
             "created_at": "2026-08-01T13:00:00Z", "updated_at": "2026-08-01T13:00:00Z"}])
        uni = Universe()
        uni.load(con)

        r = search_mod.query("SPY", uni, con)
        assert r["results"][0]["symbol"] == "SPY", "exact ticker must pin first"

        r = search_mod.query("SPY news", uni, con)
        assert r["intent"] == {"kind": "symbol-news", "symbol": "SPY"}
        assert any(x["type"] == "news" for x in r["results"])

        # live fallthrough consulted exactly when local store is empty
        called = []
        r = search_mod.query("TSLA news", uni, con,
                             live_news=lambda s: (called.append(s), [])[1])
        assert called == ["TSLA"], "empty local news must consult live_news"

        r = search_mod.query("aple", uni, con)
        syms = [x.get("symbol") for x in r["results"] if x["type"] == "symbol"]
        assert "AAPL" in syms, f"one-typo company name lost: {syms}"

        r = search_mod.query("acc", uni, con)
        assert any(x["type"] == "page" and x["page"] == "accounts" for x in r["results"])

        # REGRESSION (2026-08-02): a query that NAMES a page ranked below
        # random fuzzy tickers. Every retrieval list contributes the same RRF
        # score at rank 0, so the page tied with a symbol and lost on
        # insertion order — typing "settings" never surfaced Settings.
        # DATA and HOME are real tickers in this fixture, so the page has to
        # win on the rule, not by default.
        for name in ("settings", "home", "data", "accounts"):
            top = search_mod.query(name, uni, con)["results"][0]
            assert top["type"] == "page" and top["page"] == name, \
                f"{name!r} must lead with the {name} page, got {top}"
            lead = search_mod.page(name, uni, con,
                                   prefs={"web_search_enabled": False})["inhouse"][0]
            assert lead["type"] == "page" and lead["page"] == name, \
                f"{name!r} results page must lead with the {name} page, got {lead}"

        # ...and the colliding ticker is right underneath, never dropped.
        second = search_mod.query("data", uni, con)["results"][1]
        assert second["type"] == "symbol" and second["symbol"] == "DATA", \
            f"the DATA ticker must survive directly under the page, got {second}"

        # The inverse: we do NOT outrank a real instrument with a page we have
        # not built. "AI" is both C3.ai and our unbuilt AI page.
        ai = search_mod.query("ai", uni, con)["results"]
        assert ai[0]["type"] == "symbol" and ai[0]["symbol"] == "AI", \
            f"an unbuilt page must not outrank a real ticker, got {ai[0]}"
        assert any(x["type"] == "page" and x["page"] == "ai" for x in ai), \
            "the unbuilt page should still be visible, just not first"
        assert search_mod._page_exact("ai")[1] is False, "unbuilt page claims strongly"
        assert search_mod._page_exact("settings")[1] is True
        assert search_mod._page_exact("ai")[0]["ready"] is False, \
            "the UI cannot tell an unbuilt page from a working one"

        # A .gs address that reaches the backend is an address, not a literal.
        assert search_mod.normalize("settings.gs") == "settings"
        assert search_mod.normalize("spy.gs") == "spy"
        assert search_mod.normalize("gold news") == "gold news", \
            "normalize must not touch ordinary queries"
        assert search_mod.query("settings.gs", uni, con)["results"][0]["page"] == "settings"
        assert search_mod.query("spy.gs", uni, con)["results"][0]["symbol"] == "SPY"

        # REGRESSION (2026-08-02): platform hits filled every slot on page 1
        # and pushed web results to page 2, where the user never saw them.
        # The page is sectioned now: platform rows lead, the web gets its own
        # section, and both appear on page 1.
        fake_web = [{"type": "web", "id": f"w{i}", "title": f"web {i}",
                     "subtitle": "", "url": f"https://e.com/{i}", "site": "e.com"}
                    for i in range(12)]
        page = search_mod.page("SPY", uni, con, per_page=10,
                               prefs={"web_search_enabled": False})
        assert "inhouse" in page and "results" in page
        page["results"] = fake_web[:8]  # simulate the web section being filled
        assert len(page["inhouse"]) <= 8, "the platform section must be bounded"
        assert page["inhouse"], "platform hits must lead page 1"

        # SPY scoping must not leak SPYG (json_each, not LIKE)
        items = newsstore.latest(con, symbols=["AAPL"], limit=10)
        assert len(items) == 1 and items[0]["symbols"] == ["AAPL"]
        con.close()


@check("recorder: due math, honest validation, retention prune")
def _recorder_logic():
    import datetime as dt
    import tempfile

    sys.path.insert(0, str(CODE))
    from backend.marketdb import connect_market
    from backend.recorder import Recorder, validate_job

    now = dt.datetime(2026, 8, 2, 12, 0, 0, tzinfo=dt.timezone.utc)
    assert Recorder.is_due({"last_run_at": "", "interval_seconds": 60}, now)
    assert Recorder.is_due({"last_run_at": "2026-08-02T11:58:00Z", "interval_seconds": 60}, now)
    assert not Recorder.is_due({"last_run_at": "2026-08-02T11:59:30Z", "interval_seconds": 60}, now)

    assert validate_job("chain", "/ES", "", 300, 90, "future") is not None, \
        "futures must be rejected with a reason, not accepted and empty"
    assert "TastyTrade" in validate_job("chain", "SPX", "", 300, 90, "index")
    assert validate_job("bars", "SPY", "2Min", 300, 90, "us_equity") is not None
    assert validate_job("bars", "SPY", "1Min", 30, 90, "us_equity") is not None, "interval floor"
    assert validate_job("chain", "SPY", "", 900, 90, "us_equity") is None

    with tempfile.TemporaryDirectory() as tmp:
        con = connect_market(Path(tmp) / "m.db")
        rec = Recorder(con, lambda _u: None)
        with con:
            con.execute("INSERT INTO record_jobs (user_id, kind, symbol, timeframe,"
                        " interval_seconds, retention_days) VALUES (1,'bars','SPY','1Min',60,1)")
            con.executemany(
                "INSERT INTO rec_bars (symbol, timeframe, ts, open, high, low, close, volume)"
                " VALUES ('SPY','1Min',?,1,1,1,1,1)",
                [("2020-01-01T00:00:00Z",), ("2099-01-01T00:00:00Z",)])
        removed = rec.prune()
        left = con.execute("SELECT ts FROM rec_bars").fetchall()
        assert removed["bars"] == 1 and len(left) == 1 and left[0][0].startswith("2099"), \
            "prune must remove only rows older than retention"
        con.close()


@check("market-data parsers: snapshot, chain w/ optional greeks, OCC, bars")
def _alpaca_data_parsers():
    sys.path.insert(0, str(CODE))
    from backend.brokers.alpaca_data import (parse_bars, parse_chain_snapshot,
                                             parse_news_item, parse_occ,
                                             parse_stock_snapshot)
    from backend.brokers.base import BrokerError

    s = parse_stock_snapshot("SPY", {
        "latestTrade": {"p": 746.79, "t": "T", "c": ["@"]},
        "latestQuote": {"bp": 746.7, "ap": 746.9},
        "dailyBar": {"o": 1, "h": 2, "l": 0.5, "v": 100},
        "prevDailyBar": {"c": 741.63}})
    assert abs(s["change_pct"] - 100 * (746.79 - 741.63) / 741.63) < 1e-9
    assert parse_stock_snapshot("X", {})["price"] is None

    occ = parse_occ("SPY260813C00748000")
    assert occ == {"root": "SPY", "expiration": "2026-08-13", "right": "C", "strike": 748.0}
    assert parse_occ("garbage") is None

    chain = parse_chain_snapshot("SPY", {"snapshots": {
        "SPY260813C00748000": {"latestQuote": {"bp": 6.22, "ap": 6.25},
                                "impliedVolatility": 0.122,
                                "greeks": {"delta": 0.5}},
        "SPY260813P00700000": {"latestQuote": {"bp": 1.0, "ap": 1.1}},  # no greeks: 0DTE/zero-bid case
    }})
    assert len(chain) == 2
    assert chain[0]["delta"] == 0.5 or chain[1]["delta"] == 0.5
    assert any(c["delta"] is None for c in chain), "absent greeks must stay None, never invented"
    try:
        parse_chain_snapshot("SPY", {"nope": 1})
        raise AssertionError("garbage chain accepted")
    except BrokerError:
        pass

    bars = parse_bars("SPY", {"bars": {"SPY": [{"t": "T1", "o": 1, "h": 2, "l": 0.5,
                                                 "c": 1.5, "v": 10}]}})
    assert bars[0]["close"] == 1.5
    assert parse_bars("SPY", {"bars": None}) == []

    n = parse_news_item({"id": 7, "headline": "H", "symbols": ["SPY"],
                         "created_at": "T"})
    assert n["updated_at"] == "T", "missing updated_at must fall back to created_at"


@check("review regressions: sync lock scope, backfill pagination, prune, BRK.B, cooldown")
def _review_regressions():
    import tempfile

    sys.path.insert(0, str(CODE))
    from backend import newsstore
    from backend.app import State
    from backend.brokers.alpaca_data import parse_chain_snapshot
    from backend.marketdb import connect_market
    from backend.recorder import Recorder
    from backend.universe import sync_from_alpaca

    with tempfile.TemporaryDirectory() as tmp:
        con = connect_market(Path(tmp) / "m.db")

        # 1. sync must NEVER hold a write transaction across network I/O —
        #    the generator is consumed while con.in_transaction is False.
        class FakeAssets:
            def iter_assets(self, base):
                assert not con.in_transaction, \
                    "sync holds a write transaction across the network fetch"
                yield [{"symbol": "AAA", "name": "A", "exchange": "X",
                        "asset_class": "us_equity", "tradable": True}]
                assert not con.in_transaction
                yield [{"symbol": "BBB", "name": "B", "exchange": "X",
                        "asset_class": "us_equity", "tradable": True}]

        assert sync_from_alpaca(con, FakeAssets(), "http://x") == 2

        # 2. first-run backfill must follow page_token, not refetch page one.
        class FakeNews:
            def __init__(self):
                self.calls = []

            def news(self, symbols=None, limit=50, start=None, page_token=None):
                self.calls.append(page_token)
                n = len(self.calls)
                item = {"id": n, "headline": f"h{n}", "summary": "", "source": "t",
                        "url": "", "symbols": ["AAA"],
                        "created_at": f"2026-08-0{n}T00:00:00Z",
                        "updated_at": f"2026-08-0{n}T00:00:00Z"}
                return [item], (f"tok{n}" if n < 3 else None)

        fake = FakeNews()
        wrote = newsstore.backfill(con, fake, page_limit=5)
        assert fake.calls == [None, "tok1", "tok2"], \
            f"backfill did not paginate: {fake.calls}"
        assert wrote == 3

        # 3. prune: the LONGEST retention for the same data wins, and data
        #    with no job is untouched.
        rec = Recorder(con, lambda _u: None)
        with con:
            con.execute("INSERT INTO record_jobs (user_id, kind, symbol, timeframe,"
                        " interval_seconds, retention_days) VALUES (1,'chain','SPY','',900,7)")
            con.execute("INSERT INTO record_jobs (user_id, kind, symbol, timeframe,"
                        " interval_seconds, retention_days) VALUES (2,'chain','SPY','',900,3650)")
            con.executemany(
                "INSERT INTO rec_chain (underlying, ts, occ_symbol, expiration, strike, right)"
                " VALUES (?,?,?,?,?,?)",
                [("SPY", "2024-01-01T00:00:00Z", "SPY240119C1", "2024-01-19", 1, "C"),
                 ("QQQ", "2000-01-01T00:00:00Z", "QQQ000121C1", "2000-01-21", 1, "C")])
        rec.prune()
        left = {r[0] for r in con.execute("SELECT underlying FROM rec_chain").fetchall()}
        assert left == {"SPY", "QQQ"}, (
            f"prune violated its promises (left={left}): shortest retention must "
            "not beat longest, and job-less data must be kept")

        # 4. dotted-class chains survive the root filter (BRK.B -> root BRKB).
        chain = parse_chain_snapshot("BRK.B", {"snapshots": {
            "BRKB260821C00500000": {"latestQuote": {"bp": 1, "ap": 2}},
            "BRKB1260821C00500000": {"latestQuote": {"bp": 1, "ap": 2}},  # adjusted series
            "SPY260821C00500000": {"latestQuote": {"bp": 1, "ap": 2}},
        }})
        occs = {c["occ_symbol"] for c in chain}
        assert occs == {"BRKB260821C00500000", "BRKB1260821C00500000"}, occs

        # 5. live-news fallthrough is rate-gated per symbol.
        st = State("t")
        assert st.live_news_allowed("SPY") is True
        assert st.live_news_allowed("SPY") is False, "no cooldown — omnibox is a request amplifier"
        assert st.live_news_allowed("QQQ") is True, "cooldown must be per-symbol"
        con.close()


@check("market.db writers collide politely (busy_timeout regression)")
def _busy_timeout():
    # REGRESSION (production, 2026-08-02): a user searching during the initial
    # universe sync got 'database is locked' 500s — SQLite's default
    # busy_timeout is zero, and WAL only saves readers, not writer-vs-writer.
    import tempfile
    import threading

    sys.path.insert(0, str(CODE))
    from backend.marketdb import connect_market

    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "m.db"
        a = connect_market(p)
        assert a.execute("PRAGMA busy_timeout").fetchone()[0] >= 1000, \
            "busy_timeout not set — writer collisions become instant 500s"
        errors: list[str] = []

        def writer(n: int) -> None:
            try:
                con = connect_market(p)
                for i in range(60):
                    with con:
                        con.execute(
                            "INSERT OR REPLACE INTO assets (symbol, name) VALUES (?,?)",
                            (f"S{n}_{i}", "x" * 200),
                        )
                con.close()
            except Exception as e:  # noqa: BLE001
                errors.append(str(e))

        threads = [threading.Thread(target=writer, args=(k,)) for k in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        a.close()
        assert not errors, f"concurrent writers failed: {errors[:2]}"


@check("provider fallback: yahoo stays lazy, index symbols map, honesty labels")
def _providers():
    import importlib

    sys.path.insert(0, str(CODE))
    import backend.providers.yahoo as y
    from backend.market import provider_status

    importlib.reload(y)
    src = (CODE / "backend" / "providers" / "yahoo.py").read_text(encoding="utf-8")

    # REGRESSION (2026-08-02, measured twice): the fallback must not import
    # anything heavy. yfinance drags in pandas, costs ~8s, and HOLDS THE GIL —
    # in a single-process sidecar that is a total server freeze. Eagerly it
    # hung sign-in; lazily it blew three concurrent chart requests past their
    # deadline and opened the circuit breaker for five minutes, so a fresh
    # install had no market data at all while working fine from a shell.
    # Match IMPORTS, not the docstring that explains why they are gone.
    imports = [ln.strip() for ln in src.splitlines()
               if ln.strip().startswith(("import ", "from "))]
    for heavy in ("yfinance", "pandas", "curl_cffi"):
        assert not any(heavy in ln for ln in imports), (
            f"{heavy} is imported by the market fallback again — that freezes "
            "the whole sidecar (see the comment in backend/main.py)")
    reqs = (CODE.parent / "requirements.txt").read_text(encoding="utf-8")
    for heavy in ("yfinance>", "yfinance=", "curl_cffi="):
        assert heavy not in reqs, f"{heavy} is a dependency again"

    assert y.YahooProvider._map("SPX") == "^GSPC" and y.YahooProvider._map("aapl") == "AAPL"

    # Padding must never become a price. Yahoo nulls its series on holidays
    # and halts; a zero-filled bar would draw a crash that never happened.
    series = y.YahooProvider._series({
        "timestamp": [1704067200, 1704153600, 1704240000],
        "indicators": {"quote": [{"open": [10.0, None, 12.0], "high": [11.0, None, 13.0],
                                  "low": [9.0, None, 11.0], "close": [10.5, None, 12.5],
                                  "volume": [100, None, 300]}]},
    })
    assert len(series) == 2, f"null padding leaked into the series: {series}"
    assert all(b["close"] > 0 for b in series)

    # An index has no Alpaca feed but does have a keyless one, so it must not
    # be refused outright any more.
    from backend.market import quote_for
    assert quote_for("ES", "future", None)["available"] is False, \
        "futures have no source and must say so"

    st = provider_status(has_alpaca=False)
    assert st["yahoo_fallback"] is True and "delayed" in st["equities"]
    st = provider_status(has_alpaca=True)
    assert "alpaca" in st["equities"] and "TastyTrade" in st["futures"]

    # A user with no data API still deserves a full metrics grid. The
    # fallback used to read only last price and previous close, so every
    # other field rendered as an em dash for exactly the users who have no
    # alternative. fast_info is one fetch — read it all.
    for field in ("regularMarketPrice", "regularMarketDayHigh", "regularMarketDayLow",
                  "regularMarketVolume", "fiftyTwoWeekHigh", "fiftyTwoWeekLow"):
        assert field in src, f"the keyless fallback never asks for {field}"
    for key in ("day_open", "day_high", "day_low", "day_volume",
                "year_high", "year_low"):
        assert f'"{key}"' in src, f"the fallback quote does not expose {key}"
    # ...but a delayed feed has no order book, and an invented bid/ask is the
    # one number a trading app must never make up.
    assert '"bid": None, "ask": None' in src, \
        "the delayed fallback must not invent a bid/ask"


@check("no permanently animating logo on at-rest screens")
def _no_idle_animation():
    # REGRESSION (measured 2026-08-01): a continuously spinning logo held the
    # GPU process in a 60fps present loop — 10.7% of a core forever on a
    # static screen — and made pointer movement feel laggy. Static at rest
    # measured 0.2%. Screens the user sits on must not pass spin.
    renderer = CODE / "app" / "src" / "renderer" / "src"
    for rel in ("pages/Idle.tsx", "pages/AuthGate.tsx", "pages/Accounts.tsx",
                "modes/ContentApp.tsx"):
        p = renderer / rel
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if "<Logo" in line:
                assert "spin" not in line, (
                    f"{rel}: at-rest screen renders a spinning logo — that costs "
                    "~10% of a core continuously (see styles.css)"
                )


@check("omnibox: always present, .gs addresses platform pages, web opens sites")
def _omnibox_addressing():
    renderer = CODE / "app" / "src" / "renderer" / "src"
    src = (renderer / "urls.ts").read_text(encoding="utf-8")
    # Browser-bar rule: "google.com" is a place, "google" is a query, "AAPL"
    # is a ticker. A wrong navigation is not recoverable; a wrong search is.
    for piece in ("export function asUrl", "export function asGs",
                  "export function gsAddress", "localhost"):
        assert piece in src, f"address handling missing {piece}"
    assert "javascript" in src and "file" in src, \
        "url detection does not reject dangerous schemes"
    # .gs must never be mistaken for a real web address, or platform pages
    # would open as (nonexistent) websites.
    assert r"\.gs(\/|\?|$)" in src, ".gs addresses are not excluded from web URLs"

    # ONE classifier, shared. The home box used to have its own rule set, so
    # "settings.gs" navigated from the chrome bar and searched from the home
    # page — the same text meaning two things depending on where you typed it.
    assert "export function classify" in src, "there is no shared address classifier"
    idle = (renderer / "pages" / "Idle.tsx").read_text(encoding="utf-8")
    strip_src = (renderer / "components" / "TabStrip.tsx").read_text(encoding="utf-8")
    for who, text in (("home box", idle), ("chrome omnibox", strip_src)):
        assert "classify(" in text, f"{who} does not use the shared classifier"
    assert "openUrl(dest.url)" in idle, "typing an address does not open the site"

    # A bare page name is an address too: a browser navigates on a known
    # keyword without making you type the suffix.
    for name in ("home:", "accounts:", "data:", "settings:"):
        assert name in src.replace("'", "").replace('"', ''), \
            f"bare name {name!r} is not addressable"
    # ...but only for pages that EXIST. Routing "ai" to an unbuilt page would
    # dead-end, and AI is also a real ticker.
    assert "ai:" not in src.replace("'", "").replace('"', ''), \
        "an unbuilt page is being treated as a bare address"

    # The bar is always present — it is the app's search box, not a browser
    # accessory (it used to appear only on web tabs, leaving the home page
    # with no way to search from the chrome).
    strip = (renderer / "components" / "TabStrip.tsx").read_text(encoding="utf-8")
    assert "state.activeKind !== null" in strip, \
        "the address bar is conditional on tab kind — it must always show"
    tabs = (CODE / "app" / "src" / "main" / "tabs.ts").read_text(encoding="utf-8")
    assert "const chromeH = TABBAR_H + NAVBAR_H" in tabs, \
        "chrome height no longer reserves the omnibox on every tab"

    # A web result is a website: opening it as extracted plain text stripped
    # the layout that made it readable (Wikipedia infoboxes became pipes).
    search_page = (renderer / "pages" / "SearchPage.tsx").read_text(encoding="utf-8")
    assert "window.grindstone.openUrl(r.url)" in search_page, \
        "web results must open the real site, not the text reader"
    reader = (CODE / "backend" / "providers" / "reader.py").read_text(encoding="utf-8")
    assert "include_tables=False" in reader, \
        "table extraction produces pipe-delimited noise in the reader"


@check("one back button: the nav row owns it, pages and tab row do not")
def _single_back():
    """REGRESSION (2026-08-02): making the nav row permanent stacked a second
    back arrow directly under the tab row's, and every page header carried a
    third one of its own. Three arrows, one destination."""
    renderer = CODE / "app" / "src" / "renderer" / "src"
    strip = (renderer / "components" / "TabStrip.tsx").read_text(encoding="utf-8")
    tab_row = strip.split('className="strip"')[1].split("navBar")[0]
    assert "grindstoneTabs.back()" not in tab_row, \
        "the tab row has its own back button, duplicating the nav row's"
    assert strip.count("grindstoneTabs.back()") == 1, \
        "back is wired more than once in the chrome"

    for page in ("Accounts.tsx", "DataPage.tsx", "SettingsPage.tsx"):
        text = (renderer / "pages" / page).read_text(encoding="utf-8")
        assert 'className="back"' not in text, \
            f"{page} draws its own back button; the nav row already has one"
        assert "onBack" not in text, f"{page} still carries a dead onBack prop"


@check("a chart without numbers is decoration: key metrics accompany both charts")
def _key_metrics():
    renderer = CODE / "app" / "src" / "renderer" / "src"
    metrics = (renderer / "components" / "Metrics.tsx").read_text(encoding="utf-8")
    # The glanceable set. Sources disagree about what they supply, so every
    # one must be optional — but all of them must be asked for.
    for field in ("price", "change_pct", "day_open", "day_high", "day_low",
                  "prev_close", "day_volume", "bid", "ask"):
        assert f"quote.{field}" in metrics, f"metrics never show {field}"
    # A missing number must read as missing. A fabricated 0 in a price grid
    # is worse than an obvious blank.
    assert "'—'" in metrics, "absent values are not shown as an em dash"
    assert "quote.available" in metrics, \
        "metrics render without checking the quote is real"

    for page in ("SearchPage.tsx", "SymbolPage.tsx"):
        text = (renderer / "pages" / page).read_text(encoding="utf-8")
        assert "<Metrics" in text, f"{page} shows a chart with no numbers on it"
        assert "barRange(" in text, f"{page} does not show the range the chart sits in"

    # The featured card charts a quarter but ranges over a year: the 52-week
    # range is the useful one, and 260 daily bars at card size is noise.
    search_page = (renderer / "pages" / "SearchPage.tsx").read_text(encoding="utf-8")
    assert "limit=260" in search_page and "bars.slice(-90)" in search_page, \
        "featured card must range over a year and chart a quarter"


@check("news reader: content requested, stubs filtered, html rendered, cached")
def _news_reader():
    import tempfile

    sys.path.insert(0, str(CODE))
    from backend import newsstore
    from backend.marketdb import connect_market
    from backend.providers import reader

    # REGRESSION (2026-08-02): news opened as empty publisher pages because
    # the fetch asked for include_content=false. Measured: 44/50 articles
    # carry a real body when you ask, and exclude_contentless removes the
    # headline-only stubs.
    src = (CODE / "backend" / "brokers" / "alpaca_data.py").read_text(encoding="utf-8")
    assert '"include_content": "true"' in src, \
        "news is fetched without content — articles will open empty"
    assert "exclude_contentless" in src

    # HTML -> readable text, without trusting the markup.
    text = reader.html_to_text(
        "<p>First para with <b>bold</b> &amp; entity.</p>"
        "<script>alert(1)</script><p>Second para.</p>")
    assert "alert(1)" not in text, "script contents must not survive into the reader"
    assert "First para with bold & entity." in text
    assert "\n\n" in text and "Second para." in text
    assert reader.html_to_text("") == ""

    # Content must round-trip, and a later empty body must never erase it.
    with tempfile.TemporaryDirectory() as tmp:
        con = connect_market(Path(tmp) / "m.db")
        item = {"id": 1, "headline": "H", "summary": "s", "source": "bz",
                "url": "https://x/1", "symbols": ["SPY"],
                "created_at": "2026-08-01T00:00:00Z",
                "updated_at": "2026-08-01T00:00:00Z", "content": "<p>body text</p>"}
        newsstore.upsert(con, [item])
        assert newsstore.get(con, 1)["content"] == "<p>body text</p>"
        newsstore.upsert(con, [{**item, "content": ""}])
        assert newsstore.get(con, 1)["content"] == "<p>body text</p>", \
            "an empty refresh erased a stored article body"
        newsstore.set_content(con, 1, "extracted text")
        assert newsstore.get(con, 1)["content"] == "extracted text"
        con.close()


@check("web search: pinned backend, empty-vs-failure, boost reorders, settings")
def _websearch_and_settings():
    import tempfile

    sys.path.insert(0, str(CODE))
    from backend import search as search_mod, settings as settings_mod
    from backend.db import connect
    from backend.providers import websearch

    # ddgs 9.x is a METASEARCH aggregator: backend="auto" scrapes Google,
    # Bing, Yandex and Startpage — whose terms forbid automated querying —
    # with a fingerprint-randomizing transport. Pinning to DuckDuckGo is what
    # keeps this feature defensible; it must not drift back.
    # The engine is a user setting, but it must be a CHOSEN one — never
    # silently "auto" (which fans out to engines whose terms discourage
    # automated queries). Measured 2026-08-02: duckduckgo/mojeek/yahoo/google
    # return nothing from here; brave/bing/startpage/yandex work.
    assert websearch.DEFAULT_BACKEND in websearch.ALLOWED_BACKENDS
    assert websearch.DEFAULT_BACKEND != "auto", \
        "the default engine must be an explicit one, not the fan-out"
    src = (CODE / "backend" / "providers" / "websearch.py").read_text(encoding="utf-8")
    assert "backend=backend" in src, "the chosen backend is not passed to ddgs"
    assert "max_results=" in src, "max_results must be keyword — positionally it binds to region"
    # ddgs RAISES on an empty result set; counting that as a failure would
    # open the breaker after three innocent searches.
    assert "no results" in src.lower(), "empty results are not distinguished from failures"

    # The boost must change ranking, not just exist.
    web = [{"type": "web", "id": f"u{i}", "title": f"w{i}"} for i in range(4)]
    house = [{"type": "page", "page": p, "title": p} for p in ("accounts", "data")] + \
            [{"type": "symbol", "symbol": "TSLA", "title": "TSLA"}]
    plain = search_mod._rrf_weighted([(house, 1.0), (web, 1.0)])
    boosted = search_mod._rrf_weighted([(house, 6.0), (web, 1.0)])
    p_pos = [r["type"] for r in plain].index("symbol")
    b_pos = [r["type"] for r in boosted].index("symbol")
    assert b_pos < p_pos, f"in-house boost does not reorder results ({p_pos} -> {b_pos})"

    # Settings validate, clamp, and describe themselves.
    with tempfile.TemporaryDirectory() as tmp:
        with connect(Path(tmp) / "a.db") as db:
            db.execute("INSERT INTO users (username, pw_hash, kdf_salt, wrapped_dek)"
                       " VALUES ('u','h',x'00',x'00')")
            vals = settings_mod.put(db, 1, {"inhouse_boost": 999})
            assert vals["inhouse_boost"] == 4.0, "out-of-range setting must clamp"
            vals = settings_mod.put(db, 1, {"web_search_enabled": False})
            assert vals["web_search_enabled"] is False
            try:
                settings_mod.put(db, 1, {"not_a_setting": 1})
                raise AssertionError("unknown setting accepted")
            except ValueError:
                pass
    assert all({"key", "kind", "label", "help"} <= set(s) for s in settings_mod.schema()), \
        "settings schema must be self-describing for the UI"


@check("tab system: live re-parenting, no view leaks, drag protocol wired")
def _tab_system():
    app_dir = CODE / "app"
    tabs = (app_dir / "src/main/tabs.ts").read_text(encoding="utf-8")

    # Tabs must MOVE between windows, never be recreated — the whole point of
    # WebContentsView. A loadURL/loadFile inside adoptTab would mean a reload.
    adopt = tabs.split("adoptTab(")[1].split("detachToNewWindow")[0]
    for banned in ("loadURL", "loadFile", "new WebContentsView", "reload"):
        assert banned not in adopt, (
            f"adoptTab contains {banned!r} — tabs would reload when moved "
            "between windows, losing Chrome parity")

    # REGRESSION (2026-08-02): Electron does not destroy a window's views when
    # the window closes; the chrome view leaked as an orphan renderer that
    # still answered IPC.
    # Split on the DEFINITION, not the call site.
    closed = tabs.split("private onWindowClosed(")[1].split("private layout")[0]
    assert "chrome.webContents.close()" in closed, \
        "closing a window leaks its chrome view"

    # The cross-window drag protocol needs all three legs plus screen-space
    # hit testing (HTML5 DnD cannot cross OS windows).
    for piece in ("tabdrag:start", "tabdrag:move", "tabdrag:end", "stripHit"):
        assert piece in tabs, f"drag protocol missing {piece}"

    preload = (app_dir / "src/preload/index.ts").read_text(encoding="utf-8")
    assert "grindstoneTabs" in preload and "dragEnd" in preload, \
        "preload does not expose the tab bridge"

    # In-app browsing must feel like a browser, not an embedded frame:
    # navigation controls, an address bar, a real Chrome user agent (the
    # Electron default makes sites serve degraded pages), and dark pages.
    for piece in ("nav:forward", "nav:reload", "nav:goto", "browsingUserAgent"):
        assert piece in tabs, f"browser chrome missing {piece}"
    strip = (app_dir / "src/renderer/src/components/TabStrip.tsx").read_text(encoding="utf-8")
    assert "'addr'" in strip or '"addr"' in strip, "there is no address bar"
    main_ts = (app_dir / "src/main/index.ts").read_text(encoding="utf-8")
    assert "nativeTheme.themeSource = 'dark'" in main_ts, \
        "opened pages will render light in a dark app"
    assert "WebContentsForceDark" in main_ts
    # The live e2e is the only thing that crosses the real proxy + real
    # windows; it must stay runnable.
    assert (app_dir / "e2e/run.mjs").exists(), "the live e2e diagnostic is missing"
    pkg = json.loads((app_dir / "package.json").read_text(encoding="utf-8"))
    assert "e2e" in pkg["scripts"], "npm run e2e is not wired"


@check("browsing: sign-in leaves for the real browser, one honest identity")
def _browsing_identity():
    """REGRESSION (2026-08-04): Google answered every sign-in with "Couldn't
    sign you in - this browser or app may not be secure", and ordinary sites
    misbehaved in ways that looked unrelated. Two distinct causes, and the fix
    must keep them apart.

    1. IDENTITY FLOWS. Google blocks sign-in in any embedded view as policy,
       and its published criteria say a browser "must not use another browser's
       User-Agent string, such as Chrome" on accounts.google.com - which is
       exactly what our compatibility UA does. Unfixable in-app; RFC 8252 says
       hand it to the system browser. Three doors lead to a sign-in page and
       all three must hand off, including will-redirect: a real Google entry
       arrives by redirect far more often than by a typed URL.
    2. SELF-CONTRADICTION. setUserAgent() overrides the UA string only;
       Chromium keeps client-hint brands in a separate struct no Electron API
       writes. Measured: this session sent NO Sec-CH-UA headers at all while
       claiming Chrome/150, where real Chrome 150 sent the full triple to the
       same URL, and navigator.userAgentData.brands lacked "Google Chrome".
       Fixing it needs BOTH mechanisms - webRequest reaches the wire, the CDP
       override reaches the JS object, and neither reaches the other.
    """
    tabs = (CODE / "app/src/main/tabs.ts").read_text(encoding="utf-8")
    # "must not appear" assertions run against CODE ONLY. Twice now a check
    # here has failed on the comment explaining why the thing it forbids was
    # removed — the prose names it, so a naive substring test sees it.
    # Line-based on purpose. A `/\* … \*/` regex latched onto a stray comment
    # opener and swallowed a real line of code, silently under-counting.
    code = "\n".join(
        ln for ln in tabs.splitlines()
        if not ln.lstrip().startswith(("//", "*", "/*", "*/"))
    )
    code = re.sub(r"\s+//.*$", "", code, flags=re.M)

    # -- 1. the handoff, at every door -------------------------------------
    assert "IDENTITY_HOSTS" in tabs and "accounts.google.com" in tabs, \
        "the identity host list is gone - sign-in would load in-app again"
    assert tabs.count("isIdentityUrl(") >= 3, \
        "an identity door is unguarded: popup, navigation and direct-open must all hand off"
    assert "handOffToBrowser" in tabs and "shell.openExternal" in tabs, \
        "nothing hands the sign-in URL to the real browser"
    assert "will-redirect" in tabs, \
        "will-navigate is main-frame-only and never fires for server-side " \
        "redirects, which is how a real Google sign-in is usually reached"

    # -- 2. one identity, told the same way three times ---------------------
    assert "process.platform" in tabs and "uaPlatformToken" in tabs, \
        "the UA hardcodes an OS again - it would claim Windows on the mac build " \
        "while Sec-CH-UA-Platform, which Chromium derives from the real OS, says macOS"
    assert "onBeforeSendHeaders" in tabs and "Sec-CH-UA" in tabs, \
        "no client-hint headers: the request claims Chrome and sends none of " \
        "the hints every real Chrome has sent since v89"
    # The CDP metadata override (webContents.debugger + setUserAgentOverride)
    # is deliberately NOT here: it makes DevTools unusable on every browser tab
    # and an A/B against Cloudflare Turnstile measured no benefit at all
    # (baseline 0/4 verified, headers-only 2/4, CDP attached 0/4). Re-adding it
    # should have to survive that same measurement again.
    assert "debugger.attach" not in code, \
        "the CDP client-hint override is back: it costs DevTools on every " \
        "browser tab and measured zero benefit - re-run the A/B before keeping it"

    # -- 3. hardening that stayed, and hardening that was too blunt ---------
    assert "disableDialogs: true" not in code, \
        "disableDialogs makes window.confirm() return false unconditionally, " \
        "so confirm-gated buttons on ordinary sites silently do nothing"
    assert "safeDialogs: true" in tabs, "dialog-spam protection was dropped entirely"
    assert "AUTO_GRANT" in tabs, "the blanket permission deny is back"
    for perm in ("storage-access", "clipboard-sanitized-write", "fullscreen"):
        assert perm in tabs, f"{perm} is denied again - Chrome never prompts for it"
    # storage-access is the one that matters most: denying it puts any site
    # using an embedded SSO iframe into an endless login loop.
    grant_block = tabs.split("AUTO_GRANT")[1].split("])")[0]
    for banned in ("geolocation", "'media'", "notifications"):
        assert banned not in grant_block, \
            f"{banned} must never be auto-granted to an untrusted page"
    assert "(_wc, _perm, cb) => cb(false)" not in code, "blanket deny restored"

    # A frameless BaseWindow gets no application menu, so Electron's built-in
    # DevTools accelerator never applies: without an explicit binding there is
    # no way to see any page's console, and "the site renders blank" can never
    # be diagnosed. Both view kinds must have it.
    assert "enableDevToolsShortcut" in code, "no way to open DevTools anywhere in the app"
    assert code.count("enableDevToolsShortcut(") >= 3, \
        "DevTools must be wired on BOTH app views and browser tabs (plus its definition)"

    # The boundary these tabs exist behind is unchanged.
    for must in ("nodeIntegration: false", "contextIsolation: true", "sandbox: true",
                 "webviewTag: false"):
        assert must in tabs, f"browser-tab hardening lost {must}"


@check("gesture wheels: spec layout, honest validation, safe browser preload")
def _gesture_wheels():
    import tempfile

    sys.path.insert(0, str(CODE))
    from backend import wheels
    from backend.db import connect

    # ---- the document: defaults match Kade's spec exactly -----------------
    doc = wheels.validate(wheels.default_doc())
    ids = [w["id"] for w in doc["wheels"]]
    assert set(wheels.BUILTIN_IDS) <= set(ids), "a default wheel is missing"
    main = next(w for w in doc["wheels"] if w["id"] == "main")
    segs = main["segments"]
    assert len(segs) == 8, "the main wheel is 8 segments: 4 corners + 4 cardinal"
    # N=AI wheel, E=tabs wheel, S=search tool, W=Favorites wheel (v4: the
    # hand-typed tickers wheel became the dynamic Favorites wheel)
    assert segs[0] == {"type": "wheel", "wheel": "ai", "label": "AI"}, segs[0]
    assert segs[2]["type"] == "wheel" and segs[2]["wheel"] == "tabs", segs[2]
    assert segs[4]["type"] == "tool" and segs[4]["tool"] == "search", segs[4]
    assert segs[6]["type"] == "wheel" and segs[6]["wheel"] == "favorites", segs[6]
    # between them: home, news, Charts, settings (v2: the SPY slot became the
    # multi-chart page, per Kade's 2026-08-02 spec change)
    others = {s.get("route") or s.get("ticker") for s in (segs[1], segs[3], segs[5], segs[7])}
    assert others == {"idle", "news", "charts", "settings"}, others
    tabs_wheel = next(w for w in doc["wheels"] if w["id"] == "tabs")
    assert tabs_wheel.get("dynamic") == "tabs", "the tabs wheel must be dynamic"

    # v3 (Kade's charting-tools spec): the MAIN chart wheel is visibility
    # toggles + wheel navigations; the tools live in the Draw / Measure /
    # Indicators / Timeframe wheels one level down.
    assert doc["version"] == wheels.DOC_VERSION
    chart = next(w for w in doc["wheels"] if w["id"] == "chart")
    tools = {s.get("tool") for s in chart["segments"] if s["type"] == "chart"}
    assert tools == {"vis:draw", "vis:ind"}, \
        f"the main chart wheel carries visibility toggles only, got {tools}"
    navs = {s.get("wheel") for s in chart["segments"] if s["type"] == "wheel"}
    assert {"chart-draw", "chart-ind", "chart-tf", "chart-measure",
            "chart-tickers", "main"} <= navs, \
        f"the chart wheel is missing a tool-wheel navigation: {navs}"
    draw = next(w for w in doc["wheels"] if w["id"] == "chart-draw")
    draw_tools = {s.get("tool") for s in draw["segments"] if s["type"] == "chart"}
    # No 'select' AND no 'pointer': Select was removed 2026-08-05, and the slot
    # was not refilled with Pointer because Escape already disarms (the Escape
    # ladder ends in setTool('pointer')). A wheel segment that duplicates a key
    # press is worse than one fewer segment.
    want_draw = {"trend", "hline", "vline", "circle", "delete", "trim"}
    assert want_draw <= draw_tools, \
        f"the draw wheel is missing {want_draw - draw_tools} (it has {draw_tools})"
    assert not ({"select", "pointer"} & draw_tools), \
        "the draw wheel got a select/pointer segment back - Escape is the way " \
        "out of a tool, and a segment that only disarms is dead weight"
    measure = next(w for w in doc["wheels"] if w["id"] == "chart-measure")
    m_tools = {s.get("tool") for s in measure["segments"] if s["type"] == "chart"}
    assert {"measure", "inspect", "clearmeasure"} <= m_tools, \
        f"the measure wheel is missing tools: {m_tools}"
    for wid in ("chart-add", "chart-ind", "chart-tickers", "chart-tf"):
        dyn = next(w for w in doc["wheels"] if w["id"] == wid)
        assert dyn.get("dynamic") == wid, f"{wid} must be dynamic"
    assert "tf:1Day" in wheels.CHART_TOOLS and "isolate" in wheels.CHART_TOOLS
    ai = next(w for w in doc["wheels"] if w["id"] == "ai")
    assert any(s["type"] == "placeholder" for s in ai["segments"]), \
        "the AI wheel is a placeholder and must say so"
    assert any(s["type"] == "wheel" and s["wheel"] == "main" for s in ai["segments"]), \
        "every non-main default wheel needs a way back to main"

    # ---- validation says WHY, and corrupt data falls back -----------------
    for bad, why in [
        ({"config": {}, "wheels": []}, "empty"),
        ({"config": {}, "wheels": [{"id": "main", "name": "M", "symbol": "x",
                                    "segments": [{"type": "warp"}] * 3}]}, "bad type"),
        ({"config": {"locked": "ghost"}, "wheels": wheels.default_doc()["wheels"]},
         "ghost lock"),
    ]:
        try:
            wheels.validate(bad)
            raise AssertionError(f"validator accepted {why}")
        except ValueError:
            pass
    bad = wheels.default_doc()
    bad["wheels"] = [w for w in bad["wheels"] if w["id"] != "favorites"]
    try:
        wheels.validate(bad)
        raise AssertionError("deleting a built-in wheel was accepted")
    except ValueError:
        pass

    # round-trip through the real storage, corrupt row falls back to defaults
    with tempfile.TemporaryDirectory() as tmp:
        with connect(Path(tmp) / "a.db") as db:
            db.execute("INSERT INTO users (username, pw_hash, kdf_salt, wrapped_dek)"
                       " VALUES ('u','h',x'00',x'00')")
            d1 = wheels.default_doc()
            d1["config"]["locked"] = "favorites"
            stored = wheels.put(db, 1, d1)
            assert stored["config"]["locked"] == "favorites"
            assert wheels.get(db, 1)["config"]["locked"] == "favorites"
            db.execute("UPDATE user_settings SET value='{oops' WHERE key=?",
                       (wheels.DOC_KEY,))
            assert wheels.get(db, 1)["config"]["locked"] is None, \
                "a corrupt wheels doc must fall back to defaults, not crash"
            # A pre-versioned (v1) doc regenerates whole — never a half-mix
            # of generations that validation would then reject forever.
            old = wheels.default_doc()
            old.pop("version")
            db.execute("UPDATE user_settings SET value=? WHERE key=?",
                       (json.dumps(old), wheels.DOC_KEY))
            regen = wheels.get(db, 1)
            assert regen["version"] == wheels.DOC_VERSION and \
                any(w["id"] == "chart" for w in regen["wheels"]), \
                "a v1 wheels doc must regenerate to the current defaults"

    # ---- geometry: ONE module, both sides import it -----------------------
    app_src = CODE / "app" / "src"
    shared = (app_src / "shared" / "wheelGeometry.ts").read_text(encoding="utf-8")
    assert "export function segmentAt" in shared and "WHEEL_DEAD_ZONE" in shared
    wheel_main = (app_src / "main" / "wheel.ts").read_text(encoding="utf-8")
    overlay = (app_src / "renderer" / "src" / "modes" / "WheelOverlay.tsx").read_text(
        encoding="utf-8")
    assert "shared/wheelGeometry" in wheel_main, "main does not use the shared geometry"
    assert "shared/wheelGeometry" in overlay, "the overlay does not use the shared geometry"
    # Main decides what a release selects — never the renderer's hover report
    # (the release can beat the last hover report across the IPC boundary).
    assert "segmentAt(s.center.x" in wheel_main, \
        "main must compute the released segment itself"
    assert "wheelui:hover" not in wheel_main, \
        "main is trusting renderer-reported hover again — that races the release"

    # ---- the spec's interaction rules, where they are encoded -------------
    assert "s.mode = 'click'" in wheel_main and "'wheel:mode'" in wheel_main, \
        "hold-release on a wheel-nav must hand over to click mode"
    assert "config.locked === s.wheelId ? null : s.wheelId" in wheel_main, \
        "the hub must toggle lock/unlock of the current wheel"
    assert "despawn" in wheel_main and "'blur'" in wheel_main, \
        "the wheel must close when its window loses focus"

    # ---- the browser preload stays a one-way street -----------------------
    bp = (app_src / "preload" / "browser.ts").read_text(encoding="utf-8")
    bp_imports = [ln for ln in bp.splitlines()
                  if ln.strip().startswith(("import ", "from "))]
    assert not any("contextBridge" in ln for ln in bp_imports), \
        "the browser preload exposes an API surface to third-party pages"
    assert "exposeInMainWorld" not in bp, \
        "the browser preload exposes an API surface to third-party pages"
    assert "isTrusted" in bp, "synthetic page events could puppet the wheel"
    assert bp.count("ipcRenderer.send") == 1 and "'wheel:evt'" in bp, \
        "the browser preload must send exactly one fixed channel"
    tabs_src = (app_src / "main" / "tabs.ts").read_text(encoding="utf-8")
    assert "browserPreload" in tabs_src, "browser tabs no longer forward right-clicks"

    # ---- no permanent animation on the overlay ----------------------------
    css = (app_src / "renderer" / "src" / "styles.css").read_text(encoding="utf-8")
    for line in css.splitlines():
        if "wheel" in line and "animation" in line and "infinite" in line:
            raise AssertionError(f"looping wheel animation: {line.strip()}")

    # ---- the ticker snapshot rule: fetched once, never re-polled ----------
    face = (app_src / "renderer" / "src" / "components" / "WheelFace.tsx").read_text(
        encoding="utf-8")
    for src_name, src in (("WheelOverlay", overlay), ("WheelFace", face)):
        assert "setInterval" not in src, \
            f"{src_name} re-polls — wheel colors must not flash while open"


@check("split view + chart context + picker: the v2 shell contracts hold")
def _wheels_v2_shell():
    app_src = CODE / "app" / "src"

    # ---- split view: state, menu, divider, and the offset seam ------------
    tabs_src = (app_src / "main" / "tabs.ts").read_text(encoding="utf-8")
    for piece in ("'tabs:split'", "'tabs:unsplit'", "'split:drag'", "'tabmenu:open'"):
        assert piece in tabs_src, f"split IPC missing {piece}"
    assert "0.2" in tabs_src and "0.8" in tabs_src, \
        "the divider ratio must clamp (a 0-width pane is unrecoverable by mouse)"
    assert "buildFromTemplate" in tabs_src, "the tab context menu is gone"
    # The RIGHT pane's x offset feeds wheel coordinate conversion — a wheel
    # spawned over the right pane lands mid-window without it.
    assert "splitOffsetX" in tabs_src and "return 0 // replaced" not in tabs_src, \
        "splitOffsetX is still the stub — wheel coords over a right pane are wrong"
    divider = (app_src / "renderer" / "src" / "modes" / "SplitDivider.tsx").read_text(
        encoding="utf-8")
    assert "setPointerCapture" in divider and "screenX" in divider, \
        "the divider cannot track a drag beyond its own 8px width"
    strip = (app_src / "renderer" / "src" / "components" / "TabStrip.tsx").read_text(
        encoding="utf-8")
    assert "tabMenu(" in strip and "split-mate" in strip, \
        "the strip neither opens the tab menu nor marks split pairs"
    # Right-clicking a TAB must never spawn the wheel (Kade's spec).
    events = (app_src / "renderer" / "src" / "wheelEvents.ts").read_text(encoding="utf-8")
    assert ".strip-tab" in events, "tab right-clicks leak into the gesture wheel"

    # ---- chart context: every chart declares itself -----------------------
    chart = (app_src / "renderer" / "src" / "components" / "Chart.tsx").read_text(
        encoding="utf-8")
    # Working charts declare the wheel context; COMPACT previews must not —
    # a chart wheel over the search featured card offered eight tools that
    # all silently did nothing (review 2026-08-02).
    assert "data-wheel-context={compact ? undefined : 'chart'}" in chart, \
        "chart context must be declared for working charts and NOT for previews"
    wheel_src = (app_src / "main" / "wheel.ts").read_text(encoding="utf-8")
    assert "'chart'" in wheel_src and "context" in wheel_src, \
        "wheel.ts lost the chart-context spawn rule"
    assert "chart:action" in wheel_src, "chart segments have no delivery channel"
    draw = (app_src / "renderer" / "src" / "components" / "ChartDraw.ts").read_text(
        encoding="utf-8")
    assert "subscribeVisibleLogicalRangeChange" in draw, \
        "drawings will not re-project on pan/zoom"
    forbidden = [ln for ln in draw.splitlines()
                 if "setInterval" in ln and not ln.strip().startswith(("//", "*"))]
    assert not forbidden, f"the drawing engine polls: {forbidden[:1]}"
    sym = (app_src / "renderer" / "src" / "pages" / "SymbolPage.tsx").read_text(
        encoding="utf-8")
    assert "onChartAction" in sym, "the symbol page ignores chart-wheel actions"
    multi = (app_src / "renderer" / "src" / "pages" / "ChartsPage.tsx").read_text(
        encoding="utf-8")
    assert "multi_chart" in multi and "onChartAction" in multi, \
        "the multi-chart page neither persists nor hears the wheel"
    assert "normalize" in multi, "the % comparison mode is gone"

    # ---- the picker redesign ----------------------------------------------
    panel = (app_src / "renderer" / "src" / "components" / "GesturesPanel.tsx").read_text(
        encoding="utf-8")
    assert "searchCatalog" in panel, "the picker does not search the catalog"
    assert "wheel-seg-row" not in panel, \
        "the rejected row-per-segment editor is back"

    # ---- adversarial-review regressions (2026-08-02, all confirmed live) --
    # Lock kills the wheel: an auto-lock left a live overlay over the
    # sign-in form, unclickable in hold mode.
    assert "onLockChanged" in tabs_src and "onLockChanged" in wheel_src, \
        "locking no longer despawns a live wheel"
    # New views must not land above the overlay (invisible wedged wheel).
    assert "attachedOverlay" in tabs_src, \
        "reconcile no longer re-raises the wheel overlay above new views"
    # Saves are sequenced and lock-preserving; deletes retarget references.
    assert "saveSeq" in panel and "droppedWheelId" in panel, \
        "picker saves race again / deleting a locked wheel wedges again"
    # No-context dynamic chart wheels must not fire actions that lie.
    assert "Right-click a chart" in wheel_src, \
        "null-ctx chart wheels advertise state they do not know"
    # Stale bars must never paint (wrong-timeframe candles under a 1D button).
    assert "barsSeq" in sym, "SymbolPage bars race is back"
    # The multi-chart drawing key must carry scale semantics.
    assert "normalize ? '%' : '$'" in multi.replace('"', "'"), \
        "drawings mix dollar and percent anchors in one bucket again"


@check("chart tools v3: engine vocabulary, editor, settings, isolate, real-input e2e")
def _chart_tools_v3():
    app_src = CODE / "app" / "src"
    rend = app_src / "renderer" / "src"

    # The engine carries the full interacting tool set.
    draw = (rend / "components" / "ChartDraw.ts").read_text(encoding="utf-8")
    # No "select": the tool was removed 2026-08-05 (left-click in pointer
    # picks). Leaving it in this list was a live false green — it passed only
    # because the doc comment explaining the removal happens to contain the
    # quoted token, so the gate asserted the OPPOSITE of the truth and would
    # have failed the moment anyone reworded a comment.
    for tool in ("pointer", "trend", "hline", "vline", "circle", "delete",
                 "trim", "measure", "inspect"):
        assert f"'{tool}'" in draw, f"the drawing engine lost the {tool} tool"
    for api_name in ("updateDrawing", "deleteSelected", "clearMeasures",
                     "setDrawingsHidden", "onChange"):
        assert api_name in draw, f"engine API lost {api_name}"

    # Exact-value editing: per-orientation price/time boxes (Kade's spec).
    editor = (rend / "components" / "DrawEditor.tsx").read_text(encoding="utf-8")
    assert "price" in editor and ("date" in editor or "time" in editor), \
        "the selected-object editor lost its exact-value boxes"

    # Indicator settings edit PERIODS while the ind:* vocabulary stays fixed.
    inds = (rend / "components" / "IndicatorSettings.tsx").read_text(encoding="utf-8")
    assert "period" in inds, "indicator settings no longer edit periods"
    chart = (rend / "components" / "Chart.tsx").read_text(encoding="utf-8")
    assert "IndicatorParams" in chart and "periodOf" in chart, \
        "the chart no longer parametrizes indicator periods"

    # Both pages speak the whole v3 vocabulary; isolate lives on the multi.
    sym = (rend / "pages" / "SymbolPage.tsx").read_text(encoding="utf-8")
    multi = (rend / "pages" / "ChartsPage.tsx").read_text(encoding="utf-8")
    for piece in ("vis:draw", "clearmeasure", "tf:"):
        assert piece in sym and piece in multi, f"a page lost the {piece} action"
    assert "isolated" in multi and "all-but" in multi.replace("—", "-") or \
        "isolated" in multi, "isolate left the multi-chart page"
    assert "vis:ind" in sym, "the indicator-visibility stash left the symbol page"

    # The wheel side: timeframe wheel + state decoration + settings segment.
    wheel_src = (app_src / "main" / "wheel.ts").read_text(encoding="utf-8")
    for piece in ("chart-tf", "decorateChartState", "'settings'"):
        assert piece in wheel_src, f"wheel v3 lost {piece}"

    # The e2e must keep driving REAL input — that requirement found three
    # bugs (point-less clicks, below-the-fold clicks, stale rects) that no
    # synthetic-event test could see.
    e2e = (CODE / "app" / "e2e" / "run.mjs").read_text(encoding="utf-8")
    assert "dispatchMouseEvent" in e2e and "scrollIntoView" in e2e, \
        "the chart tools are no longer exercised with trusted input"


@check("chart selection: measures are first-class, Escape disarms, lock wins")
def _chart_selection():
    """REGRESSION (2026-08-04): five separate reports that were one bug plus
    two one-liners.

    The engine had exactly ONE picking function and its loop body was
    `bucket().drawings`, while the selection it fed was Drawing-typed end to
    end. So a measurement could not be clicked, could not be selected, could
    not be deleted by the Delete key or the Delete tool, and getState() threw
    a selected measure's id away silently. Widening the pick and letting the
    flat id list carry all three kinds closed all of it at once; ids are
    globally unique (one mkId counter), so no per-kind bookkeeping exists.

    Separately: Escape's ladder stopped after "clear selection" and never
    disarmed the TOOL, so the next click drew again. And the wheel spawn read
    "context BEATS the locked default", which meant locking the Draw wheel and
    right-clicking the chart you were drawing on swapped it for the chart hub
    -- the lock worked everywhere except the surface it exists for.
    """
    draw = (CODE / "app/src/renderer/src/components/ChartDraw.ts").read_text(encoding="utf-8")

    # -- picking reaches every kind ---------------------------------------
    assert "hitAny" in draw, "the widened picking entry point is gone"
    for verb in ("clickSelect", "clickDelete"):
        seg = draw.split(f"private {verb}(")[1][:400]
        assert "hitAny(" in seg, f"{verb} went back to the drawings-only hitTest"
    # hitTest itself must stay lines-only: trim and measure-snap call it
    # directly and widening it would let "trim" resolve to a measurement.
    trim = draw.split("private clickTrim(")[1][:300]
    assert "hitTest(" in trim, "clickTrim must keep using the narrow hitTest"

    # -- left-click selects, and there is no Select tool (2026-08-05) -------
    # The 2026-08-04 fix widened picking but left selection behind a mode you
    # had to arm. Forgetting to arm it is indistinguishable from a broken
    # picker, which is exactly how "measures are not clickable" survived the
    # check above: the wiring it greps for was correct the whole time. An e2e
    # click on a placed measure now proves the behaviour end to end.
    ids = draw.split("DRAW_TOOL_IDS = [")[1].split("]")[0]
    assert "'select'" not in ids, \
        "the select tool is back - plain left-click in pointer is the picker now"
    sw = draw.split("switch (this.tool)")[1][:1200]
    assert "case 'pointer':" in sw and "clickSelect" in sw.split("case 'pointer':")[1][:160], \
        "pointer no longer routes to clickSelect - left-click stopped selecting"
    # The switch arm is not enough on its own: the ORIGINAL bug was an early
    # return ABOVE the switch, which the slice above cannot see. Restoring it
    # would make every left-click in the resting tool a no-op again with the
    # gate still green.
    head = draw.split("private handleClick(")[1].split("switch (this.tool)")[0]
    assert "'pointer') return" not in head, \
        "handleClick returns early for pointer again - that is the original " \
        "bug (nothing is pickable without arming a tool), and the case-arm " \
        "assertion above passes right through it"

    # Picking must respect visibility. render() skips everything when hidden,
    # so a picker that does not would select invisible objects - and since
    # pointer is the resting tool, any stray click could do it.
    for fn in ("hitAny", "hitTest"):
        seg = draw.split(f"private {fn}(")[1][:700]
        assert "this.hidden" in seg, \
            f"{fn} ignores this.hidden - a left-click can select, and Delete " \
            f"can remove, a drawing the user cannot see"

    # Plain click replaces, modifier adds. Toggling on every plain click made
    # the resting gesture silently accumulate a multi-kind selection that the
    # single-object editor's Delete button would then sweep.
    sel = draw.split("private clickSelect(")[1][:900]
    assert "downAdditive" in sel, \
        "clickSelect toggles on every plain click again - selections accumulate " \
        "invisibly and Delete takes more than the editor is showing"

    # -- drag to move (2026-08-05) -----------------------------------------
    # A drag has three failure modes that all LOOK like it works, so each gets
    # its own assertion. The behaviour itself is proved by the e2e, which drags
    # an h-line with held-button CDP input and reads the price back out of the
    # editor - source greps alone would not have caught any of these.
    assert "private moveDragged(" in draw and "private endDrag(" in draw, \
        "drag-to-move is gone"
    mv = draw.split("private moveDragged(")[1][:900]
    assert "timeAtX" in mv and "priceAtY" in mv, \
        "moveDragged translates in DATA units again - the x axis is affine in " \
        "bar index, so a constant time delta is not a constant pixel delta " \
        "across a weekend and the drawing would drift from the cursor"
    assert "nearestBarTime" in mv, \
        "a dragged point is no longer snapped to a bar, so it can land between " \
        "bars and stop being projectable"
    assert "justDragged" in draw, \
        "the trailing click after a drag is unguarded: the library fires a " \
        "click on the mouseup that ends one, and it lands at the DROP point"
    # Bounded by the end of the function, not a character count: the first
    # version of this check sliced [:1400] and failed on a correct engine
    # because the line it wants sits at 1780. A window that has to be re-tuned
    # whenever a comment grows is a check that will cry wolf.
    down = draw.split("const onDown = (")[1].split("this.host.addEventListener('mousedown'")[0]
    assert "handleScroll: false" in down, \
        "grabbing a drawing no longer suspends the chart's own pan, so a drag " \
        "moves the drawing AND scrolls the chart under it"
    endd = draw.split("private endDrag(")[1][:700]
    assert "handleScroll: true" in endd, \
        "pan/zoom is never restored after a drag - the chart would be frozen"

    # The repaint guard. Pointer is the app's RESTING tool and it now has to
    # follow the crosshair to know what is under it; rendering unconditionally
    # there is a full overlay rebuild at mouse-move rate. Only an id CHANGE may
    # paint. (Measured 2026-08-01: a 60fps repaint loop cost 10.7% of a core.)
    assert "hoverId" in draw, "the pointer-mode hover id is gone"
    assert "if (id === this.hoverId) return" in draw, \
        "pointer mode repaints on every crosshair move again - the unchanged-id " \
        "early-out is what keeps the default tool free"

    # No page may offer a Select button, and no wheel may offer the segment.
    for page in ("ChartsPage", "SymbolPage"):
        src = (CODE / f"app/src/renderer/src/pages/{page}.tsx").read_text(encoding="utf-8")
        assert "'select'" not in src, f"{page} still offers a Select tool"
    cat = (CODE / "app/src/renderer/src/wheelCatalog.ts").read_text(encoding="utf-8")
    assert "chart:select" not in cat, "the wheel catalog still offers a Select segment"
    wsrc = (CODE / "backend/wheels.py").read_text(encoding="utf-8")
    tools = wsrc.split("CHART_TOOLS = (")[1].split(")")[0]
    assert '"select"' not in tools, \
        "backend still accepts a 'select' chart tool - a stored wheel could fire it"

    # -- one doomed set sweeps all three collections ------------------------
    seg = draw.split("deleteSelected(): void")[1][:600]
    for arr in ("b.drawings", "b.measures", "b.pins"):
        assert f"{arr} = {arr}.filter" in seg, \
            f"deleteSelected no longer removes {arr} - that is the whole bug"

    # -- chips are clickable, and only from a COMPLETE frame ----------------
    assert "hotZones" in draw and "zoneDraft" in draw, "chip hit-zones are gone"
    assert "this.hotZones = this.zoneDraft" in draw, \
        "hit-zones must be published atomically at the END of render(); " \
        "picking against a half-built list resolves to the wrong object"

    # -- Escape's third rung ------------------------------------------------
    esc = draw.split("if (e.key === 'Escape')")[1][:1400]
    assert "setTool('pointer')" in esc, \
        "Escape stops at clearing the selection again - the tool stays armed " \
        "and the next click draws"

    # -- the pages act on EVERY selected kind, not just drawings ------------
    for page in ("ChartsPage", "SymbolPage"):
        src = (CODE / f"app/src/renderer/src/pages/{page}.tsx").read_text(encoding="utf-8")
        assert "s.selected.length > 0" in src, \
            f"{page}'s Delete reads `selection` (drawings only) again, so a " \
            f"selected measurement falls through to arming click-to-delete"
        assert "setDrawTool(s.tool)" in src or "setTool(s.tool)" in src, \
            f"{page} no longer mirrors the engine's tool back - after Escape " \
            f"disarms the engine the toolbar button goes dead"

    # -- the lock outranks chart context ------------------------------------
    wheel = (CODE / "app/src/main/wheel.ts").read_text(encoding="utf-8")
    assert "usableOverChart" in wheel, "the lock/chart-context precedence fix is gone"
    assert "Context BEATS the locked default" not in wheel, \
        "the old precedence is back: a locked chart-draw wheel gets replaced " \
        "by the chart hub on the chart it is being used to draw on"


@check("candle depth setting + fixed tab actions")
def _candles_and_tab_actions():
    sys.path.insert(0, str(CODE))
    from backend import settings as settings_mod

    # The setting exists with the specced default: ALL available history.
    spec = settings_mod.SPEC.get("chart_candles")
    assert spec and spec["default"] == "all" and "all" in spec["choices"], \
        "chart_candles must exist and default to 'all'"
    assert all(c == "all" or c.isdigit() for c in spec["choices"])

    # The bars endpoint honors it: limit=0 (the new default) consults the
    # setting; 'all' reaches for full history (Yahoo period max, deep spans).
    app_src = (CODE / "backend" / "app.py").read_text(encoding="utf-8")
    assert "chart_candles" in app_src and "want_all" in app_src, \
        "the bars endpoint no longer consults the candle-depth setting"
    assert '"max" if want_all' in app_src, \
        "'all' no longer requests Yahoo's full listing history"
    # Pages defer to it: their main chart fetches carry NO limit param.
    for page in ("SymbolPage.tsx", "ChartsPage.tsx"):
        src = (CODE / "app" / "src" / "renderer" / "src" / "pages" / page
               ).read_text(encoding="utf-8")
        main_fetches = [ln for ln in src.splitlines()
                        if "/bars?timeframe=${timeframe}" in ln]
        assert main_fetches and all("limit=" not in ln for ln in main_fetches), \
            f"{page} hardcodes a chart depth instead of the user's setting"

    # Tab actions live OUTSIDE the scroller (always reachable) and the
    # Previous-tab jump exists end to end.
    tabs_src = (CODE / "app" / "src" / "main" / "tabs.ts").read_text(encoding="utf-8")
    assert "'tabs:prev'" in tabs_src and "prevActiveId" in tabs_src, \
        "the Previous-tab jump left the tab system"
    strip = (CODE / "app" / "src" / "renderer" / "src" / "components" /
             "TabStrip.tsx").read_text(encoding="utf-8")
    assert "strip-actions" in strip and "prevTab" in strip, \
        "the fixed New/Previous tab buttons left the strip"
    assert "strip-new" not in strip, \
        "the old inline + is back — it scrolls away with a full strip"
    css = (CODE / "app" / "src" / "renderer" / "src" / "styles.css"
           ).read_text(encoding="utf-8")
    assert "overflow-x: auto" in css.split(".strip-tabs {")[1].split("}")[0], \
        "a full strip no longer scrolls — tabs will overflow invisibly"


@check("help system: sections match the search index, content names real UI")
def _help_system():
    import re as re_mod
    import tempfile

    sys.path.insert(0, str(CODE))
    from backend import search as search_mod
    from backend.marketdb import connect_market
    from backend.universe import Universe

    rend = CODE / "app" / "src" / "renderer" / "src"
    help_src = (rend / "pages" / "HelpPage.tsx").read_text(encoding="utf-8")

    # The two lists that MUST stay in lockstep: backend topics point at page
    # section ids — a drifted pair sends a search to a section that is not
    # there, which reads as "search is broken".
    page_sections = set(re_mod.findall(r"^  '([a-z-]+)',$", help_src, re_mod.M))
    topic_sections = {t["section"] for t in search_mod.HELP_TOPICS}
    assert topic_sections == page_sections, (
        f"help topics and page sections drifted: only-in-search="
        f"{topic_sections - page_sections} only-in-page={page_sections - topic_sections}")

    # Searching a feature lands on its section, ranked first (Kade's spec:
    # "searching for drawing will pull the drawing section").
    with tempfile.TemporaryDirectory() as tmp:
        con = connect_market(Path(tmp) / "m.db")
        con.execute("INSERT INTO assets (symbol, name, exchange, asset_class, tradable)"
                    " VALUES ('SPY','SPDR','ARCA','us_equity',1)")
        con.commit()
        uni = Universe()
        uni.load(con)
        for q, section in (("drawing", "drawing"), ("trim", "drawing"),
                           ("isolate", "multi-charts"), ("measure", "measuring"),
                           ("split", "split-view"), ("wheel", "wheels")):
            rows = search_mod.query(q, uni, con)["results"]
            hit = next((r for r in rows if r.get("page") == "help"), None)
            assert hit and hit["section"] == section, \
                f"searching {q!r} does not surface Help·{section}: {hit}"
            assert rows.index(hit) <= 2, \
                f"Help·{section} ranks too low for {q!r}: position {rows.index(hit)}"
        con.close()

    # The manual must name the REAL UI, not an imagined one. "Sel" was in this
    # list until 2026-08-05 and became the inverse of its own purpose: the
    # button was deleted, the manual still described it, and this check kept
    # the stale text green while failing anyone who fixed it. A required name
    # is only safe while the thing it names exists.
    for real in ("Trim", "Ptr", "Clear M", "⇄", "Candles per chart",
                 "Split with", "⦿", "Esc", "Timeframe", "Gesture wheels"):
        assert real in help_src, f"help content never mentions {real!r}"
    for gone in ("Sel ·", "<strong>Sel</strong>"):
        assert gone not in help_src, \
            f"the manual still documents the removed Select button ({gone!r})"
    # ...and never a stale count or path that rots.
    assert "SELFTEST OK" not in help_src, "help must not hardcode the gate count"

    # Addressing: help.gs and section deep-links round-trip.
    urls_src = (rend / "urls.ts").read_text(encoding="utf-8")
    assert "'help'" in urls_src and "help.gs?s=" in urls_src, \
        "help is not addressable"
    app_tsx = (rend / "App.tsx").read_text(encoding="utf-8")
    assert "help:" in app_tsx, "the help route cannot carry a section"


# ----------------------------------------------------- backtest engine checks

@check("favorites: store honesty, wheels v4, mirrored page lists")
def _favorites_system():
    import re as re_mod
    import sqlite3

    sys.path.insert(0, str(CODE))
    from backend import favorites, wheels
    from backend.db import _SCHEMA

    # ---- the store: honest validation, idempotent star --------------------
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    con.execute("INSERT INTO users (id, username, pw_hash, kdf_salt, wrapped_dek)"
                " VALUES (1,'t','x',x'00',x'00')")
    f1 = favorites.add(con, 1, "symbol", "spy", "SPY", "")
    assert f1["key"] == "SPY", "symbols normalize upper"
    assert favorites.add(con, 1, "symbol", "SPY", "other", "")["id"] == f1["id"], \
        "starring twice must be idempotent, not a duplicate or an error"
    fp = favorites.add(con, 1, "page", "Help.gs?s=drawing", "Help", "")
    assert fp["key"] == "help.gs?s=drawing", "page address lowercases, query survives"
    favorites.add(con, 1, "web", "https://example.com/", "Example", "")
    assert [f["key"] for f in favorites.list_(con, 1)] == \
        ["SPY", "help.gs?s=drawing", "https://example.com/"], "insertion order holds"
    for bad in (("symbol", "WAYTOOLONG1", "x", ""), ("page", "nope", "x", ""),
                ("web", "ftp://x", "x", ""), ("symbol", "SPY", "", ""),
                ("web", "https://x.co/", "x", "javascript:alert(1)")):
        try:
            favorites.add(con, 1, *bad)
            raise AssertionError(f"favorites accepted {bad}")
        except ValueError:
            pass
    assert favorites.remove(con, 1, f1["id"]) and not favorites.remove(con, 1, f1["id"])
    # A favicon that lands AFTER the star is an upgrade, not a no-op: sites
    # commonly report theirs a beat late, and the tile would keep a letter
    # for the life of the favorite.
    late = favorites.add(con, 1, "web", "https://late.example/", "Late", "")
    assert late["icon"] == ""
    filled = favorites.add(con, 1, "web", "https://late.example/", "Late",
                           "data:image/png;base64,AAAA")
    assert filled["id"] == late["id"] and filled["icon"].startswith("data:image/"), \
        "a re-star carrying an icon must fill an empty one"

    # ---- icon capture guards: the sidecar holds credentials and sits inside
    # the LAN, so favicon fetches must never be steerable at either ---------
    for host in ("localhost", "127.0.0.1", "192.168.1.4", "10.0.0.9",
                 "svc.internal", "0.0.0.0", "169.254.1.1", "[::1]", ""):
        assert favorites._blocked_host(host), f"{host} must be blocked"
    # A literal PUBLIC address stays allowed (no DNS — the gate is offline).
    assert not favorites._blocked_host("93.184.216.34")
    assert favorites.fetch_icon("file:///etc/passwd") == ""
    assert favorites.fetch_icon("http://localhost:8000/icon.png") == ""

    # REVIEW 2026-08-04: the guard used to string-match names and check only
    # the FIRST hop. Both holes were real — 'localtest.me' is a public name
    # that has always resolved to 127.0.0.1, and any attacker host could
    # answer 302 http://127.0.0.1:… These two cases hold that line.
    import http.server
    import socket as socket_mod
    import threading

    def _resolves_to_loopback(name: str) -> bool:
        try:
            socket_mod.getaddrinfo(name, None)
        except OSError:
            return False
        return True

    if _resolves_to_loopback("localtest.me"):  # skipped offline, never faked
        assert favorites._blocked_host("localtest.me"), \
            "a NAME resolving to loopback must be blocked, not just the literal"

    hits: list[str] = []

    class _Redirector(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — stdlib's spelling
            hits.append(self.path)
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "http://blocked.example/secret.png")
                self.end_headers()
            else:
                body = b"\x89PNG\r\n\x1a\n" + b"0" * 32
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, *a):  # keep the gate's output clean
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _Redirector)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    real_blocked = favorites._blocked_host
    # The server must live on loopback to be offline, so swap the policy for
    # the duration: 'blocked.example' plays the private host. The REDIRECT
    # HANDLING under test is entirely real.
    favorites._blocked_host = lambda h: h == "blocked.example"
    try:
        assert favorites.fetch_icon(f"http://127.0.0.1:{port}/icon.png").startswith(
            "data:image/png;base64,"), "a plain favicon must still be captured"
        assert favorites.fetch_icon(f"http://127.0.0.1:{port}/redirect") == "", \
            "a redirect to a blocked host must be refused, not followed"
    finally:
        favorites._blocked_host = real_blocked
        srv.shutdown()
        srv.server_close()
    assert "/redirect" in hits, "the redirect case never reached the test server"

    # ---- wheels v4: tickers wheel gone, Favorites dynamic, links validated -
    doc = wheels.validate(wheels.default_doc())
    ids = [w["id"] for w in doc["wheels"]]
    assert "tickers" not in ids and "favorites" in ids, ids
    fav_wheel = next(w for w in doc["wheels"] if w["id"] == "favorites")
    assert fav_wheel.get("dynamic") == "favorites", "Favorites builds from live stars"
    d2 = wheels.default_doc()
    d2["wheels"][0]["segments"][1] = {"type": "link",
                                      "address": "help.gs?s=drawing", "label": "Help"}
    assert wheels.validate(d2)["wheels"][0]["segments"][1]["address"] == "help.gs?s=drawing"
    for addr in ("javascript:x", "notanaddress", "x" * 301):
        d2["wheels"][0]["segments"][1] = {"type": "link", "address": addr, "label": "x"}
        try:
            wheels.validate(d2)
            raise AssertionError(f"wheels accepted link address {addr!r}")
        except ValueError:
            pass

    # ---- the API surface + the one broadcast chokepoint -------------------
    app_py = (CODE / "backend/app.py").read_text(encoding="utf-8")
    for frag in ('"/api/favorites"', '"/api/pages"', "fetch_icon"):
        assert frag in app_py, f"app.py missing {frag}"
    api_ts = (CODE / "app/src/main/api.ts").read_text(encoding="utf-8")
    assert "favorites:changed" in api_ts and "/api/favorites" in api_ts, \
        "the api proxy must broadcast favorites mutations to every view"

    # ---- the platform-page list has TWO mirrors that must agree with
    # urls.ts (this drift actually happened: 'help' was missing from tabs.ts
    # and a help.gs tab counted as ticker HELP in symbolTabs) ---------------
    urls_ts = (CODE / "app/src/renderer/src/urls.ts").read_text(encoding="utf-8")
    m = re_mod.search(r"const PAGES = \[(.*?)\]", urls_ts, re_mod.S)
    renderer_pages = set(re_mod.findall(r"'([a-z]+)'", m.group(1)))
    tabs_ts = (CODE / "app/src/main/tabs.ts").read_text(encoding="utf-8")
    m = re_mod.search(r"const PAGE_NAMES = new Set\(\[(.*?)\]\)", tabs_ts, re_mod.S)
    main_pages = set(re_mod.findall(r"'([a-z]+)'", m.group(1)))
    assert renderer_pages == main_pages, \
        f"urls.ts PAGES != tabs.ts PAGE_NAMES, diff: {renderer_pages ^ main_pages}"

    # ---- the built surfaces consume the store (string-level; the e2e is
    # the functional proof of each) -----------------------------------------
    idle = (CODE / "app/src/renderer/src/pages/Idle.tsx").read_text(encoding="utf-8")
    assert "/api/favorites" in idle and "onFavoritesChanged" in idle, \
        "the home grid must render the live store and follow the broadcast"
    assert "const FAVORITES" not in idle, \
        "the hardcoded app tiles are gone — provider apps live in the launcher"
    strip = (CODE / "app/src/renderer/src/components/TabStrip.tsx").read_text(
        encoding="utf-8")
    for frag in ("addr-star", "apps-btn", "favoriteIdentity", "launcherToggle"):
        assert frag in strip, f"TabStrip.tsx missing {frag}"
    tabs_src = tabs_ts  # already read above
    assert "page-favicon-updated" in tabs_src and "activeFavicon" in tabs_src, \
        "the shell must capture the tab image the star submits"
    wheel_ts = (CODE / "app/src/main/wheel.ts").read_text(encoding="utf-8")
    for frag in ("favorites", "openAddress", "launcher:toggle", "/api/pages"):
        assert frag in wheel_ts, f"wheel.ts missing {frag}"
    panel = (CODE / "app/src/renderer/src/components/GesturesPanel.tsx").read_text(
        encoding="utf-8")
    assert "favoriteEntry" in panel and "submitTicker" not in panel, \
        "the picker offers favorites, not a free-typed ticker"


@check("bt engine unit tests (120 known-answer tests from the tastytrade refs)")
def _bt_engine():
    # REGRESSION (2026-08-04): this decorator was stacked on _favorites_system
    # instead, ~170 lines up. check() appends (name, fn) and returns fn
    # unchanged, so BOTH names registered against the favorites function: the
    # gate printed "ok bt engine unit tests" while running the favorites check
    # twice, the count still read 40/40, and the engine's known-answer suite
    # had silently not run since it was vendored. A duplicate-function-object
    # assertion in main() now makes that shape impossible to reintroduce.
    #
    # The vendored engine ships its own suite: fees, buying power, profit
    # definition, selection ties, sandboxing — every number read off the real
    # reference exports. A subprocess so numpy never enters THIS process.
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "backend/bt/tests", "-q",
         "--no-header", "-p", "no:cacheprovider"],
        cwd=CODE, capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, f"engine tests failed:\n{(r.stdout or r.stderr)[-1500:]}"
    assert " passed" in r.stdout, f"pytest produced no pass line:\n{r.stdout[-300:]}"


@check("backtests api: seeding, validation surface, honest refusals, report key")
def _backtests_api():
    import os
    import tempfile
    sys.path.insert(0, str(CODE))
    from fastapi.testclient import TestClient
    from backend.app import State, create_app

    with tempfile.TemporaryDirectory() as td:
        old = os.environ.get("GRINDSTONE_DATA_DIR")
        os.environ["GRINDSTONE_DATA_DIR"] = td
        try:
            state = State("boot", db_path=Path(td) / "app.db",
                          market_path=Path(td) / "market.db")
            app = create_app(state)

            # The GIL contract (requirements.txt): the sidecar process must
            # never import numpy — the engine's heavy half loads only in the
            # runner subprocess. This is the tripwire.
            assert "numpy" not in sys.modules, \
                "backend import chain pulled numpy into the sidecar process"

            c = TestClient(app, base_url="http://127.0.0.1",
                           headers={"X-App-Token": "boot"})
            r = c.post("/api/auth/setup",
                       json={"username": "bt", "password": "fixture-pw-123"})
            c.headers["Authorization"] = f"Bearer {r.json()['token']}"

            # Seeding: 7 bundled presets, exactly 3 flagged as calibration
            # references, present from the first GET.
            presets = c.get("/api/backtests/presets").json()
            assert len(presets) == 7, f"seeded {len(presets)} presets, wanted 7"
            assert sum(p["calibration"] for p in presets) == 3

            # Validation carries the engine's own message quality: an unknown
            # key names itself; an unknown rule variable is named too.
            good = presets[0]["spec"]
            r = c.post("/api/backtests/validate", json={"spec": good}).json()
            assert r["ok"] and "describe" in r
            r = c.post("/api/backtests/validate",
                       json={"spec": dict(good, bogus=1)}).json()
            assert not r["ok"] and "bogus" in r["error"]
            bad = json.loads(json.dumps(good))
            bad["entry"] = {"when": "vixx > 5"}
            r = c.post("/api/backtests/validate", json={"spec": bad}).json()
            assert not r["ok"] and "vixx" in r["error"]

            # Built-in presets are read-only references.
            r = c.patch(f"/api/backtests/presets/{presets[0]['id']}",
                        json={"name": "x"})
            assert r.status_code == 409, "built-in preset was editable"

            # No chain DB in this temp world: starting a run must refuse with
            # the honest message, not fail 60s later.
            r = c.post("/api/backtests/runs", json={"kind": "run", "spec": good})
            assert r.status_code == 422 and "chain database" in r.json()["detail"]

            # The report route is the ONE token-exempt door; without a valid
            # single-use key it must refuse, and a key must die on first use.
            # Garbage keys (incl. non-ASCII) must 403, never 500 — this route
            # is reachable without any token.
            assert c.get("/api/backtests/report/1").status_code == 403
            assert c.get("/api/backtests/report/1", params={"k": "käße"}).status_code == 403
            mgr = state.backtests
            key = mgr.mint_report_key(1, "report.html")
            assert mgr.consume_report_key(1, key) == "report.html"
            assert mgr.consume_report_key(1, key) is None, "report key reusable"
            assert mgr.consume_report_key(1, "wrong-key") is None

            # Cancel and delete are separate verbs: a Cancel racing a finished
            # run must 409, never fall through to deleting the results
            # (review 2026-08-03). And the runs list must carry `calib`, or
            # the Verify-engine scorecard silently never renders.
            mcon = __import__("backend.marketdb", fromlist=["connect_market"]) \
                .connect_market(Path(td) / "market.db")
            with mcon:
                mcon.execute(
                    "INSERT INTO backtest_runs (id, user_id, name, kind, status,"
                    " calib, report_files) VALUES (77, 1, 'cal', 'calibration',"
                    " 'done', '[{\"reference\": \"put\"}]', '[\"put.html\"]')")
            mcon.close()
            assert c.post("/api/backtests/runs/77/cancel").status_code == 409, \
                "cancelling a finished run must refuse, not delete"
            rows = c.get("/api/backtests/runs").json()
            assert rows and rows[0]["calib"] == [{"reference": "put"}], \
                "runs list dropped `calib` — the scorecard cannot render"
            assert c.delete("/api/backtests/runs/77").status_code == 200
            assert c.get("/api/backtests/runs").json() == []

            # Bad dates are refused before a run row exists.
            r = c.post("/api/backtests/runs",
                       json={"kind": "run", "spec": good, "start": "not-a-date"})
            assert r.status_code == 422 and "ISO date" in r.json()["detail"]

            # A fresh install owns its data store from the first status call
            # (source 'recorded', honestly empty), and one click wires the
            # recorder into it.
            st = c.get("/api/backtests/status").json()
            assert st["source"] == "recorded" and st["recorded"]["days"] == 0
            assert not st["can_run"] and "record" in st["reason"]
            r = c.post("/api/backtests/data/setup-recording",
                       json={"underlying": "SPY"}).json()
            assert sorted(r["created"]) == ["bars", "chain"], r
            r = c.post("/api/backtests/data/setup-recording",
                       json={"underlying": "SPY"}).json()
            assert r["created"] == [], "setup-recording is not idempotent"
            jobs = c.get("/api/datamgmt/jobs").json()
            assert len(jobs) == 2 and {j["kind"] for j in jobs} == {"bars", "chain"}
            # empty sync completes cleanly through the background thread
            assert c.post("/api/backtests/data/sync",
                          json={"underlying": "SPY"}).json()["ok"]
            import time as _time
            for _ in range(50):
                sync = c.get("/api/backtests/status").json()["sync"]
                if sync["state"] in ("done", "error"):
                    break
                _time.sleep(0.1)
            assert sync["state"] == "done" and sync["days"] == 0, sync
            # calibration must refuse the recorded source honestly
            r = c.post("/api/backtests/runs", json={"kind": "calibration"})
            assert r.status_code == 422, r.text
        finally:
            if old is None:
                os.environ.pop("GRINDSTONE_DATA_DIR", None)
            else:
                os.environ["GRINDSTONE_DATA_DIR"] = old


@check("recorded-data pipeline: rec_chain -> app-owned store -> engine runs on it")
def _btdata_pipeline():
    """The path a real user takes: no spy_options.db anywhere — the recorder
    captured chain snapshots, the app's own database is built from them, and
    the engine backtests on that. Chains are synthetic but ARBITRAGE-CLEAN
    (Black-76 prices, exact put-call parity) so the engine's forward
    extraction, strike selection and fills all exercise for real, through the
    actual runner subprocess."""
    import json as _json
    import math
    import os
    import subprocess
    import tempfile
    sys.path.insert(0, str(CODE))

    def norm_cdf(x):
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    def b76(F, K, T, v, call):
        if T <= 0 or v <= 0:
            return max(F - K, 0.0) if call else max(K - F, 0.0)
        d1 = (math.log(F / K) + 0.5 * v * v * T) / (v * math.sqrt(T))
        d2 = d1 - v * math.sqrt(T)
        if call:
            return F * norm_cdf(d1) - K * norm_cdf(d2)
        return K * norm_cdf(-d2) - F * norm_cdf(-d1)

    with tempfile.TemporaryDirectory() as td:
        old = os.environ.get("GRINDSTONE_DATA_DIR")
        os.environ["GRINDSTONE_DATA_DIR"] = td
        try:
            from backend import btdata
            from backend.marketdb import connect_market

            # --- a recorder's worth of synthetic SPY chains: 5 weekdays,
            # two expirations, snapshots at 19:45Z (in-hours ET), plus one
            # stale weekend snapshot that must be filtered out.
            mcon = connect_market(Path(td) / "market.db")
            days = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08",
                    "2026-01-09"]
            exps = ["2026-01-16", "2026-02-20"]
            iv = 0.20
            with mcon:
                for i, day in enumerate(days):
                    F = 500.0 + i * 0.5
                    ts = f"{day}T19:45:00Z"
                    d0 = dt_date(day)
                    for exp in exps:
                        T = max(( dt_date(exp) - d0).days, 1) / 365.0
                        for k in range(400, 601, 5):
                            for right in ("C", "P"):
                                px = b76(F, float(k), T, iv, right == "C")
                                d1 = (math.log(F / k) + 0.5 * iv * iv * T) / (iv * math.sqrt(T))
                                delta = norm_cdf(d1) if right == "C" else norm_cdf(d1) - 1.0
                                mcon.execute(
                                    "INSERT INTO rec_chain (underlying, ts, occ_symbol,"
                                    " expiration, strike, right, bid, ask, iv, delta)"
                                    " VALUES ('SPY',?,?,?,?,?,?,?,?,?)",
                                    (ts, f"SPY{exp}{right}{k}", exp, float(k), right,
                                     max(px - 0.02, 0.01), px + 0.02, iv, delta))
                # stale weekend snapshot: same rows, Saturday ts — must not sync
                mcon.execute(
                    "INSERT INTO rec_chain (underlying, ts, occ_symbol, expiration,"
                    " strike, right, bid, ask) VALUES"
                    " ('SPY','2026-01-10T19:45:00Z','X','2026-01-16',500,'P',1,1.1)")

            # --- sync into the app-owned store
            store = btdata.data_db_path("SPY")
            dcon = btdata.connect_data(store)
            r = btdata.sync_from_recorded(mcon, dcon, "SPY")
            assert r["days"] == 5, f"synced {r['days']} days, wanted 5 (weekend filtered)"
            got = btdata.stats(dcon)
            assert got["first"] == "2026-01-05" and got["last"] == "2026-01-09"
            row = dcon.execute(
                "SELECT bid, ask, mark, delta FROM opt WHERE d=20260105 AND cp=1"
                " AND strike=500000 AND exp=20260116").fetchone()
            assert row is not None, "strike*1000 / cp / ymd mapping broke"
            assert abs(row["mark"] - 0.5 * (row["bid"] + row["ask"])) < 1e-9
            # incremental: nothing new -> nothing copied
            assert btdata.sync_from_recorded(mcon, dcon, "SPY")["days"] == 0
            dcon.close()
            mcon.close()

            # --- the REAL runner subprocess backtests on the synced store
            out_dir = Path(td) / "run"
            out_dir.mkdir()
            job = {
                "run_id": 1, "kind": "run", "name": "pipeline",
                "spec": {"name": "pipeline", "capital": 100000,
                         "legs": [{"action": "sell", "right": "put",
                                   "delta": 0.2, "dte": 10}],
                         "exits": {"dte": 9}},
                "out_dir": str(out_dir),
                "paths": {"options_db": str(store), "bars_db": str(store),
                          "vix_csv": str(CODE / "backend" / "bt" / "data" / "vix_history.csv"),
                          "cache": str(Path(td) / "cache.db")},
            }
            (out_dir / "job.json").write_text(_json.dumps(job), encoding="utf-8")
            env = dict(os.environ, PYTHONIOENCODING="utf-8")
            r2 = subprocess.run(
                [sys.executable, "-u", "-m", "backend.bt_runner",
                 str(out_dir / "job.json")],
                cwd=CODE, capture_output=True, text=True, timeout=120, env=env,
                stdin=subprocess.DEVNULL)
            assert r2.returncode == 0, f"runner failed:\n{r2.stderr[-800:]}"
            result = _json.loads((out_dir / "result.json").read_text(encoding="utf-8"))
            assert not result.get("error"), f"engine error: {result.get('error')}"
            assert len(result["daily"]) == 5, \
                f"engine saw {len(result['daily'])} trading days, wanted 5"
            assert result["trades"] or result.get("skipped"), \
                "no trade and no skip reason — selection never engaged"
            assert (out_dir / "report.html").is_file()
        finally:
            if old is None:
                os.environ.pop("GRINDSTONE_DATA_DIR", None)
            else:
                os.environ["GRINDSTONE_DATA_DIR"] = old


def dt_date(s: str):
    import datetime as _dt
    return _dt.date.fromisoformat(s)


@check("backtest page: registered in every seam that must agree")
def _backtest_page():
    rend = CODE / "app" / "src" / "renderer" / "src"
    urls_src = (rend / "urls.ts").read_text(encoding="utf-8")
    assert "'backtest'" in urls_src, "urls.ts PAGES: backtest.gs would become a ticker"
    assert "backtest: 'backtest'" in urls_src, "urls.ts PAGE_ROUTES missing backtest"
    app_tsx = (rend / "App.tsx").read_text(encoding="utf-8")
    assert "'backtest'" in app_tsx, \
        "App.tsx parseRoute: the backtest route dead-ends to idle"
    content = (rend / "modes" / "ContentApp.tsx").read_text(encoding="utf-8")
    assert "BacktestPage" in content and "case 'backtest':" in content, \
        "ContentApp never mounts BacktestPage"
    tabs_src = (CODE / "app" / "src" / "main" / "tabs.ts").read_text(encoding="utf-8")
    assert "'backtest'" in tabs_src, \
        "main tabs.ts PAGE_NAMES: the wheel would misread backtest.gs as a ticker"
    assert "backtest:openReport" in tabs_src, "report-open IPC not wired in main"
    strip = (rend / "components" / "TabStrip.tsx").read_text(encoding="utf-8")
    assert "case 'backtest':" in strip, "tab strip has no backtest icon case"
    preload = (CODE / "app" / "src" / "preload" / "index.ts").read_text(encoding="utf-8")
    assert "openBacktestReport" in preload, "preload bridge misses openBacktestReport"
    main_api = (CODE / "app" / "src" / "main" / "api.ts").read_text(encoding="utf-8")
    assert "/api/backtests/report/" in main_api and "report_key" in main_api, \
        "main/api.ts cannot build a report URL"
    assert "endsWith('/report-key')" in main_api, \
        "the proxy no longer walls off report-key minting from renderers"
    assert "shippableUrl" in tabs_src, \
        "tab URLs ship to renderers without stripping the report key/port"

    sys.path.insert(0, str(CODE))
    from backend import search as search_mod
    entry = next((p for p in search_mod.PAGES if p["key"] == "backtest"), None)
    assert entry is not None and entry["ready"], "search registry misses backtest"

    # The report middleware exemption is deliberate and must stay EXACTLY one
    # route wide: a GET on the report prefix, nothing else.
    app_py = (CODE / "backend" / "app.py").read_text(encoding="utf-8")
    assert app_py.count("startswith(\"/api/backtests/report/\")") == 1, \
        "the token-middleware exemption changed shape — re-review it"

    from backend import marketdb
    assert marketdb.SCHEMA_VERSION >= 3 and "backtest_runs" in marketdb._SCHEMA, \
        "backtest_runs will never exist on already-migrated market.db files"
    from backend import db as app_db
    assert "backtest_presets" in app_db._SCHEMA, "backtest_presets missing from app.db"


@check("installer: entry points, endings, icons and launch target agree")
def _installer():
    """The install path is the first thing a new machine runs, and every way it
    can break is silent: a CRLF shebang, a missing exec bit, one non-ASCII byte
    in a PowerShell file, an icon that is declared but absent, a shortcut
    pointing at a build step nobody runs."""
    for rel in ("Install.cmd", "Install.command", "install.sh",
                "setup.ps1", "setup.sh", ".gitattributes",
                "tools/installer/windows/Install.ps1",
                "tools/installer/windows/Steps.ps1",
                "tools/installer/posix/install.sh",
                "tools/installer/posix/ui.sh",
                "tools/installer/posix/shortcuts.sh",
                "tools/icons/make-icons.ps1"):
        assert (ROOT / rel).is_file(), f"installer file missing: {rel}"

    posix = ["Install.command", "install.sh", "setup.sh",
             "tools/installer/posix/install.sh",
             "tools/installer/posix/ui.sh",
             "tools/installer/posix/shortcuts.sh"]
    windows = ["Install.cmd", "tools/installer/windows/Install.ps1",
               "tools/installer/windows/Steps.ps1", "tools/icons/make-icons.ps1"]

    # Shebang, LF, and the exec bit as git records it. A .command without mode
    # 755 does nothing whatsoever when double-clicked in Finder, and CRLF turns
    # the shebang into "bad interpreter: /usr/bin/env bash^M".
    modes = {}
    for line in subprocess.run(["git", "ls-files", "-s"], cwd=ROOT,
                               capture_output=True, text=True, check=True).stdout.splitlines():
        meta, _, name = line.partition("\t")
        modes[name] = meta.split()[0]
    for rel in posix:
        raw = (ROOT / rel).read_bytes()
        assert raw.startswith(b"#!"), f"{rel}: no shebang"
        assert b"\r\n" not in raw, f"{rel}: CRLF endings break the shebang on macOS/Linux"
        assert modes.get(rel) == "100755", \
            f"{rel}: git mode is {modes.get(rel)}, not 100755 - it will not be executable in a clone"

    # PowerShell 5.1 reads a BOM-less UTF-8 script as ANSI: a single em-dash in
    # a comment broke setup.ps1's parse (fresh-clone test, 2026-08-02).
    for rel in posix + windows:
        raw = (ROOT / rel).read_bytes()
        bad = [i for i, byte in enumerate(raw) if byte > 127]
        assert not bad, f"{rel}: non-ASCII byte at offset {bad[0]} - keep installer scripts pure ASCII"

    ga = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    for rule in ("*.sh", "*.command", "eol=lf"):
        assert rule in ga, f".gitattributes must pin {rule} or another machine's autocrlf will undo it"

    # Icons: declared in branding, present, and genuinely parseable.
    brand = json.loads((CODE / "assets/branding/branding.json").read_text(encoding="utf-8"))
    ico = CODE / brand["logo"]["windowIcon"]
    assert ico.is_file(), f"branding declares {brand['logo']['windowIcon']} but it is not there"
    raw = ico.read_bytes()
    assert raw[:4] == b"\x00\x00\x01\x00", "app.ico is not an icon file"
    count = int.from_bytes(raw[4:6], "little")
    assert count >= 4, f"app.ico carries only {count} sizes"
    for i in range(count):
        off = 6 + 16 * i
        length = int.from_bytes(raw[off + 8:off + 12], "little")
        start = int.from_bytes(raw[off + 12:off + 16], "little")
        assert start + length <= len(raw), f"app.ico entry {i} runs past the end of the file"
        assert raw[start:start + 4] == b"\x89PNG", f"app.ico entry {i} is not the PNG it claims to be"
    assert (CODE / "assets/branding/app.icns").read_bytes()[:4] == b"icns", "app.icns malformed"
    assert (CODE / "assets/branding/icon-256.png").read_bytes()[:4] == b"\x89PNG", "icon-256.png malformed"

    # The shortcuts launch Electron directly, so out/ must exist by then: the
    # installers have to run the build, which `npm run start` never does.
    pkg = json.loads((CODE / "app/package.json").read_text(encoding="utf-8"))
    assert pkg["main"] == "out/main/index.js", "shortcut target assumes out/main/index.js"
    assert "build" in pkg["scripts"], "no build script, but preview only serves an existing out/"
    steps = (ROOT / "tools/installer/windows/Steps.ps1").read_text(encoding="utf-8")
    assert "node_modules\\electron\\dist\\electron.exe" in steps, "Windows shortcut target changed"
    assert "'run', 'build'" in steps, "the Windows installer never builds out/"
    posix_main = (ROOT / "tools/installer/posix/install.sh").read_text(encoding="utf-8")
    assert "run build" in posix_main, "the posix installer never builds out/"
    shortcuts = (ROOT / "tools/installer/posix/shortcuts.sh").read_text(encoding="utf-8")
    assert "Electron.app/Contents/MacOS/Electron" in shortcuts, "mac launch path missing"

    # Window icon wired in, and both sides naming the SAME AppUserModelID -
    # if they drift, a pinned shortcut and the live window become two buttons.
    tabs_src = (CODE / "app/src/main/tabs.ts").read_text(encoding="utf-8")
    assert "app.ico" in tabs_src and "icon-256.png" in tabs_src, \
        "tabs.ts no longer resolves a window icon"
    index_src = (CODE / "app/src/main/index.ts").read_text(encoding="utf-8")
    assert "com.grindstone.app" in index_src, "main never sets the AppUserModelID"
    assert "com.grindstone.app" in steps, \
        "installer and app disagree on the AppUserModelID - pinning would show two taskbar buttons"


@check("frontend: sources present; typecheck when toolchain available")
def _frontend():
    app_dir = CODE / "app"
    for rel in ("package.json", "electron.vite.config.ts",
                "src/main/index.ts", "src/main/sidecar.ts", "src/main/api.ts",
                "src/main/tabs.ts", "src/preload/index.ts",
                "src/renderer/index.html", "src/renderer/src/App.tsx"):
        assert (app_dir / rel).exists(), f"missing app source {rel}"
    pkg = json.loads((app_dir / "package.json").read_text(encoding="utf-8"))
    assert pkg["name"] == "grindstone"

    # Dependencies were never installed, so a typecheck is genuinely impossible.
    # This is the ONLY honest reason to skip: both installers run `npm install`
    # before the gate, so a real install never lands here.
    tsc = app_dir / "node_modules" / "typescript" / "bin" / "tsc"
    if not tsc.exists():
        print("      (node_modules absent — file checks only; npm install enables the typecheck)")
        return

    # Resolve node the way every machine can: PATH first. This previously looked
    # ONLY at <workspace>/runtimes/node/node.exe — three levels ABOVE the repo —
    # so the typecheck silently no-opped on every clone except the one machine
    # that happened to have that folder, and being `.exe` it could never run on
    # Linux or macOS. It still counted `ok`, which made the gate report coverage
    # it did not have. The portable copy stays as a fallback, now cross-platform.
    exe = shutil.which("node")
    if not exe:
        portable = ROOT.parent.parent / "runtimes" / "node"
        cands = [portable / "node.exe", portable / "node"]
        # install.sh unpacks a private Node here and deliberately does NOT
        # persist it to PATH (the app launches Electron directly and never
        # needs node again). Without this, every standalone gate run after a
        # Linux install — checkpoint.py included — would fail the assert below
        # on a machine where the toolchain is in fact sitting right there.
        cands += sorted((Path.home() / ".local/share/grindstone").glob("node-*/bin/node"),
                        reverse=True)
        for cand in cands:
            if cand.exists():
                exe = str(cand)
                break
    # typescript is installed, so the toolchain was meant to be here. Skipping
    # now would recreate exactly the false green this check exists to avoid.
    assert exe, ("typescript is installed but no node runtime is on PATH — "
                 "the typecheck would be skipped while still reporting ok")
    r = subprocess.run([exe, str(tsc), "--noEmit"], cwd=app_dir,
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, f"tsc failed:\n{(r.stdout or r.stderr)[:1500]}"


def _node_exe() -> str | None:
    """Same resolution order as _frontend's. Kept separate on purpose for now:
    merging them means touching the check that closed the typecheck false
    green, and this one is new."""
    exe = shutil.which("node")
    if exe:
        return exe
    portable = ROOT.parent.parent / "runtimes" / "node"
    cands = [portable / "node.exe", portable / "node"]
    cands += sorted((Path.home() / ".local/share/grindstone").glob("node-*/bin/node"),
                    reverse=True)
    for cand in cands:
        if cand.exists():
            return str(cand)
    return None


@check("chart time is derived from candles: same span, same slope, any timeframe")
def _chart_time():
    """The property Kade asked for: "30 candles of 5 min and 15 candles of
    10 min - our slope is the same". This RUNS the engine's own arithmetic
    rather than restating it.

    Every earlier draft of this check was worthless, and each failure mode is
    worth naming because they all look green:
      - `slopePerHour(10,30,5) == slopePerHour(10,15,10)` hand-feeds both
        operands, so it is the statement 30*5 == 15*10 and cannot fail whatever
        TF_MINUTES holds.
      - greping the source for `chartMinutes` is satisfied by a comment.
      - asserting `'86400' not in the file` goes RED on a correct tree, because
        fmtSpan legitimately uses it twice.
    So: build real candle arrays, call the real methods, compare real numbers.
    Only ChartDraw's CONSTRUCTOR touches the DOM, so the methods run fine on a
    plain object via .call() with no jsdom and no bundler.
    """
    app_dir = CODE / "app"
    if not (app_dir / "node_modules" / "typescript").exists():
        print("      (node_modules absent — npm install enables the chart-time check)")
        return
    exe = _node_exe()
    assert exe, "no node runtime on PATH — the chart-time arithmetic cannot be run"
    ver = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30)
    m = re.match(r"v(\d+)\.(\d+)", ver.stdout.strip())
    assert m, f"could not read node version: {ver.stdout!r}"
    major, minor = int(m.group(1)), int(m.group(2))
    # Importing a .ts file directly needs type stripping, on by default from
    # 22.18. The installers pin 22.20 so a real install is fine; an older node
    # gets an honest message instead of a mystery failure.
    assert (major, minor) >= (22, 18), (
        f"node {major}.{minor} cannot import TypeScript directly (need >= 22.18). "
        f"The installers pin v22.20; update node or rerun the installer.")

    probe = r"""
import { TF_MINUTES, slopePerHour, ChartDraw, chartMinutePrefix, barIndexOf }
  from './src/renderer/src/components/ChartDraw.ts'
const out = []
const ok = (name, cond, detail) => out.push({ name, cond: !!cond, detail })

// Candles at a fixed step, as ISO strings - the shape barsIdx() expects.
const mk = (startMs, stepMin, n) =>
  Array.from({ length: n }, (_, i) => ({ ts: new Date(startMs + i * stepMin * 60000).toISOString() }))
// The stub must INHERIT the prototype, not just borrow one method off it:
// chartMinutes calls this.barsIdx() and this.barMinutes(), so a plain object
// throws "this.barsIdx is not a function". Only the CONSTRUCTOR touches the
// DOM, so an object with the prototype and the two fields these methods read
// is a complete enough `this` to run real arithmetic on.
const stub = (key, bars) =>
  Object.assign(Object.create(ChartDraw.prototype), { key, barsOpt: () => bars })
const cm = (key, bars, a, b) => ChartDraw.prototype.chartMinutes.call(stub(key, bars), a, b)
const sec = (b) => Math.floor(Date.parse(b.ts) / 1000)

const T0 = Date.parse('2024-01-02T14:30:00Z')   // 09:30 ET

// THE PROPERTY: the same two moments, on two timeframes, agree.
const b5 = mk(T0, 5, 25)     // 24 steps x 5min  = 120 minutes
const b10 = mk(T0, 10, 13)   // 12 steps x 10min = 120 minutes
const b60 = mk(T0, 60, 3)    //  2 steps x 60min = 120 minutes
const m5 = cm('X|5Min', b5, sec(b5[0]), sec(b5[24]))
const m10 = cm('X|10Min', b10, sec(b10[0]), sec(b10[12]))
const m60 = cm('X|1Hour', b60, sec(b60[0]), sec(b60[2]))
ok('5Min span is 120 chart-minutes', m5 === 120, m5)
ok('1Hour span agrees with 5Min', m60 === m5, `${m60} vs ${m5}`)
ok('unknown timeframe degrades to null', m10 === null, m10)

// Kade's own example, in the timeframes this app actually ships:
// 30 candles of 5Min and 10 candles of 15Min are both 150 chart-minutes, so
// the SAME price move over them must give the SAME slope.
const k5 = mk(T0, 5, 31)     // 30 steps x 5  = 150
const k15 = mk(T0, 15, 11)   // 10 steps x 15 = 150
const c5 = cm('X|5Min', k5, sec(k5[0]), sec(k5[30]))
const c15 = cm('X|15Min', k15, sec(k15[0]), sec(k15[10]))
ok('30 candles of 5Min is 150 chart-minutes', c5 === 150, c5)
ok('10 candles of 15Min is the same span', c15 === c5, `${c15} vs ${c5}`)
ok('so the same move gives the same slope',
   slopePerHour(10, c5) === slopePerHour(10, c15), `${slopePerHour(10, c5)}`)

// OVERNIGHT COSTS ONE CANDLE, not the calendar gap. Two sessions of 5Min.
const day1 = mk(T0, 5, 3)
const day2 = mk(T0 + 24 * 3600 * 1000, 5, 3)
const both = [...day1, ...day2]
const across = cm('X|5Min', both, sec(both[0]), sec(both[5]))
ok('overnight break costs one candle', across === 25, across)   // 5 steps x 5

// SIGN: a slope has a direction.
const fwd = cm('X|5Min', b5, sec(b5[0]), sec(b5[24]))
const rev = cm('X|5Min', b5, sec(b5[24]), sec(b5[0]))
ok('reversed span is negated', rev === -fwd, `${rev} vs ${fwd}`)
ok('slope sign follows it', slopePerHour(10, rev) === -slopePerHour(10, fwd), '')
ok('zero span has no slope', slopePerHour(10, 0) === null, '')

// The constant that makes daily reconcile with intraday.
ok('1Day is one session, not a calendar day', TF_MINUTES['1Day'] === 390, TF_MINUTES['1Day'])
ok('every shipped timeframe has a duration',
   ['1Min', '5Min', '15Min', '1Hour', '1Day'].every((k) => TF_MINUTES[k] > 0), '')

// ---- the prefix sum that replaced the per-step loop ------------------------
// DIFFERENTIAL test against the original implementation, inlined here. That is
// the only honest way to check a refactor whose whole claim is "same numbers":
// re-deriving the expected values by hand would just be a second chance to make
// the same mistake.
const naiveMinutes = (times, per, ia, ib) => {
  const lo = Math.min(ia, ib), hi = Math.max(ia, ib)
  let mins = 0
  for (let i = lo; i < hi; i++) {
    const gap = (times[i + 1] - times[i]) / 60
    mins += gap > 0 && gap < per ? gap : per
  }
  return ib >= ia ? mins : -mins
}
// Built to BEND. On clean regular bars the prefix sum is exactly i*per, so a
// well-formed series cannot tell a correct implementation from one that ignores
// the gaps entirely. This one carries a 2-minute step (shorter than its candle
// -- a resampled or half-session artefact) and an overnight break.
const odd = [0, 300, 600, 720, 1020, 1020 + 17 * 3600, 1020 + 17 * 3600 + 300]
const P = chartMinutePrefix(odd, 5)
let agree = true
for (let a = 0; a < odd.length; a++)
  for (let b = 0; b < odd.length; b++)
    if (Math.abs(P[b] - P[a] - naiveMinutes(odd, 5, a, b)) > 1e-12) agree = false
ok('the prefix sum agrees with the loop it replaced, both directions', agree, JSON.stringify(P))
ok('a gap SHORTER than a candle costs its real length', P[3] - P[2] === 2, P[3] - P[2])
ok('an overnight gap still costs exactly one candle', P[5] - P[4] === 5, P[5] - P[4])
ok('the prefix starts at zero', P[0] === 0, P[0])
// The property the solver will rest on: on clean bars, chart time is exactly
// proportional to bar index, which is what makes a slope constraint linear.
const clean = Array.from({ length: 9 }, (_, i) => i * 300)
const Pc = chartMinutePrefix(clean, 5)
ok('on clean bars chart time is exactly proportional to bar index',
   Pc.every((v, i) => v === i * 5), JSON.stringify(Pc))

// The prefix sum belongs to the CANDLE LENGTH as much as to the bar array, and
// caching it on array identity alone is a live trap: setKey can change the
// timeframe while a page hands back the same array, leaving 1Hour minutes
// describing a 5Min chart. Hourly bars discriminate, because reading them as
// 5Min makes every 60-minute gap cost one 5-minute candle instead.
const shared = mk(T0, 60, 5)
const s2 = stub('X|1Hour', shared)
const asHour = ChartDraw.prototype.chartMinutes.call(s2, sec(shared[0]), sec(shared[4]))
s2.key = 'X|5Min' // SAME array, different candle length
const asFive = ChartDraw.prototype.chartMinutes.call(s2, sec(shared[0]), sec(shared[4]))
ok('the prefix sum follows a timeframe change on the same bar array',
   asHour === 240 && asFive === 20, `${asHour} then ${asFive}`)

// ---- barIndexOf reports instead of clamping --------------------------------
// nearestBarTime pins to the first/last bar and never says so. Fine for the
// handle it places; wrong for anything a constraint must satisfy.
ok('an index inside the lattice resolves', barIndexOf(3.4, 10).i === 3, JSON.stringify(barIndexOf(3.4, 10)))
const over = barIndexOf(128, 119)
ok('running off the end REPORTS, it does not clamp',
   over.side === 'after' && over.byBars === 10, JSON.stringify(over))
const under = barIndexOf(-4, 119)
ok('running off the start reports too',
   under.side === 'before' && under.byBars === 4, JSON.stringify(under))
ok('an empty lattice is not silently index 0',
   barIndexOf(0, 0).side === 'before', JSON.stringify(barIndexOf(0, 0)))

console.log(JSON.stringify(out))
"""
    probe_path = app_dir / ".selftest-charttime.mjs"
    try:
        probe_path.write_text(probe, encoding="utf-8")
        r = subprocess.run([exe, str(probe_path)], cwd=app_dir,
                           capture_output=True, text=True, timeout=180)
        assert r.returncode == 0, f"chart-time probe crashed:\n{(r.stderr or r.stdout)[:1500]}"
        results = json.loads(r.stdout.strip().splitlines()[-1])
    finally:
        probe_path.unlink(missing_ok=True)
    bad = [x for x in results if not x["cond"]]
    assert not bad, "chart-time arithmetic is wrong:\n" + "\n".join(
        f"  - {x['name']} (got {x['detail']})" for x in bad)
    assert len(results) >= 22, f"the probe lost assertions: only {len(results)} ran"


@check("chart objects persist: strict store, one vocabulary, engine round-trip")
def _chart_persistence():
    """Drawings survive a restart (NOTES D7), and the three ways that quietly
    fails are each covered here rather than reasoned about:

      1. The STORE takes garbage. A NaN price round-trips through Python's json
         happily and then makes the renderer's JSON.parse throw — one bad write
         takes out every chart for that key.
      2. The two VOCABULARIES drift. The validator enumerates the engine's
         kinds, so adding a DrawKind to ChartDraw.ts and not to chartobjects.py
         makes the new tool save-refused at runtime and nowhere else.
      3. The ENGINE loses the last edit. Draw, switch tab: the debounce timer
         dies with the engine unless setKey/destroy flush it. And a restored
         'dw1' collides with the fresh counter's first 'dw1', which makes two
         objects select and delete as one.
    """
    import tempfile

    sys.path.insert(0, str(CODE))
    from fastapi.testclient import TestClient

    from backend import chartobjects as co
    from backend.app import State, create_app

    KEY = "SPY|1Day|$"
    DOC = {
        "drawings": [
            {"id": "dw1", "kind": "trend", "points": [
                {"time": 1700000000, "price": 450.5},
                {"time": 1700086400, "price": 455.0}]},
            {"id": "dw2", "kind": "hline", "points": [{"time": 1700000000, "price": 460}]},
        ],
        "measures": [
            {"id": "ms3",
             "a": {"kind": "candle", "time": 1700000000},
             "b": {"kind": "line", "drawingId": "dw1", "u": 0.5,
                   "time": 1700043200, "price": 452.7},
             "place": {"axis": "time", "at": 1700020000}},
            # A free diagonal: no place at all. JSON has no 'undefined', so the
            # renderer sends null and the store must read that as "absent".
            {"id": "ms4",
             "a": {"kind": "free", "time": 1700000000, "price": 1.0},
             "b": {"kind": "free", "time": 1700086400, "price": 2.0},
             "place": None},
        ],
        "pins": [{"id": "pin5", "time": 1700000000}],
    }

    # -- 1. the routes, end to end ------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        state = State("boot-token-for-tests", db_path=Path(tmp) / "app.db")
        client = TestClient(create_app(state), base_url="http://127.0.0.1")
        B = {"X-App-Token": "boot-token-for-tests"}
        token = client.post("/api/auth/setup", headers=B,
                            json={"username": "t", "password": "longenough1"}).json()["token"]
        A = {**B, "Authorization": f"Bearer {token}"}

        assert client.get("/api/chart-objects", headers=B,
                          params={"key": KEY}).status_code == 401, \
            "chart objects are readable without a session"

        r = client.get("/api/chart-objects", headers=A, params={"key": KEY})
        assert r.status_code == 200 and r.json()["doc"]["drawings"] == [], \
            f"an undrawn chart should read empty, got {r.text[:200]}"

        r = client.put("/api/chart-objects", headers=A, json={"key": KEY, "doc": DOC})
        assert r.status_code == 200, r.text
        back = client.get("/api/chart-objects", headers=A, params={"key": KEY}).json()["doc"]
        assert back == r.json()["doc"], "a saved chart did not read back identically"
        assert len(back["drawings"]) == 2 and len(back["measures"]) == 2, back
        assert "place" not in back["measures"][1], \
            "a null place must round-trip as ABSENT — a free diagonal is not axis-locked"
        assert back["measures"][0]["place"] == {"axis": "time", "at": 1700020000}

        # The key is a query parameter because '|' is not a legal path char.
        keys = client.get("/api/chart-objects/keys", headers=A).json()["charts"]
        assert [k["key"] for k in keys] == [KEY], keys

        # Clearing a chart removes the row, so 'keys' stays an honest answer to
        # "where did I draw something".
        client.put("/api/chart-objects", headers=A,
                   json={"key": KEY, "doc": {"drawings": [], "measures": [], "pins": []}})
        assert client.get("/api/chart-objects/keys", headers=A).json()["charts"] == [], \
            "clearing a chart left an empty row behind"

        # Garbage is a 422 naming the reason, never a 500 and never a silent
        # substitution — a 'fixed' document is how a user's work disappears.
        bad_docs = {
            # What a JS client actually sends for a NaN: JSON.stringify(NaN)
            # is the string "null", not "NaN".
            "null price": {"drawings": [{"id": "d", "kind": "hline",
                                         "points": [{"time": 1, "price": None}]}]},
            "unknown kind": {"drawings": [{"id": "d", "kind": "fib", "points": []}]},
            "wrong point count": {"drawings": [{"id": "d", "kind": "trend",
                                                "points": [{"time": 1, "price": 2}]}]},
            "fractional bar time": {"pins": [{"id": "p", "time": 1.5}]},
            "unknown anchor": {"measures": [{"id": "m", "a": {"kind": "ghost"},
                                             "b": {"kind": "candle", "time": 1}}]},
            "duplicate id": {"drawings": [{"id": "x", "kind": "hline",
                                           "points": [{"time": 1, "price": 2}]}],
                             "pins": [{"id": "x", "time": 1}]},
            # Epoch 0 is a placeholder that escaped, never a bar. moveDimension
            # wrote exactly this and then returned early over the right-hand
            # whitespace, persisting a dimension pinned to 1970.
            "epoch-0 dimension": {"measures": [
                {"id": "m", "a": {"kind": "candle", "time": 1700000000},
                 "b": {"kind": "candle", "time": 1700086400},
                 "place": {"axis": "time", "at": 0}}]},
            "negative bar time": {"pins": [{"id": "p", "time": -5}]},
        }
        for name, doc in bad_docs.items():
            r = client.put("/api/chart-objects", headers=A, json={"key": KEY, "doc": doc})
            assert r.status_code == 422, f"{name}: expected 422, got {r.status_code} {r.text[:160]}"
        assert client.put("/api/chart-objects", headers=A,
                          json={"key": "x" * 500, "doc": {}}).status_code == 422, \
            "an unbounded chart key was accepted"

        # NaN/Infinity have to be posted as RAW bytes: they are not legal JSON,
        # so no compliant client encoder will emit them — but Python's json
        # module PARSES them, which is exactly how one reaches the validator and
        # then the database. A stored NaN makes the renderer's JSON.parse throw
        # and takes out every chart for that key.
        raw = ('{"key": "' + KEY + '", "doc": {"drawings": [{"id": "d", "kind": '
               '"hline", "points": [{"time": 1, "price": NaN}]}]}}')
        r = client.put("/api/chart-objects", headers={**A, "Content-Type": "application/json"},
                       content=raw)
        assert r.status_code == 422, f"a NaN price was accepted: {r.status_code} {r.text[:160]}"

        # A corrupt row degrades to blank rather than crashing every chart.
        with state.db() as db:
            db.execute("INSERT INTO chart_objects (user_id, key, doc) VALUES (1,?,?)",
                       ("BAD|1Day|$", "{not json"))
        r = client.get("/api/chart-objects", headers=A, params={"key": "BAD|1Day|$"})
        assert r.status_code == 200 and r.json()["doc"]["drawings"] == [], \
            "a corrupt row must read as empty, not 500"

    # -- 2. one vocabulary, two files ---------------------------------------
    draw_src = (CODE / "app/src/renderer/src/components/ChartDraw.ts").read_text(
        encoding="utf-8")

    def _block(pattern: str, what: str) -> str:
        m = re.search(pattern, draw_src, re.S)
        assert m, f"ChartDraw.ts no longer declares {what} — this check cannot see drift"
        return m.group(1)

    kinds = set(re.findall(r"'(\w+)'", _block(r"export type DrawKind =([^\n]+)", "DrawKind")))
    anchors = set(re.findall(
        r"kind: '(\w+)'", _block(r"export type MeasureAnchor =(.*?)\n\n", "MeasureAnchor")))
    axes = set(re.findall(
        r"axis: '(\w+)'", _block(r"export type MeasurePlace =(.*?)\n\n", "MeasurePlace")))

    assert kinds == set(co.DRAW_KINDS), (
        f"drawing vocabulary drifted: engine {sorted(kinds)} vs "
        f"backend {sorted(co.DRAW_KINDS)} — the new kind cannot be saved")
    assert anchors == set(co.ANCHOR_KINDS), (
        f"anchor vocabulary drifted: engine {sorted(anchors)} vs "
        f"backend {sorted(co.ANCHOR_KINDS)}")
    assert axes == set(co.PLACE_AXES), (
        f"dimension axes drifted: engine {sorted(axes)} vs backend {sorted(co.PLACE_AXES)}")

    ckinds = set(re.findall(
        r"'(\w+)'", _block(r"export type ConstraintKind =([^\n]+)", "ConstraintKind")))
    parts = set(re.findall(
        r"'(\w+)'", _block(r"export type EntityPart =([^\n]+)", "EntityPart")))
    assert ckinds == set(co.CONSTRAINT_KINDS), (
        f"constraint vocabulary drifted: engine {sorted(ckinds)} vs backend "
        f"{sorted(co.CONSTRAINT_KINDS)} — the backend would accept a constraint "
        "the engine ignores, or refuse one it can honour")
    assert parts == set(co.ENTITY_PARTS), (
        f"entity parts drifted: engine {sorted(parts)} vs backend {sorted(co.ENTITY_PARTS)}")
    assert set(co.POINTS_FOR) == set(co.DRAW_KINDS), \
        "a DrawKind has no declared point count in chartobjects.POINTS_FOR"

    m = re.search(r"export const CHART_DOC_VERSION = (\d+)", draw_src)
    assert m and int(m.group(1)) == co.DOC_VERSION, (
        f"doc version drifted: engine {m and m.group(1)} vs backend {co.DOC_VERSION} "
        "— every saved chart would read back blank")

    # The engine must not import the API: the checks above and _chart_time run
    # it under plain node, and the window.grindstone bridge does not exist there.
    assert "from '../api'" not in draw_src and 'from "../api"' not in draw_src, \
        "ChartDraw.ts imports the API — persistence is INJECTED (chartStore.ts) so " \
        "the engine stays runnable outside a browser"
    for page in ("ChartsPage.tsx", "SymbolPage.tsx"):
        src = (CODE / "app/src/renderer/src/pages" / page).read_text(encoding="utf-8")
        assert "makeChartStore" in src and "store: chartStore" in src, \
            f"{page} builds an engine with no store — its drawings die with the process"
        assert "draw-save-err" in src, \
            f"{page} cannot report a failed save, so lost drawings would be silent"

    # -- 3. the engine's own load/save behaviour ----------------------------
    app_dir = CODE / "app"
    if not (app_dir / "node_modules" / "typescript").exists():
        print("      (node_modules absent — npm install enables the engine probe)")
        return
    exe = _node_exe()
    assert exe, "no node runtime on PATH — the persistence round-trip cannot be run"

    probe = r"""
import { ChartDraw, CHART_DOC_VERSION } from './src/renderer/src/components/ChartDraw.ts'
const out = []
const ok = (name, cond, detail) => out.push({ name, cond: !!cond, detail })
const settle = (ms = 0) => new Promise((r) => setTimeout(r, ms))

// A store that records every call, so the assertions are about what the engine
// ASKED FOR rather than about a mock's internal state.
const mkStore = (docs, delayMs = 0) => ({
  loads: [], saves: [],
  load(key) {
    this.loads.push(key)
    return new Promise((r) => setTimeout(() => r(docs[key] ?? null), delayMs))
  },
  save(key, doc) {
    this.saves.push({ key, doc: JSON.parse(JSON.stringify(doc)) })
    return Promise.resolve()
  },
})

// Only the CONSTRUCTOR touches the DOM (see _chart_time). An object with the
// prototype plus the fields these methods read is a complete enough `this`;
// render/applyCursor are own properties, which shadow the DOM-touching ones.
const mkEngine = (key, store) =>
  Object.assign(Object.create(ChartDraw.prototype), {
    key, store, saveTimer: null, destroyed: false, changeCb: null,
    tool: 'pointer', selected: [], hidden: false,
    barsOpt: () => [],
    series: { coordinateToPrice: (y) => 100 + y, priceToCoordinate: (p) => p - 100 },
    teardown: [], host: { style: {} }, svg: { remove() {} }, labels: { remove() {} },
    render() {}, applyCursor() {},
  })

// The ids a real FIRST session produces: mkId's counter starts at 1, so these
// are exactly what a user's first three objects are called. Using high ids
// here (dw7/ms8/pin9) makes the collision assertion below unfailable -- the
// fresh counter would mint 'dw1', which collides with nothing in that set.
// That false green is what this comment exists to stop coming back.
const RESTORED = {
  version: 1,
  drawings: [{ id: 'dw1', kind: 'trend',
               points: [{ time: 1700000000, price: 1 }, { time: 1700086400, price: 2 }] }],
  measures: [{ id: 'ms2', a: { kind: 'candle', time: 1700000000 },
               b: { kind: 'candle', time: 1700086400 } }],
  pins: [{ id: 'pin3', time: 1700000000 }],
}

const store = mkStore({ 'SPY|1Day': RESTORED })
const e = mkEngine('SPY|1Day', store)
e.hydrate()
await settle(5)

let s = e.getState()
ok('a saved chart comes back', s.drawings === 1, s.drawings)
ok('measures and pins come back too', s.measures === 2, s.measures)
ok('hydrating is not itself a write', store.saves.length === 0, store.saves.length)

// THE ID COLLISION: a fresh module counter starts at 1, so without adoptIds
// the first new drawing is called 'dw1' -- the name a restored drawing already
// has. The selection is a flat string[] matched by id, so the two would
// select, drag and delete as one object.
const restoredIds = new Set(['dw1', 'ms2', 'pin3'])
e.clickHline(10, 20, 1700000000)
const ids = e.bucket().drawings.map((d) => d.id)
ok('a new object cannot reuse a restored id',
   ids.length === 2 && !restoredIds.has(ids[1]), ids.join(','))
// The invariant itself, independent of which ids the stored doc happened to
// use: no id may appear twice anywhere in the bucket.
const b0 = e.bucket()
const allIds = [...b0.drawings, ...b0.measures, ...b0.pins].map((o) => o.id)
ok('every id in a restored chart is unique',
   new Set(allIds).size === allIds.length, allIds.join(','))

e.flushSave()
ok('an edit is written', store.saves.length === 1, store.saves.length)
const wrote = store.saves[0]
ok('it writes the key it edited', wrote.key === 'SPY|1Day', wrote.key)
ok('the written doc holds both drawings', wrote.doc.drawings.length === 2,
   wrote.doc.drawings.length)
ok('the doc is stamped with a version', wrote.doc.version === CHART_DOC_VERSION,
   wrote.doc.version)
// What you drew, not how you are looking at it.
ok('no ui state is persisted',
   !('tool' in wrote.doc) && !('selected' in wrote.doc) && !('hidden' in wrote.doc),
   Object.keys(wrote.doc).join(','))

// Every mutator emits, including selection. Writing on each would be a request
// per click.
e.selected = [ids[0]]
e.emit()
e.flushSave()
ok('selecting something is not a change worth saving', store.saves.length === 1,
   store.saves.length)

// THE TAB SWITCH: the debounce timer is cancelled by the key change, so
// without a flush the last drawing before the switch is simply gone.
e.clickHline(10, 30, 1700000000)
e.setKey('SPY|1Hour')
ok('switching charts flushes the previous one',
   store.saves.length === 2 && store.saves[1].key === 'SPY|1Day',
   store.saves.map((x) => x.key).join(','))
ok('and the new key is hydrated', store.loads.includes('SPY|1Hour'), store.loads.join(','))

// THE UNMOUNT: same failure, different exit.
const e2 = mkEngine('QQQ|1Day', store)
e2.hydrate()
await settle(5)
e2.clickHline(10, 20, 1700000000)
const before = store.saves.length
e2.destroy()
ok('destroy writes the pending edit',
   store.saves.length === before + 1 && store.saves.at(-1).key === 'QQQ|1Day',
   store.saves.at(-1).key)

// A revisit inside the session reads memory: the bucket is NEWER than the
// server whenever a save is still debouncing.
const e3 = mkEngine('SPY|1Day', store)
e3.hydrate()
await settle(5)
ok('a key is loaded at most once per session',
   store.loads.filter((k) => k === 'SPY|1Day').length === 1, store.loads.join(','))
ok('and the revisit still sees the objects', e3.getState().drawings === 3,
   e3.getState().drawings)

// THE RACE: a slow load that lands after the user has already drawn must not
// replace their work with the older server copy.
const slow = mkStore({ 'IWM|1Day': {
  version: 1, drawings: [{ id: 'dwX', kind: 'hline',
                           points: [{ time: 1700000000, price: 5 }] }],
  measures: [], pins: [] } }, 30)
const e4 = mkEngine('IWM|1Day', slow)
e4.hydrate()
e4.clickHline(10, 20, 1700000000)   // drawn while the request is in flight
await settle(80)
const raced = e4.bucket().drawings.map((d) => d.id)
ok('an in-flight load never clobbers a fresh drawing',
   raced.length === 1 && !raced.includes('dwX'), raced.join(','))

console.log(JSON.stringify(out))
"""
    probe_path = app_dir / ".selftest-chartpersist.mjs"
    try:
        probe_path.write_text(probe, encoding="utf-8")
        r = subprocess.run([exe, str(probe_path)], cwd=app_dir,
                           capture_output=True, text=True, timeout=180)
        assert r.returncode == 0, f"persistence probe crashed:\n{(r.stderr or r.stdout)[:1500]}"
        results = json.loads(r.stdout.strip().splitlines()[-1])
    finally:
        probe_path.unlink(missing_ok=True)
    bad = [x for x in results if not x["cond"]]
    assert not bad, "the drawing engine's persistence is wrong:\n" + "\n".join(
        f"  - {x['name']} (got {x['detail']})" for x in bad)
    assert len(results) >= 16, f"the probe lost assertions: only {len(results)} ran"


@check("chart constraints: lock removes DOF exactly, and says why it will not move")
def _chart_constraints():
    """Stage 2 of the sketch-constraint work: `lock`, and nothing that needs a
    solver. The claims worth pinning down are all about EXACTNESS and HONESTY:

      - DOF is counted, not estimated. A handle coordinate must never enter the
        count — an hline's time and a vline's price only place the grab handle,
        and if they were variables nothing could ever reach 'fully defined' and
        the whole status signal would be dead.
      - A second lock on one coordinate adds no information, and is refused by
        an O(1) occupancy test rather than discovered numerically. Refusing it
        matters twice over: DOF stays exact, and the user is told.
      - A locked drawing does not move, and the refusal reaches the page. A
        lock that let a drag start and then snapped back would read as jitter.
      - A constraint naming a deleted drawing DANGLES. It is invisible, still
        counts against DOF, and no other collection has this failure mode.
    """
    import tempfile

    sys.path.insert(0, str(CODE))
    from fastapi.testclient import TestClient

    from backend import chartobjects as co
    from backend.app import State, create_app

    KEY = "SPY|1Day"

    # -- 1. the store keeps constraints, and refuses a dangling one ----------
    with tempfile.TemporaryDirectory() as tmp:
        state = State("boot-token-for-tests", db_path=Path(tmp) / "app.db")
        client = TestClient(create_app(state), base_url="http://127.0.0.1")
        B = {"X-App-Token": "boot-token-for-tests"}
        token = client.post("/api/auth/setup", headers=B,
                            json={"username": "t", "password": "longenough1"}).json()["token"]
        A = {**B, "Authorization": f"Bearer {token}"}

        doc = {
            "drawings": [
                {"id": "dw1", "kind": "hline", "points": [{"time": 1700000000, "price": 600}]},
                {"id": "dw2", "kind": "trend", "points": [
                    {"time": 1700000000, "price": 600}, {"time": 1700086400, "price": 604}]},
            ],
            "constraints": [{"id": "cn3", "kind": "lock", "a": {"id": "dw1", "part": "line"}}],
        }
        r = client.put("/api/chart-objects", headers=A, json={"key": KEY, "doc": doc})
        assert r.status_code == 200, r.text
        back = client.get("/api/chart-objects", headers=A, params={"key": KEY}).json()["doc"]
        assert back["constraints"] == [
            {"id": "cn3", "kind": "lock", "a": {"id": "dw1", "part": "line"}}], back
        assert len(back["drawings"]) == 2

        # NOT asserted here: "a constraints-only document is not empty". It is
        # unreachable — a constraint must name a drawing in the same document,
        # so constraints imply drawings and the assertion would pass whatever
        # is_empty did with them. The rule that makes it unreachable is the
        # dangling-reference refusal below, which IS tested.
        assert not co.is_empty(back)

        bad = {
            "dangling reference": {**doc, "constraints": [
                {"id": "cn4", "kind": "lock", "a": {"id": "nope", "part": "line"}}]},
            "unknown constraint kind": {**doc, "constraints": [
                {"id": "cn4", "kind": "parallel", "a": {"id": "dw1", "part": "line"}}]},
            "unknown entity part": {**doc, "constraints": [
                {"id": "cn4", "kind": "lock", "a": {"id": "dw1", "part": "middle"}}]},
            "constraint id collides with a drawing": {**doc, "constraints": [
                {"id": "dw1", "kind": "lock", "a": {"id": "dw1", "part": "line"}}]},
        }
        for name, d in bad.items():
            rr = client.put("/api/chart-objects", headers=A, json={"key": KEY, "doc": d})
            assert rr.status_code == 422, f"{name}: expected 422, got {rr.status_code} {rr.text[:160]}"

    # -- 2. the seams that must agree, or the feature is invisible -----------
    # A refusal the engine computes and no page renders is the same as no
    # refusal at all, and that is the specific failure this feature cannot have.
    for page in ("ChartsPage.tsx", "SymbolPage.tsx"):
        src = (CODE / "app/src/renderer/src/pages" / page).read_text(encoding="utf-8")
        assert "drawState?.issue" in src, \
            f"{page} never renders the engine's issue — a refused lock would be silent"
        assert "data-draw-issue" in src, f"{page}: the issue is not testable from the DOM"
        assert "dofFree" in src, f"{page} does not report degrees of freedom to the chart"
    chart = (CODE / "app/src/renderer/src/components/Chart.tsx").read_text(encoding="utf-8")
    assert "data-draw-dof" in chart, "Chart.tsx exposes no DOF attribute for e2e to read"
    draw_src = (CODE / "app/src/renderer/src/components/ChartDraw.ts").read_text(encoding="utf-8")
    # pruneConstraints must hang off commit(), not off each deletion path: trim
    # replaces a drawing with new ones and would otherwise leave a lock behind.
    assert "this.pruneConstraints(this.bucket())" in draw_src, \
        "commit() no longer prunes dangling constraints — trim and delete would leave them"

    # -- 3. the engine's own arithmetic and behaviour ------------------------
    app_dir = CODE / "app"
    if not (app_dir / "node_modules" / "typescript").exists():
        print("      (node_modules absent — npm install enables the constraint probe)")
        return
    exe = _node_exe()
    assert exe, "no node runtime on PATH — the constraint arithmetic cannot be run"

    probe = r"""
import { ChartDraw, slotsOf, degreesOfFreedom }
  from './src/renderer/src/components/ChartDraw.ts'
const out = []
const ok = (name, cond, detail) => out.push({ name, cond: !!cond, detail })

const hline = (id, price) => ({ id, kind: 'hline', points: [{ time: 1700000000, price }] })
const vline = (id) => ({ id, kind: 'vline', points: [{ time: 1700000000, price: 1 }] })
const trend = (id) => ({ id, kind: 'trend',
  points: [{ time: 1700000000, price: 600 }, { time: 1700086400, price: 604 }] })
const circle = (id) => ({ id, kind: 'circle',
  points: [{ time: 1700000000, price: 600 }, { time: 1700086400, price: 604 }] })
const lock = (id, on, part = 'line') => ({ id, kind: 'lock', a: { id: on, part } })

// A HANDLE IS NOT A VARIABLE. An hline is one price; its time only places the
// grab handle. If handles counted, every hline would carry a permanently free
// DOF and nothing could ever read 'fully defined'.
ok('an hline owns exactly one coordinate', slotsOf(hline('dw1', 600)).length === 1,
   JSON.stringify(slotsOf(hline('dw1', 600))))
ok('a vline owns exactly one', slotsOf(vline('dw2')).length === 1, JSON.stringify(slotsOf(vline('dw2'))))
ok('a trend owns four', slotsOf(trend('dw3')).length === 4, JSON.stringify(slotsOf(trend('dw3'))))
ok('a circle owns none — the plane is not isotropic', slotsOf(circle('dw4')).length === 0, '')
ok('an hline holds its PRICE, not its time', slotsOf(hline('dw1', 600))[0].endsWith(':p'),
   slotsOf(hline('dw1', 600))[0])
ok('a vline holds its TIME', slotsOf(vline('dw2'))[0].endsWith(':i'), slotsOf(vline('dw2'))[0])

// Kade's scene: two h-lines and a diagonal = 1 + 1 + 4 = 6 free coordinates.
const scene = [hline('dw1', 600), hline('dw2', 604), trend('dw3')]
ok('the two-hlines-and-a-diagonal scene has 6 coordinates',
   degreesOfFreedom(scene, []).total === 6, degreesOfFreedom(scene, []).total)
ok('with nothing locked, all 6 are free', degreesOfFreedom(scene, []).free === 6, '')
ok('locking one h-line removes exactly one',
   degreesOfFreedom(scene, [lock('cn1', 'dw1')]).free === 5,
   degreesOfFreedom(scene, [lock('cn1', 'dw1')]).free)
ok('locking the whole diagonal removes four',
   degreesOfFreedom(scene, [lock('cn1', 'dw3')]).free === 2,
   degreesOfFreedom(scene, [lock('cn1', 'dw3')]).free)
ok('locking ONE endpoint removes two',
   degreesOfFreedom(scene, [lock('cn1', 'dw3', 'a')]).free === 4,
   degreesOfFreedom(scene, [lock('cn1', 'dw3', 'a')]).free)
// THE OCCUPANCY RULE, at the arithmetic level: a repeated lock adds nothing, so
// counting rows instead of SLOTS would report a DOF that is simply wrong.
ok('a duplicated lock does not remove a second DOF',
   degreesOfFreedom(scene, [lock('cn1', 'dw1'), lock('cn2', 'dw1')]).free === 5,
   degreesOfFreedom(scene, [lock('cn1', 'dw1'), lock('cn2', 'dw1')]).free)
ok('a lock on a missing drawing removes nothing',
   degreesOfFreedom(scene, [lock('cn1', 'ghost')]).free === 6, '')
ok('a lock on a circle removes nothing',
   degreesOfFreedom([circle('dw9')], [lock('cn1', 'dw9')]).free === 0, '')

// ---- the engine surface ---------------------------------------------------
// Only the constructor touches the DOM (see _chart_time): the prototype plus
// the fields these methods read is a complete enough `this`.
const mkEngine = (key) => Object.assign(Object.create(ChartDraw.prototype), {
  key, saveTimer: null, destroyed: false, changeCb: null, issue: null,
  tool: 'pointer', selected: [], hidden: false, barsOpt: () => [],
  render() {}, applyCursor() {},
})

const e = mkEngine('LOCK|1Day')
const b = e.bucket()
b.drawings.push(hline('dw1', 600), trend('dw3'))

e.selected = ['dw1']
e.lockSelected()
ok('locking the selection adds one constraint', b.constraints.length === 1,
   JSON.stringify(b.constraints))
ok('and the state reports the DOF', e.getState().dof.free === 4, JSON.stringify(e.getState().dof))
ok('and names what is locked', e.getState().lockedIds.join() === 'dw1', e.getState().lockedIds.join())

// Locking twice is refused WITH A REASON, not silently ignored.
e.lockSelected()
ok('a second lock on the same thing is refused', b.constraints.length === 1, b.constraints.length)
ok('and the refusal reaches the page in words',
   e.getState().issue && e.getState().issue.code === 'duplicate',
   JSON.stringify(e.getState().issue))

// A dangling constraint is pruned by commit(), which every deletion path ends
// in — including trim, which replaces a drawing with new ones.
b.drawings = b.drawings.filter((d) => d.id !== 'dw1')
e.commit()
ok('deleting the drawing takes its lock with it', b.constraints.length === 0,
   JSON.stringify(b.constraints))
ok('so the DOF returns to the diagonal alone', e.getState().dof.free === 4,
   JSON.stringify(e.getState().dof))

// Unlock is the inverse, and it is what the blocked-drag notice points at.
e.selected = ['dw3']
e.lockSelected()
ok('the diagonal locks whole', e.getState().dof.free === 0, JSON.stringify(e.getState().dof))

// THE BLOCKED DRAG. This is the rule the whole feature rests on: a locked
// drawing must be refused BEFORE the gesture starts, or it reads as jitter
// rather than as a constraint.
const blocked = e.movableIds(['dw3'])
ok('a locked drawing cannot be dragged', blocked.ids.length === 0, JSON.stringify(blocked.ids))
ok('and the refusal names it in words',
   blocked.issue && blocked.issue.code === 'blocked' && /locked/i.test(blocked.issue.message),
   JSON.stringify(blocked.issue))
// A mixed selection still moves what it can, rather than refusing wholesale.
b.drawings.push(hline('dw7', 610))
const partly = e.movableIds(['dw3', 'dw7'])
ok('a mixed selection moves the unlocked part',
   partly.ids.join() === 'dw7' && partly.issue === null, JSON.stringify(partly))

e.unlockSelected()
ok('unlock releases it', e.getState().dof.free === 5 && b.constraints.length === 0,
   JSON.stringify(e.getState().dof))
ok('and clears the notice', e.getState().issue === null, JSON.stringify(e.getState().issue))
const free = e.movableIds(['dw3'])
ok('and the drag is permitted again', free.ids.join() === 'dw3' && free.issue === null,
   JSON.stringify(free))

console.log(JSON.stringify(out))
"""
    probe_path = app_dir / ".selftest-constraints.mjs"
    try:
        probe_path.write_text(probe, encoding="utf-8")
        r = subprocess.run([exe, str(probe_path)], cwd=app_dir,
                           capture_output=True, text=True, timeout=180)
        assert r.returncode == 0, f"constraint probe crashed:\n{(r.stderr or r.stdout)[:1500]}"
        results = json.loads(r.stdout.strip().splitlines()[-1])
    finally:
        probe_path.unlink(missing_ok=True)
    bad_r = [x for x in results if not x["cond"]]
    assert not bad_r, "the constraint model is wrong:\n" + "\n".join(
        f"  - {x['name']} (got {x['detail']})" for x in bad_r)
    assert len(results) >= 22, f"the probe lost assertions: only {len(results)} ran"


def main() -> int:
    passed = 0
    total = len(CHECKS)

    # Two @check decorators stacked on one function register two NAMES against
    # one BODY: the count still adds up, every line still prints ok, and the
    # check whose decorator drifted silently stops running. That happened to
    # the bt engine suite (see _bt_engine). A name is only a real check if it
    # has a body of its own.
    seen: dict[object, str] = {}
    for name, fn in CHECKS:
        if fn in seen:
            print(f"FAIL  {name}: shares its body with {seen[fn]!r} — a @check "
                  f"decorator is stacked on the wrong function, so one of the "
                  f"two never runs")
            print(f"SELFTEST FAILED 0/{total}")
            return 1
        seen[fn] = name

    for name, fn in CHECKS:
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - report, don't crash the runner
            print(f"FAIL  {name}: {e}")
            print(f"SELFTEST FAILED {passed}/{total}")
            return 1
        passed += 1
        print(f"ok    {name}")
    print(f"SELFTEST OK {passed}/{total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
