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


def member_body(src: str, sig: str) -> str:
    """A class member's body, bounded by the NEXT member rather than by a
    character count.

    A fixed ``[:N]`` slice fails in both directions, and both have happened in
    this file: it goes RED on correct code once a comment grows past N (the note
    above `down` records that one), and it goes GREEN-but-blind when the string
    it asserts drifts in from the following method. The second is the dangerous
    one — `deleteSelected`'s 600-char window reaches into `clearDrawings`, which
    filters `b.drawings` too, so the assertion would pass with the body gutted.

    The boundary is the next declaration at the class's own two-space indent: a
    doc comment, a modifier, or a bare method name. Nothing inside a body sits
    at that indent except its closing brace, which is not a declaration.
    """
    parts = src.split(sig, 1)
    assert len(parts) > 1, f"member not found in source: {sig!r}"
    tail = parts[1]
    nxt = re.search(
        r"\n  (?:/\*\*|private |public |protected |static |readonly |get |set |[A-Za-z_$][\w$]*[(<])",
        tail,
    )
    return tail[: nxt.start()] if nxt else tail


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

        # -- "SPY Opt" reaches the workstation ---------------------------
        # The omnibox keeps anything containing a space as a SEARCH by its own
        # conservative rule, so this grammar is the only thing that makes
        # Kade's "SPY Opt" land somewhere. Same shape as the "<TICKER> news"
        # intent it sits beside.
        for phrase in ("SPY opt", "opt SPY", "SPY options", "spy chain"):
            r = search_mod.query(phrase, uni, con)
            assert r["intent"] == {"kind": "symbol-opt", "symbol": "SPY"}, (phrase, r["intent"])
            top = r["results"][0]
            assert top["action"] == "symbol-opt" and top["symbol"] == "SPY", (phrase, top)
            assert top["title"] == "SPY Opt", (phrase, top)
        # NEWS still wins where both words appear: "SPY options news" is a news
        # query about options, and the more specific reading must not be stolen.
        assert search_mod.query("SPY options news", uni, con)["intent"] == {
            "kind": "symbol-news", "symbol": "SPY"}, "the opt grammar swallowed a news query"
        # A bare word is not a destination — no ticker, no intent.
        assert search_mod.query("options", uni, con)["intent"] is None

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
    from backend.brokers import alpaca_data as ad
    from backend.brokers.alpaca_data import (AlpacaData, parse_bars,
                                             parse_chain_snapshot,
                                             parse_news_item, parse_occ,
                                             parse_stock_snapshot)
    from backend.brokers.base import BrokerError

    # ---- what a failure SAYS, which is half of what it is ------------------
    # 401 and 403 mean different things: rejected keys versus keys that work
    # but are not entitled to THIS feed. Lumping them sent a real debugging
    # session at the wrong half (the options chain 401'd while the account
    # test passed), so the distinction is pinned here.
    class _Resp:
        def __init__(self, code, payload):
            self.status_code = code
            self._payload = payload
            self.text = str(payload)

        def json(self):
            if isinstance(self._payload, dict):
                return self._payload
            raise ValueError("not json")

    real_get = ad.httpx.get
    try:
        for code, payload, want in (
            (401, {"message": "forbidden"}, "keys rejected (401)"),
            (403, {"message": "subscription does not permit"}, "not entitled"),
            (429, {"message": "too many"}, "rate limited (429)"),
            (500, "gateway blew up", "HTTP 500"),
        ):
            ad.httpx.get = lambda *a, _c=code, _p=payload, **k: _Resp(_c, _p)
            try:
                AlpacaData("k", "s").stock_snapshot("SPY")
                raise AssertionError(f"HTTP {code} did not raise")
            except BrokerError as e:
                assert want in str(e), f"HTTP {code} said {e!r}, wanted {want!r}"
                # Alpaca's own words reach the user, not just our status code.
                if isinstance(payload, dict):
                    assert payload["message"][:20] in str(e), \
                        f"upstream message dropped from {e!r}"
    finally:
        ad.httpx.get = real_get

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
    mv = member_body(draw, "private moveDragged(")
    assert "timeAtX" in mv and "priceAtY" in mv, \
        "moveDragged translates in DATA units again - the x axis is affine in " \
        "bar index, so a constant time delta is not a constant pixel delta " \
        "across a weekend and the drawing would drift from the cursor"
    # snapDrawTime, not nearestBarTime: the snap must still happen, but onto
    # the lattice EXTENDED past the last candle. nearestBarTime clamps a future
    # time back onto the last bar, which is what made it impossible to draw
    # where an option leg's expiration lives — see _chart_legs, which pins the
    # difference. The invariant here is unchanged: a dragged point lands on a
    # slot, never between them.
    assert "snapDrawTime" in mv, \
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
    endd = member_body(draw, "private endDrag(")
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

    # -- one doomed set sweeps all four collections -------------------------
    # The filters live in sweep() rather than inline, because three delete
    # paths need identical bookkeeping and three copies is how one of them ends
    # up missing an array. BOTH halves are asserted: that the sweep is complete,
    # AND that deleteSelected actually routes through it — pinning the sweep
    # alone would pass just as happily on a sweep nobody calls.
    seg = member_body(draw, "private sweep(")
    for arr in ("b.drawings", "b.measures", "b.pins", "b.legs"):
        assert f"{arr} = {arr}.filter" in seg, \
            f"sweep no longer removes {arr} - that is the whole bug"
    dsel = member_body(draw, "deleteSelected(): void")
    assert "this.sweep(" in dsel and "cascadeDoomed" in dsel, \
        "deleteSelected stopped routing through cascadeDoomed+sweep, so deleting " \
        "a leg leaves its four bounding lines orphaned on the chart again"

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
        # Closed in the finally: on Windows an open handle makes the tempdir
        # cleanup raise WinError 32 as this check unwinds, REPLACING a genuine
        # assertion message with a file-locking one. Found while mutating it.
        opened = []
        try:
            from backend import btdata
            from backend.marketdb import connect_market

            # --- a recorder's worth of synthetic SPY chains: 5 weekdays,
            # two expirations, snapshots at 19:45Z (in-hours ET), plus one
            # stale weekend snapshot that must be filtered out.
            mcon = connect_market(Path(td) / "market.db"); opened.append(mcon)
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
            dcon = btdata.connect_data(store); opened.append(dcon)
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

            # --- THE IMPORT PATH into the same store, same five days, a third
            # expiration. The engine must end up reading a store that two
            # different writers filled, because that is what a real install
            # looks like: some recorded, some uploaded, some pulled.
            from backend.chainimport import parse_text
            from backend import chainimport as _ci
            hdr = ("date,symbol,expiration,strike,type,bid,ask,"
                   "implied_volatility,delta")
            lines = [hdr]
            for i, day in enumerate(days):
                F = 500.0 + i * 0.5
                T = max((dt_date("2026-03-20") - dt_date(day)).days, 1) / 365.0
                for k in range(400, 601, 5):
                    for right in ("C", "P"):
                        px = b76(F, float(k), T, iv, right == "C")
                        d1 = (math.log(F / k) + 0.5 * iv * iv * T) / (iv * math.sqrt(T))
                        dl = norm_cdf(d1) if right == "C" else norm_cdf(d1) - 1.0
                        lines.append(f"{day},SPY,2026-03-20,{k}.0,{right},"
                                     f"{max(px - 0.02, 0.01):.4f},{px + 0.02:.4f},"
                                     f"{iv},{dl:.4f}")
            parsed = parse_text("\n".join(lines), "option_chain", "csv", "upload:t.csv")
            res = btdata.import_chain(dcon, "SPY", parsed.chain, "upload:t.csv")
            assert res["days"] == 5, f"imported {res['days']} days, wanted 5"
            assert res["contracts"] == len(lines) - 1

            st = btdata.stats(dcon)
            assert st["days"] == 5, \
                f"the import invented trading days: {st['days']} (wanted 5)"
            srcs = {s["src"]: s for s in st["sources"]}
            assert set(srcs) == {"recorded", "upload:t.csv"}, \
                f"provenance lost: {sorted(srcs)}"
            assert srcs["upload:t.csv"]["contracts"] == len(lines) - 1

            # Re-importing a CORRECTED file REPLACES. Both halves matter and
            # they fail differently: a plain INSERT doubles the rows, while an
            # INSERT OR IGNORE keeps the count right and silently discards the
            # correction — so counting alone passes a store that ignored every
            # fix the user uploaded.
            before = st["contracts"]
            fixed = [parsed.chain[0].__class__(
                **{**parsed.chain[0].__dict__, "bid": 99.25, "ask": 99.75})]
            btdata.import_chain(dcon, "SPY", fixed, "upload:t.csv")
            assert btdata.stats(dcon)["contracts"] == before, \
                "re-importing the same contract doubled its rows instead of replacing"
            r0 = parsed.chain[0]
            got = dcon.execute(
                "SELECT bid, ask FROM opt WHERE d=? AND exp=? AND cp=? AND strike=?",
                (int(r0.date.replace("-", "")), int(r0.expiration.replace("-", "")),
                 0 if r0.right == "C" else 1, int(round(r0.strike * 1000)))).fetchone()
            assert got["bid"] == 99.25 and got["ask"] == 99.75, \
                (f"a corrected re-upload was ignored: bid is still {got['bid']}. "
                 f"The row count would look perfectly correct.")

            # STRIKE ROUNDING, the quietest trap in the store. The engine keys
            # contracts by k/1000.0, so the importer must round exactly as the
            # recorder does. Truncation is off by one tenth of a cent on the
            # strikes where float multiplication lands just under the integer
            # (8.2 * 1000 is 8199.999999999999), and the failure is silent:
            # nothing raises, the holding just never finds its contract again
            # and quietly marks to intrinsic.
            # These specific values are the point: int(round(x*1000)) and
            # int(x*1000) agree on 512.5 and 640.1 and disagree here, because
            # 32.3*1000 is 32299.999999999996. A fixture of "awkward-looking"
            # strikes picked by eye tests nothing at all.
            awkward = [32.3, 64.1, 128.2, 256.4, 65.1]
            rows_a = [parsed.chain[0].__class__(
                **{**parsed.chain[0].__dict__, "strike": s,
                   "expiration": "2026-04-17"}) for s in awkward]
            btdata.import_chain(dcon, "SPY", rows_a, "upload:t.csv")
            got = sorted(r[0] / 1000.0 for r in dcon.execute(
                "SELECT strike FROM opt WHERE exp=20260417"))
            assert got == sorted(awkward), \
                f"strike round-trip broke: {got} != {sorted(awkward)}"

            # The two guards the parser cannot make.
            other = [r.__class__(**{**r.__dict__, "symbol": "QQQ"})
                     for r in parsed.chain[:2]]
            try:
                btdata.import_chain(dcon, "SPY", other, "x")
                raise AssertionError("a QQQ file imported into SPY.db — opt has "
                                     "no symbol column, so this blends two chains")
            except btdata.ImportRefused:
                pass
            sat = [r.__class__(**{**r.__dict__, "date": "2026-01-10"})
                   for r in parsed.chain[:2]]
            try:
                btdata.import_chain(dcon, "SPY", sat, "x")
                raise AssertionError("a Saturday imported: imported dates BECOME "
                                     "the engine's calendar, so it would trade it")
            except btdata.ImportRefused:
                pass
            assert btdata.stats(dcon)["days"] == 5, \
                "a refused import still wrote rows — it must be all-or-nothing"
            del _ci
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
            for c in opened:
                try:
                    c.close()
                except Exception:  # noqa: BLE001 - cleanup must not mask a failure
                    pass
            if old is None:
                os.environ.pop("GRINDSTONE_DATA_DIR", None)
            else:
                os.environ["GRINDSTONE_DATA_DIR"] = old


@check("engine store: migration reaches an EXISTING db, and missing stays NULL")
def _btdata_schema():
    """The trap this exists for has already fired once in this codebase.

    `CREATE TABLE IF NOT EXISTS` delivers a new TABLE to an old database but
    can NEVER deliver a new COLUMN, and it fails silently. market.db is the
    proof: chain_cache/chain_cover were added to its _SCHEMA without bumping
    SCHEMA_VERSION, and this developer's market.db has never had them — the
    backend log carries hundreds of 'no such table: chain_cover' fallbacks
    while every gate run stayed green.

    Green is exactly what a normal test gives you here, because tests build
    databases in tempdirs where user_version is 0 and the schema script runs
    unconditionally. So this check builds the OLD shape BY HAND and migrates
    it — the only way to exercise the path a real install takes.

    The last assertion is the general one: a migrated database and a fresh one
    must end up with identical columns. That fails the moment someone adds a
    column to _SCHEMA and forgets the ALTER, whatever the column is called."""
    import os
    import sqlite3
    import tempfile
    sys.path.insert(0, str(CODE))

    # The v0 shape: btdata's schema as it stood before provenance existed.
    V0 = """
    CREATE TABLE opt (d INTEGER NOT NULL, exp INTEGER NOT NULL, cp INTEGER NOT NULL,
      strike INTEGER NOT NULL, bid REAL, ask REAL, mark REAL, vol REAL, oi REAL,
      delta REAL, iv REAL, PRIMARY KEY (d, exp, cp, strike)) WITHOUT ROWID;
    CREATE TABLE bars (d INTEGER NOT NULL, t TEXT NOT NULL, o REAL, h REAL,
      l REAL, c REAL, PRIMARY KEY (d, t));
    CREATE TABLE sync_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """

    with tempfile.TemporaryDirectory() as td:
        old = os.environ.get("GRINDSTONE_DATA_DIR")
        os.environ["GRINDSTONE_DATA_DIR"] = td
        # Every connection opened below, closed in the finally. On Windows an
        # open handle makes TemporaryDirectory's cleanup raise WinError 32 as
        # the check unwinds — which REPLACES the assertion message with a file
        # -locking one, so a genuine failure reports the wrong cause. Observed
        # while mutation-testing this very check.
        opened = []
        try:
            from backend import btdata
            from backend.marketdb import connect_market

            assert btdata.SCHEMA_VERSION >= 1, "btdata must declare a SCHEMA_VERSION"

            aged = Path(td) / "aged.db"
            raw = sqlite3.connect(aged)
            raw.executescript(V0)
            raw.execute("INSERT INTO opt (d,exp,cp,strike,bid,ask,mark)"
                        " VALUES (20260105,20260220,1,600000,1.5,1.6,1.55)")
            raw.commit()
            assert raw.execute("PRAGMA user_version").fetchone()[0] == 0
            raw.close()

            con = btdata.connect_data(aged); opened.append(con)
            ver = con.execute("PRAGMA user_version").fetchone()[0]
            assert ver == btdata.SCHEMA_VERSION, \
                f"migration left user_version at {ver}, wanted {btdata.SCHEMA_VERSION}"
            aged_opt = {r[1] for r in con.execute("PRAGMA table_info(opt)")}
            aged_bars = {r[1] for r in con.execute("PRAGMA table_info(bars)")}
            assert {"src", "imported_at"} <= aged_opt, \
                f"ALTER never reached an existing opt table: {sorted(aged_opt)}"
            assert "src" in aged_bars, f"bars.src missing: {sorted(aged_bars)}"
            assert con.execute("SELECT COUNT(*) FROM opt").fetchone()[0] == 1, \
                "the migration destroyed rows it should have preserved"
            con.close()

            # A fresh database takes the _SCHEMA path instead; the ALTERs must
            # not raise on columns the CREATE already made.
            fresh = Path(td) / "fresh.db"
            con = btdata.connect_data(fresh); opened.append(con)
            fresh_opt = {r[1] for r in con.execute("PRAGMA table_info(opt)")}
            fresh_bars = {r[1] for r in con.execute("PRAGMA table_info(bars)")}
            con.close()
            assert aged_opt == fresh_opt, (
                "migrated and fresh databases disagree on opt's columns — a "
                f"column was added to _SCHEMA with no ALTER to deliver it: "
                f"fresh-only={sorted(fresh_opt - aged_opt)} "
                f"aged-only={sorted(aged_opt - fresh_opt)}")
            assert aged_bars == fresh_bars, (
                "migrated and fresh databases disagree on bars' columns: "
                f"fresh-only={sorted(fresh_bars - aged_bars)}")

            # MISSING IS NOT ZERO, through the real recorder->store writer.
            # A contract nobody quoted must land as NULL: a stored 0.0 is a
            # claim that someone bid zero, and nothing downstream can ever
            # tell the two apart again.
            mcon = connect_market(Path(td) / "market.db"); opened.append(mcon)
            with mcon:
                mcon.execute(
                    "INSERT INTO rec_chain (underlying, ts, occ_symbol, expiration,"
                    " strike, right, bid, ask, delta) VALUES"
                    " ('SPY','2026-01-05T19:45:00Z','A','2026-02-20',600,'P',1.5,1.6,-0.3)")
                mcon.execute(
                    "INSERT INTO rec_chain (underlying, ts, occ_symbol, expiration,"
                    " strike, right, bid, ask, delta) VALUES"
                    " ('SPY','2026-01-05T19:45:00Z','B','2026-02-20',400,'P',NULL,NULL,NULL)")
            store = btdata.data_db_path("SPY")
            dcon = btdata.connect_data(store); opened.append(dcon)
            btdata.sync_from_recorded(mcon, dcon, "SPY")
            quiet = dcon.execute(
                "SELECT bid, ask, mark, delta, src FROM opt WHERE strike=400000").fetchone()
            assert quiet is not None, "the unquoted contract was dropped entirely"
            assert quiet["bid"] is None, f"unquoted bid stored as {quiet['bid']!r}, wanted NULL"
            assert quiet["ask"] is None, f"unquoted ask stored as {quiet['ask']!r}, wanted NULL"
            assert quiet["mark"] is None, f"unquoted mark stored as {quiet['mark']!r}, wanted NULL"
            assert quiet["delta"] is None, (
                "a 0.0 delta is not a missing delta: bt/data.py trusts any "
                "|delta| < 9.0 verbatim and skips the model solve")
            assert quiet["src"] == "recorded", f"provenance not stamped: {quiet['src']!r}"
            live = dcon.execute(
                "SELECT bid, mark FROM opt WHERE strike=600000").fetchone()
            assert live["bid"] == 1.5 and abs(live["mark"] - 1.55) < 1e-9, \
                "a real quote was damaged by the null-preserving path"

            # And the engine still prices it, NULLs and all.
            from backend.bt.data import MarketData
            dcon.close()
            md = MarketData(str(store))
            # MarketData has no close(); register its read-only handles too.
            opened.append(md._opt)
            if md._bars is not None:
                opened.append(md._bars)
            assert md.all_dates() == [dt_date("2026-01-05")], repr(md.all_dates())
        finally:
            for c in opened:
                try:
                    c.close()
                except Exception:  # noqa: BLE001 - cleanup must not mask a failure
                    pass
            if old is None:
                os.environ.pop("GRINDSTONE_DATA_DIR", None)
            else:
                os.environ["GRINDSTONE_DATA_DIR"] = old


@check("chain import: header-driven, six refusals fire, missing stays NULL")
def _chain_import():
    """DATA_IMPORT.md's rules, each asserted individually.

    Fixtures are built in code, never checked in: .gitignore blocks *.csv.gz
    and *.db, and the credential scanner reads every tracked file with no
    self-exemption for test data.

    The refusals matter more than the happy path. Importing something
    plausible produces a chart and a backtest that are confidently wrong,
    which is strictly worse than a refused upload — the user of a refused
    upload knows they have a problem."""
    sys.path.insert(0, str(CODE))
    from backend.chainimport import Refused, parse_text

    HEAD = ("date,symbol,expiration,strike,type,bid,ask,last,volume,"
            "open_interest,implied_volatility,delta")
    GOOD = HEAD + "\n2026-08-05,SPY,2026-09-18,640.0,put,3.41,3.45,3.42,1204,8817,0.1412,-0.2731"

    def refused(label, text, kind="option_chain", fmt="csv", contains=""):
        try:
            parse_text(text, kind, fmt, "t")
        except Refused as e:
            assert contains.lower() in str(e).lower(), \
                f"{label}: refused for the wrong reason: {e}"
            return e
        raise AssertionError(f"{label}: ACCEPTED a file that must be refused")

    # --- the shape holds, and the header drives it
    p = parse_text(GOOD, "option_chain", "csv", "t")
    assert len(p.chain) == 1 and p.chain[0].right == "P" and p.chain[0].strike == 640.0
    assert abs(p.chain[0].iv - 0.1412) < 1e-9, "iv must stay a decimal"
    # Column ORDER must be irrelevant. The upstream archive reordered its
    # columns in 2024-11; a positional parse silently swaps fields across that
    # boundary and yields files that are well-formed and completely wrong.
    shuffled = ("zzz,delta,ask,type,strike,expiration,symbol,date,bid,implied_volatility\n"
                "junk,-0.27,3.45,P,640,2026-09-18,SPY,2026-08-05,3.41,0.1412")
    s = parse_text(shuffled, "option_chain", "csv", "t").chain[0]
    assert s.strike == 640.0 and s.right == "P" and s.bid == 3.41, \
        "header-name lookup broke: the parse went positional"

    # --- MISSING IS NOT ZERO, and a real zero is still a zero
    quiet = parse_text(HEAD + "\n2026-08-05,SPY,2026-09-18,640.0,put,,,,,,,",
                       "option_chain", "csv", "t").chain[0]
    assert quiet.bid is None and quiet.ask is None, \
        f"an unquoted contract became {quiet.bid!r}/{quiet.ask!r}, wanted None"
    assert quiet.delta is None, (
        "an absent delta became 0.0 — bt/data.py:69 trusts any |delta| < 9.0 "
        "verbatim and skips the model solve, so this silently replaces a "
        "computed delta with a fabricated one")
    real_zero = parse_text(HEAD + "\n2026-08-05,SPY,2026-09-18,640.0,put,0,0,,,,,",
                           "option_chain", "csv", "t").chain[0]
    assert real_zero.bid == 0.0, "a genuine zero bid must survive as 0.0"

    # --- the six refusals
    refused("unknown kind", GOOD, kind="chains", contains="unknown kind")
    refused("IV as percent",
            HEAD + "\n2026-08-05,SPY,2026-09-18,640,put,3.41,3.45,,,,14.12,-0.27"
                   "\n2026-08-05,SPY,2026-09-18,645,put,4.41,4.45,,,,15.02,-0.31",
            contains="percent")
    refused("naive intraday timestamp",
            "symbol,timestamp,open,high,low,close\nSPY,2026-08-05T13:30:00,1,2,0.5,1.5",
            kind="bars", contains="no timezone")
    refused("duplicate contract",
            GOOD + "\n2026-08-05,SPY,2026-09-18,640.0,put,3.50,3.55,,,,0.14,-0.27",
            contains="twice")
    refused("missing required column",
            "date,symbol,expiration,strike,type\n2026-08-05,SPY,2026-09-18,640,put",
            contains="missing required column")
    refused("json kind mismatch",
            '{"kind":"bars","rows":[{"a":1}]}', fmt="json", contains="declares kind")
    refused("header but no rows", HEAD, contains="no rows")
    refused("date-only on an intraday timeframe",
            "symbol,timestamp,timeframe,open,high,low,close\nSPY,2026-08-05,5Min,1,2,0.5,1.5",
            kind="bars", contains="only accepted for daily")

    # A refusal has to name the row, or the user is sent back to a
    # 14,000-line spreadsheet with "invalid file" and no way in.
    e = refused("bad number", GOOD + "\n2026-08-05,SPY,2026-09-18,650,put,x,3.4,,,,0.1,-0.2",
                contains="not a number")
    assert e.line == 3, f"refusal pointed at line {e.line}, wanted 3"

    # A date-only stamp IS the identity for a daily bar.
    ok = parse_text("symbol,timestamp,timeframe,open,high,low,close\n"
                    "SPY,2026-08-05,1Day,1,2,0.5,1.5", "bars", "csv", "t")
    assert ok.bars[0].ts == "2026-08-05"

    # numpy must not arrive with it: this module is imported by the sidecar,
    # and main.py bans heavy imports there.
    #
    # In a FRESH interpreter, because by the time this check runs some earlier
    # check has already imported numpy into THIS process — asserting on
    # sys.modules here would test the gate's own history, not chainimport.
    import subprocess
    r = subprocess.run(
        [sys.executable, "-c",
         "import sys; import backend.chainimport;"
         " sys.exit(1 if 'numpy' in sys.modules else 0)"],
        cwd=CODE, capture_output=True, text=True, timeout=60,
        stdin=subprocess.DEVNULL)
    assert r.returncode == 0, (
        "importing backend.chainimport pulls in numpy, which must never enter "
        f"the sidecar process:\n{r.stderr[-400:]}")


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
import { TF_MINUTES, slopePerHour, ChartDraw, chartMinutePrefix, barIndexOf,
         tradingDayOffset, dateAtTradingOffset }
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

// ---- trading-day arithmetic: expirations into bar space --------------------
// A leg object's expiration is a FUTURE calendar date; the chart's axis is
// trading days. This mapping is what places a zone past the last candle, so
// its edges are pinned on known dates: 2024-01-05 was a Friday.
ok('one trading day across a weekend is one bar',
   tradingDayOffset('2024-01-05', '2024-01-08') === 1, tradingDayOffset('2024-01-05', '2024-01-08'))
ok('a calendar week is five bars',
   tradingDayOffset('2024-01-05', '2024-01-12') === 5, tradingDayOffset('2024-01-05', '2024-01-12'))
ok('a Saturday target lands on the Friday before it (where it trades)',
   tradingDayOffset('2024-01-05', '2024-01-06') === 0, tradingDayOffset('2024-01-05', '2024-01-06'))
ok('the offset is signed', tradingDayOffset('2024-01-08', '2024-01-05') === -1, '')
ok('same day is zero', tradingDayOffset('2024-01-05', '2024-01-05') === 0, '')
ok('garbage dates degrade to null, not NaN', tradingDayOffset('junk', '2024-01-05') === null, '')
// The inverse, which turns a dragged pixel position back into an expiration.
ok('the inverse recovers the date across the weekend',
   dateAtTradingOffset('2024-01-05', 1) === '2024-01-08', dateAtTradingOffset('2024-01-05', 1))
ok('five bars forward is the next Friday',
   dateAtTradingOffset('2024-01-05', 5) === '2024-01-12', dateAtTradingOffset('2024-01-05', 5))
ok('and the round trip closes', (() => {
  const target = '2024-02-16' // 30 trading days out, another Friday
  const off = tradingDayOffset('2024-01-05', target)
  return dateAtTradingOffset('2024-01-05', off) === target
})(), '')

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
    assert len(results) >= 31, f"the probe lost assertions: only {len(results)} ran"


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
  // A leg holds the HIGHEST restored id, so the counter must adopt from the
  // legs list specifically — dw/ms/pin only reach 3, and a counter that skips
  // legs would mint 'lg4' for the next new object: two legs, one name, and
  // deleteLeg sweeps both.
  legs: [{ id: 'lg4', side: 'short', right: 'P', expiration: '2026-09-18',
           strike: 560, dteTol: 3, strikeTol: 5, slot: 0 }],
}

const store = mkStore({ 'SPY|1Day': RESTORED })
const e = mkEngine('SPY|1Day', store)
e.hydrate()
await settle(5)

let s = e.getState()
ok('a saved chart comes back', s.drawings === 1, s.drawings)
ok('measures and pins come back too', s.measures === 2, s.measures)
ok('legs come back with the rest', s.legs.length === 1 && s.legs[0].id === 'lg4',
   JSON.stringify(s.legs.map((l) => l.id)))
ok('hydrating is not itself a write', store.saves.length === 0, store.saves.length)

// The id counter adopted from the LEGS list: lg4 is the highest restored id,
// so the next minted object must be 5+, never a second 'lg4'.
const mintedLeg = e.addLeg({ side: 'long', right: 'C', expiration: '2026-10-16',
                             strike: 570, dteTol: 3, strikeTol: 5 }).id
ok('a new leg cannot reuse a restored leg id',
   mintedLeg !== 'lg4' && e.bucket().legs.filter((l) => l.id === 'lg4').length === 1,
   mintedLeg)
e.deleteLeg(mintedLeg) // leave the scene as the later assertions expect

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
    assert len(results) >= 19, f"the probe lost assertions: only {len(results)} ran"


@check("options: leg-window filtering is exact, and no-creds is a designed state")
def _options_chain():
    """The chart's leg objects filter contracts by an acceptance window
    (expiration ± DTE, strike ± $). The window bounds ride to the provider as
    query params AND are re-applied to the parsed rows by a pure function —
    the guard against a provider ignoring a param, and the thing this check
    can exercise offline. The claims:

      - Every bound is INCLUSIVE: a strike exactly on the zone's edge is
        inside it, which is what the drawn rectangle says.
      - A window reaching into the past is clamped to today and SAYS so —
        expired contracts are absent from the snapshot, and 'no contracts
        exist' and 'your window is yesterday' need different user reactions.
      - No creds is available=False with a reason, HTTP 200. The e2e profile
        has no key, so this state is rendered UI, not an error path.
      - Malformed parameters are 422, never a fetch.
    """
    import datetime as _dt
    import sqlite3
    import tempfile

    sys.path.insert(0, str(CODE))
    from fastapi.testclient import TestClient

    from backend import options as options_mod
    from backend.app import State, create_app

    today = _dt.date.today()
    d = lambda n: (today + _dt.timedelta(days=n)).isoformat()  # noqa: E731

    rows = [
        {"occ_symbol": "SPY..C1", "expiration": d(10), "strike": 560.0, "right": "C"},
        {"occ_symbol": "SPY..P1", "expiration": d(10), "strike": 560.0, "right": "P"},
        {"occ_symbol": "SPY..P2", "expiration": d(13), "strike": 555.0, "right": "P"},
        {"occ_symbol": "SPY..P3", "expiration": d(17), "strike": 550.0, "right": "P"},
        {"occ_symbol": "SPY..P4", "expiration": d(30), "strike": 560.0, "right": "P"},
        {"occ_symbol": "bad-exp", "expiration": "garbage", "strike": 560.0, "right": "P"},
        {"occ_symbol": "bad-strike", "expiration": d(10), "strike": None, "right": "P"},
    ]
    F = options_mod.filter_contracts

    got = F(rows, _dt.date.fromisoformat(d(10)), _dt.date.fromisoformat(d(17)),
            550.0, 560.0, "P")
    # Window midpoint is 555, so P2 (555, distance 0) leads; P1 and P3 tie at
    # distance 5 and the SOONER expiration wins. Order is the sort's claim.
    assert [r["occ_symbol"] for r in got] == ["SPY..P2", "SPY..P1", "SPY..P3"], got
    # INCLUSIVE bounds, all four edges: every one of those rows sits ON an edge
    # (exp 10 and 17, strike 550 and 560), so an exclusive comparison anywhere
    # drops a row this assertion names.
    assert F(rows, _dt.date.fromisoformat(d(10)), _dt.date.fromisoformat(d(10)),
             560.0, 560.0, "P")[0]["occ_symbol"] == "SPY..P1"
    assert F(rows, _dt.date.fromisoformat(d(1)), _dt.date.fromisoformat(d(60)),
             0.0, 1000.0, None).__len__() == 5, "right=None must keep both rights"
    assert all(r["right"] == "C" for r in
               F(rows, _dt.date.fromisoformat(d(1)), _dt.date.fromisoformat(d(60)),
                 0.0, 1000.0, "C"))
    # Sort: nearest the window's strike midpoint first, sooner expiry on ties.
    wide = F(rows, _dt.date.fromisoformat(d(1)), _dt.date.fromisoformat(d(60)),
             550.0, 570.0, "P")
    assert wide[0]["strike"] == 560.0 and wide[0]["expiration"] == d(10), wide[0]
    assert wide[1]["strike"] == 560.0 and wide[1]["expiration"] == d(30), wide[1]

    # ---- the service policy, offline --------------------------------------
    none_creds = options_mod.fetch(None, "spy", d(5), d(15), 500.0, 600.0, None)
    assert none_creds["available"] is False and "key" in none_creds["reason"], none_creds
    assert none_creds["underlying"] == "SPY", "symbol must upper-case"

    past = options_mod.fetch(None, "SPY", d(-30), d(-10), 500.0, 600.0, None)
    assert past["available"] is True and past["total"] == 0, past
    assert "past" in past.get("reason", ""), \
        "an all-past window must say WHY it is empty"

    for name, args in {
        "bad date": ("nope", d(5), 500.0, 600.0, None),
        "inverted dates": (d(15), d(5), 500.0, 600.0, None),
        "inverted strikes": (d(5), d(15), 600.0, 500.0, None),
        "bad right": (d(5), d(15), 500.0, 600.0, "X"),
    }.items():
        try:
            options_mod.fetch(None, "SPY", *args)
            raise AssertionError(f"{name}: accepted")
        except ValueError:
            pass

    # ---- the CREDS-PRESENT path -------------------------------------------
    # THE GAP THAT SHIPPED A BUG: every assertion above runs with creds=None,
    # so the branch that actually builds a client was never exercised. It did
    # `AlpacaData(*creds)` on a DICT, which unpacks the KEYS — the app
    # authenticated with the literal strings "key_id"/"secret_key" and Alpaca
    # answered 401 on every chain fetch while the account test passed.
    seen: dict[str, Any] = {}

    class _FakeClient:
        def __init__(self, key_id, secret_key):  # noqa: ANN001
            seen["ctor"] = (key_id, secret_key)

        def chain_snapshot(self, underlying, **kw):  # noqa: ANN001
            seen.setdefault("calls", []).append({"underlying": underlying, **kw})
            return rows

    real_client = options_mod.AlpacaData
    options_mod._cache.clear()
    try:
        options_mod.AlpacaData = _FakeClient  # type: ignore[misc]
        creds = {"key_id": "PKTESTKEYID", "secret_key": "test-secret-value"}
        got = options_mod.fetch(creds, "SPY", d(10), d(17), 550.0, 560.0, "P")
        assert seen.get("ctor") == ("PKTESTKEYID", "test-secret-value"), (
            f"the client was built with {seen.get('ctor')!r} — credentials must "
            "be passed by NAME; unpacking the dict sends its keys as the key")
        assert got["available"] is True and len(got["contracts"]) == 3, got
        # The window rides UPSTREAM as query params — that is what keeps one
        # leg's fetch to a page instead of the ~10k-row full chain.
        call = seen["calls"][0]
        assert call["underlying"] == "SPY", call
        assert call["exp_gte"] == d(10) and call["exp_lte"] == d(17), call
        assert call["strike_gte"] == 550.0 and call["strike_lte"] == 560.0, call
        assert call["right"] == "P", call
        # And the TTL cache means a repeated identical window costs nothing:
        # a drag-storm of commits must not amplify against the shared budget.
        options_mod.fetch(creds, "SPY", d(10), d(17), 550.0, 560.0, "P")
        assert len(seen["calls"]) == 1, \
            f"cache miss: {len(seen['calls'])} upstream calls for one window"

        # -- the fields the heatmap draws its COLUMNS from -------------------
        # Real listed dates come from the data, never from a weekday rule: SPY
        # has Good-Friday Thursdays and daily expiries, and any generated
        # calendar gets those wrong.
        assert got["expirations"] == sorted({c["expiration"] for c in got["contracts"]}), \
            f"expirations must be the distinct dates of the filtered rows: {got}"
        # And they come from the FULL filtered set, before the MAX_ROWS slice.
        # Reading them off the truncated list would drop whole columns while
        # `total` still counted the contracts in them — a grid quietly missing
        # its far month. Proven by truncating hard and checking the far date
        # survives even though its rows do not.
        real_max = options_mod.MAX_ROWS
        try:
            options_mod.MAX_ROWS = 1
            options_mod._cache.clear()
            cut = options_mod.fetch(creds, "SPY", d(10), d(17), 550.0, 560.0, "P")
            assert cut["truncated"] is True and len(cut["contracts"]) == 1, cut
            assert len(cut["expirations"]) > len(
                {c["expiration"] for c in cut["contracts"]}), (
                "expirations were taken AFTER the row cap, so a truncated "
                f"response loses whole columns: {cut['expirations']}")
        finally:
            options_mod.MAX_ROWS = real_max
        # Staleness is the server's to declare — only it knows whether it
        # served a live call or a cached region.
        assert isinstance(got.get("age_seconds"), (int, float)), got

        # -- archived history: the Opt page's history side --------------------
        # opthist reads <data>/options_history.db, which loadhist.py builds from
        # the vault. The gate is HERMETIC: data_dir is patched to a temp dir, so a
        # 588MB real database on this machine can neither help nor hurt.
        from backend import opthist as opthist_mod
        real_dd = opthist_mod.data_dir
        try:
            with tempfile.TemporaryDirectory() as tmp:
                opthist_mod.data_dir = lambda: Path(tmp)
                # ABSENT database: a named refusal, never an empty chart. This is
                # every fresh install, because Alpaca sells no historical option
                # quotes at any plan — the reason must say where history comes from.
                r = opthist_mod.history("SPY", "2026-09-18", 560.0, "P")
                assert r["available"] is False and "no archived" in r["reason"], r
                fr = opthist_mod.fanchart("SPY", "2026-09-18", 560.0, "P")
                assert fr["available"] is False and fr["band"] == [] and fr["path"] == [], fr

                hdb = sqlite3.connect(Path(tmp) / "options_history.db")
                hdb.executescript(
                    "CREATE TABLE hist_chain (underlying TEXT, date TEXT, expiration TEXT,"
                    " strike REAL, right TEXT, bid REAL, ask REAL, last REAL, iv REAL,"
                    " delta REAL, volume REAL, open_interest REAL,"
                    " PRIMARY KEY (underlying, expiration, strike, right, date));"
                    "CREATE TABLE hist_spread_pct (underlying TEXT, right TEXT, dte INT,"
                    " bucket INT, p10 REAL, p25 REAL, p50 REAL, p75 REAL, p90 REAL, n INT,"
                    " PRIMARY KEY (underlying, right, dte, bucket));"
                    "CREATE TABLE hist_meta (key TEXT PRIMARY KEY, value TEXT);")
                hist_rows = [
                    # three days of one contract; the middle day has NO BID
                    ("SPY", "2026-08-01", "2026-09-18", 560.0, "P", 3.40, 3.50, 3.45, .14, -.28, 10, 100),
                    ("SPY", "2026-08-04", "2026-09-18", 560.0, "P", 0.0, 3.60, None, .15, -.30, 0, 100),
                    ("SPY", "2026-08-05", "2026-09-18", 560.0, "P", 3.80, 3.90, 3.85, .15, -.32, 5, 90),
                ]
                hdb.executemany("INSERT INTO hist_chain VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", hist_rows)
                # two buckets of band rows; only the contract's own bucket may return.
                # median |delta| of (.28,.30,.32) is .30 -> bucket 3 (0.25-0.35)
                hdb.executemany("INSERT INTO hist_spread_pct VALUES (?,?,?,?,?,?,?,?,?,?)", [
                    ("SPY", "P", 40, 3, .05, .08, .10, .14, .20, 500),
                    ("SPY", "P", 45, 3, .05, .08, .11, .15, .22, 480),
                    ("SPY", "P", 40, 1, .01, .02, .03, .04, .05, 900),  # wrong bucket
                ])
                hdb.commit()
                hdb.close()

                h = opthist_mod.history("SPY", "2026-09-18", 560.0, "P")
                assert h["available"] is True and len(h["rows"]) == 3, h
                assert [x["date"] for x in h["rows"]] == ["2026-08-01", "2026-08-04", "2026-08-05"]
                assert h["rows"][0]["dte"] == 48, h["rows"][0]   # 08-01 -> 09-18
                # THE GAP RULE: a one-sided market has no mid and no spread. A zero
                # bid averaged into either would poison both charts downstream.
                assert h["rows"][1]["mid"] is None and h["rows"][1]["spread"] is None, h["rows"][1]
                assert abs(h["rows"][0]["spread"] - 0.10) < 1e-9, h["rows"][0]

                    # THE CONSTANT-SHAPE SERIES, delta-first (Kade's call): per day,
                # nearest DTE wins, then nearest |delta| — and the chosen strike
                # WALKS with the market, which is the whole point of the mode.
                hdb2 = sqlite3.connect(Path(tmp) / "options_history.db")
                hdb2.executemany(
                    "INSERT INTO hist_chain VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
                    # day 1: two candidates — dte 21 @ .31 must beat dte 23 @ .30
                    ("SPY", "2026-07-01", "2026-07-22", 600.0, "P", 2.0, 2.1, None, .2, -.31, 1, 1),
                    ("SPY", "2026-07-01", "2026-07-24", 601.0, "P", 2.2, 2.3, None, .2, -.30, 1, 1),
                    # day 2: same dte twice — nearer |delta| (.29) must win
                    ("SPY", "2026-07-02", "2026-07-23", 590.0, "P", 1.9, 2.0, None, .2, -.29, 1, 1),
                    ("SPY", "2026-07-02", "2026-07-23", 595.0, "P", 2.4, 2.5, None, .2, -.38, 1, 1),
                    # a no-delta row must never enter delta mode
                    ("SPY", "2026-07-06", "2026-07-27", 592.0, "P", 2.0, 2.1, None, .2, None, 1, 1),
                ])
                hdb2.commit(); hdb2.close()
                sr = opthist_mod.series_history("SPY", "P", 21, delta=-0.30)
                assert sr["available"] is True and sr["mode"] == "delta", sr
                days = {r["date"]: r for r in sr["rows"]}
                assert days["2026-07-01"]["used_dte"] == 21, days["2026-07-01"]
                assert abs(days["2026-07-02"]["used_delta"]) == 0.29, days["2026-07-02"]
                # the strike is an OUTPUT here, not a filter — it must differ by day
                assert days["2026-07-01"]["used_strike"] != days["2026-07-02"]["used_strike"]
                assert "2026-07-06" not in days, "a delta-less row entered the delta series"
                # no shape at all is a caller error, not an empty 200
                try:
                    opthist_mod.series_history("SPY", "P", 21)
                    raise AssertionError("series without delta or strike must refuse")
                except ValueError:
                    pass

                f = opthist_mod.fanchart("SPY", "2026-09-18", 560.0, "P")
                assert f["available"] is True and len(f["path"]) == 3, f
                assert f["bucket"]["median_delta"] == 0.3, f["bucket"]
                # only the median-delta bucket's band rows — never the wrong pool
                assert len(f["band"]) == 2 and all(b["p50"] >= 0.10 for b in f["band"]), f["band"]
                # an unknown contract refuses with its own reason, not an empty 200
                miss = opthist_mod.history("SPY", "2026-09-18", 999.0, "P")
                assert miss["available"] is False and "no archived rows" in miss["reason"], miss
        finally:
            opthist_mod.data_dir = real_dd


        # -- asset classes that have no chain, answered BEFORE the network ---
        seen["calls"].clear()
        for klass, word in (("index", "index"), ("future", "futures"), ("crypto", "crypto")):
            r = options_mod.fetch(creds, "SPX", d(10), d(17), 550.0, 560.0, "P",
                                  asset_class=klass)
            assert r["available"] is False and word in r["reason"], (klass, r)
            assert r["expirations"] == [], r
        assert not seen["calls"], \
            "an asset class with no chain still called the provider — the point " \
            "is to answer without one, so an empty list can never be confused " \
            "with a filter that matched nothing"
        # An UNKNOWN class passes through: the universe is not exhaustive, and
        # refusing on absence would block every ticker it has not heard of.
        options_mod._cache.clear()
        assert options_mod.fetch(creds, "SPY", d(10), d(17), 550.0, 560.0, "P",
                                 asset_class=None)["available"] is True
    finally:
        options_mod.AlpacaData = real_client  # type: ignore[misc]
        options_mod._cache.clear()

    # ---- the DATABASE cache: a drag inside a fetched window is free --------
    # The old cache was keyed by the exact filter tuple, and a drag mints a new
    # tuple on every commit — so it missed on precisely the motion it existed to
    # absorb. Coverage keying is the fix: a fetch records the REGION it covered,
    # and any later request inside that region is served without a call.
    import sqlite3 as _sq
    from backend import marketdb as _mdb

    seen2: dict[str, Any] = {}

    class _CountingClient:
        def __init__(self, key_id, secret_key):  # noqa: ANN001
            pass

        def chain_snapshot(self, underlying, **kw):  # noqa: ANN001
            seen2.setdefault("calls", []).append(kw)
            return rows

    with tempfile.TemporaryDirectory() as tmp2:
        con = _mdb.connect_market(Path(tmp2) / "market.db")
        real2 = options_mod.AlpacaData
        options_mod._cache.clear()
        try:
            options_mod.AlpacaData = _CountingClient  # type: ignore[misc]
            creds2 = {"key_id": "K", "secret_key": "S"}
            a = options_mod.fetch(creds2, "SPY", d(10), d(17), 550.0, 560.0, "P",
                                  con=con, ttl_minutes=15.0)
            assert a["available"] is True and len(a["calls" if False else "contracts"]) == 3, a
            assert len(seen2["calls"]) == 1, seen2

            # THE PAD: the live call deliberately asked for MORE than the leg
            # wanted, so the neighbourhood is covered by one request.
            call = seen2["calls"][0]
            assert call["strike_gte"] < 550.0 and call["strike_lte"] > 560.0, call
            # BOTH date edges. Asserting only the far one let a mutation that
            # dropped the near-side pad survive: half a pad still covers a
            # drag outward and misses every drag inward.
            assert call["exp_lte"] > d(17), call
            assert call["exp_gte"] < d(10), call

            # A DRAG: a different, narrower window inside the covered region.
            # The in-memory cache cannot serve this — different tuple — so a
            # second upstream call here would mean the DB cache did nothing.
            options_mod._cache.clear()
            b2 = options_mod.fetch(creds2, "SPY", d(11), d(16), 552.0, 558.0, "P",
                                   con=con, ttl_minutes=15.0)
            assert len(seen2["calls"]) == 1,                 f"a drag inside a cached window re-fetched: {seen2['calls']}"
            assert b2["source"].endswith("cached)"), b2["source"]
            assert b2["available"] is True, b2

            # OUTSIDE the covered region is a real miss, not a silent empty.
            options_mod._cache.clear()
            options_mod.fetch(creds2, "SPY", d(10), d(17), 900.0, 950.0, "P",
                              con=con, ttl_minutes=15.0)
            assert len(seen2["calls"]) == 2,                 f"an uncovered window did not reach the provider: {seen2['calls']}"

            # RETENTION IS THE USER'S: ttl 0 means always live.
            options_mod._cache.clear()
            options_mod.fetch(creds2, "SPY", d(11), d(16), 552.0, 558.0, "P",
                              con=con, ttl_minutes=0.0)
            assert len(seen2["calls"]) == 3,                 "ttl=0 must always fetch live, never serve the cache"

            st = options_mod.cache_stats(con)
            assert st["contracts"] > 0 and st["windows"] > 0, st
        finally:
            options_mod.AlpacaData = real2  # type: ignore[misc]
            options_mod._cache.clear()
            con.close()

    # ---- the route ---------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        state = State("boot-token-for-tests", db_path=Path(tmp) / "app.db")
        client = TestClient(create_app(state), base_url="http://127.0.0.1")
        B = {"X-App-Token": "boot-token-for-tests"}
        token = client.post("/api/auth/setup", headers=B,
                            json={"username": "t", "password": "longenough1"}).json()["token"]
        A = {**B, "Authorization": f"Bearer {token}"}
        q = {"exp_from": d(5), "exp_to": d(15),
             "strike_from": 500, "strike_to": 600}

        assert client.get("/api/symbols/SPY/options", headers=B,
                          params=q).status_code == 401
        r = client.get("/api/symbols/SPY/options", headers=A, params=q)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["available"] is False and body["contracts"] == [], body
        assert client.get("/api/symbols/SPY/options", headers=A,
                          params={**q, "exp_to": "junk"}).status_code == 422
        assert client.get("/api/symbols/SPY/options", headers=A,
                          params={**q, "right": "x"}).status_code == 422
        # Bounds are REQUIRED: the unfiltered chain is ~10k rows.
        assert client.get("/api/symbols/SPY/options", headers=A).status_code == 422, \
            "boundless chain request was accepted — that is a 10k-row payload"


@check("option legs: expiration is the primitive, hosts drive, snapshots survive")
def _chart_legs():
    """A leg is a point (expiration, strike) with an acceptance window, drawn
    on the chart and used to FILTER a chain — never to place an order. The
    claims that keep it honest:

      - The EXPIRATION DATE is the primitive. Deriving it from a host-relative
        fraction would make the expiration drift when the host's endpoints
        move in time, which is never what moving a trend means.
      - A trend host drives the strike AT the leg's expiration, segment
        EXTRAPOLATED — chart time is linear in bar index, so this is exact.
      - A dangling hostId is LEGAL (the measures policy): the leg degrades to
        its stored snapshot instead of vanishing with its host. Which also
        makes a legs-only document REACHABLE, so is_empty must count legs or
        the next autosave deletes the row.
      - Scalars round-trip. The enum drift check cannot see a dropped number,
        and validate()'s whitelist rebuild is exactly how one goes missing.
    """
    import tempfile

    sys.path.insert(0, str(CODE))
    from fastapi.testclient import TestClient

    from backend import chartobjects as co
    from backend.app import State, create_app

    KEY = "SPY|1Day"
    LEG = {"id": "lg9", "side": "short", "right": "P", "expiration": "2026-09-18",
           "strike": 560.0, "dteTol": 3, "strikeTol": 5.0, "slot": 1,
           "hostId": "gone-with-the-trend", "timeHostId": "dw7", "priceHostId": "dw8",
           "group": "gp3", "hidden": True, "pick": "SPY260918P00560000",
           "strikeHostA": "dwA", "strikeHostB": "dwB",
           "timeHostA": "dwC", "timeHostB": "dwD"}
    # A leg-minted guide. The flag is what lets the engine sweep it when no leg
    # rides it any more, and it must survive the database or every restored
    # chart leaks the lines its legs made.
    GUIDE = {"id": "dw7", "kind": "vline", "legOwned": True,
             "points": [{"time": 1789000000, "price": 560.0}]}

    # -- 1. store: round-trip, reachable legs-only doc, refusals -------------
    with tempfile.TemporaryDirectory() as tmp:
        state = State("boot-token-for-tests", db_path=Path(tmp) / "app.db")
        client = TestClient(create_app(state), base_url="http://127.0.0.1")
        B = {"X-App-Token": "boot-token-for-tests"}
        token = client.post("/api/auth/setup", headers=B,
                            json={"username": "t", "password": "longenough1"}).json()["token"]
        A = {**B, "Authorization": f"Bearer {token}"}

        r = client.put("/api/chart-objects", headers=A,
                       json={"key": KEY, "doc": {"legs": [LEG], "drawings": [GUIDE]}})
        assert r.status_code == 200, r.text
        back = client.get("/api/chart-objects", headers=A,
                          params={"key": KEY}).json()["doc"]
        assert back["legs"] == [LEG], f"a leg did not round-trip intact: {back['legs']}"
        # Scalar-blind-spot insurance: each number individually.
        got = back["legs"][0]
        assert got["strike"] == 560.0 and got["dteTol"] == 3 \
            and got["strikeTol"] == 5.0 and got["slot"] == 1, got
        # The dangling hostId SURVIVED — measures policy, not constraints.
        assert got["hostId"] == "gone-with-the-trend", got
        # EACH new field, by exact value. The vocabulary harvest compares enum
        # unions and is structurally blind to a scalar the validator simply
        # forgets to copy — which is exactly how `group` was lost: every condor
        # came back from the database as four unrelated legs, and no check
        # could see it because no vocabulary string had changed.
        assert got["timeHostId"] == "dw7", got
        assert got["priceHostId"] == "dw8", got
        assert got["group"] == "gp3", got
        # Per-leg visibility, same blind spot and a worse symptom: a boolean
        # introduces no vocabulary string at all, so every enum-drift assertion
        # stays green while the validator drops it. The leg then works for the
        # whole session and comes back VISIBLE after a restart, with nothing
        # anywhere reporting a loss. True, not False, on purpose — the field is
        # stored truthy-only, so False normalizes away and would prove nothing.
        assert got["hidden"] is True, got
        # The CHOSEN contract. A leg is a filter matching dozens of contracts;
        # this is the one turning it into a priceable trade, so losing it on
        # restart would silently move the analytics back onto a guess.
        assert got["pick"] == "SPY260918P00560000", got
        for f in ("strikeHostA", "strikeHostB", "timeHostA", "timeHostB"):
            assert got[f] == {"strikeHostA": "dwA", "strikeHostB": "dwB",
                              "timeHostA": "dwC", "timeHostB": "dwD"}[f], (f, got)
        assert back["drawings"][0].get("legOwned") is True, back["drawings"]

        # A legs-only doc is NOT empty: the row must exist after the save above.
        keys = client.get("/api/chart-objects/keys", headers=A).json()["charts"]
        assert [k["key"] for k in keys] == [KEY], \
            f"a legs-only chart lost its row: {keys}"
        assert not co.is_empty(back), "a legs-only document read as empty"

        bad = {
            "unknown side": {**LEG, "side": "hedged"},
            "unknown right": {**LEG, "right": "put"},
            "garbage expiration": {**LEG, "expiration": "Sep 18"},
            "negative strike": {**LEG, "strike": -5},
            "absurd dteTol": {**LEG, "dteTol": 400},
            "negative strikeTol": {**LEG, "strikeTol": -1},
            "float slot": {**LEG, "slot": 1.5},
            "numeric timeHostId": {**LEG, "timeHostId": 123},
            "numeric priceHostId": {**LEG, "priceHostId": 123},
            "numeric group": {**LEG, "group": 42},
            "numeric strikeHostA": {**LEG, "strikeHostA": 1},
            "numeric timeHostB": {**LEG, "timeHostB": 1},
            # 1 is truthy, so a validator that only tested truthiness would
            # store it and hand the renderer a non-boolean for a boolean field.
            "numeric hidden": {**LEG, "hidden": 1},
            "stringy hidden": {**LEG, "hidden": "yes"},
            "numeric pick": {**LEG, "pick": 12345},
            "oversized pick": {**LEG, "pick": "X" * 500},
        }
        for name, leg in bad.items():
            rr = client.put("/api/chart-objects", headers=A,
                            json={"key": KEY, "doc": {"legs": [leg]}})
            assert rr.status_code == 422, \
                f"{name}: expected 422, got {rr.status_code} {rr.text[:160]}"

    # -- 1b. WIRING, not behaviour -------------------------------------------
    # The probe below runs the lattice arithmetic under plain node, where there
    # is no timeScale — so it proves the maths and says nothing about the
    # projection actually reaching it. The e2e draws a real vline in the
    # whitespace; these two lines are what make that possible.
    draw_src_ws = (CODE / "app/src/renderer/src/components/ChartDraw.ts").read_text(
        encoding="utf-8")
    assert "return i === null ? null : this.xAtIdx(i)" in draw_src_ws, \
        "xForTime lost its whitespace fallback — nothing past the last candle projects"
    assert "this.timeAtX(p.point!.x)" in draw_src_ws, \
        "the crosshair no longer resolves a time in the whitespace, so the " \
        "placement preview dims out exactly where a leg's expiration lives"

    # -- 1c. WIRING: the label must not become a hit target -------------------
    # renderLeg builds its chip through chip(), which writes HTML — so the
    # node probe cannot reach it and this is a source assertion, honestly
    # labelled as one. What it guards is real and was reported: a chip
    # registered as a hot zone answers at distance 0 over whatever is beneath
    # it, so a label that appears on HOVER made the line it covered unpickable
    # at exactly the moment the user was reaching for it.
    leg_src = draw_src_ws.split("private renderLeg(")[1].split("\n  private ")[0]
    assert "this.chip(" in leg_src, "the leg label is gone entirely"
    chip_zone = [ln for ln in leg_src.splitlines()
                 if "zoneDraft.push" in ln and "box" in ln]
    assert not chip_zone, (
        "the leg's chip is a hot zone again — it will swallow clicks aimed at "
        f"the lines under it: {chip_zone[:1]}")
    assert "kind: 'leg', id: leg.id" in leg_src,         "the leg region stopped being pickable at all"

    # -- 2. vocabulary lockstep ----------------------------------------------
    draw_src = (CODE / "app/src/renderer/src/components/ChartDraw.ts").read_text(
        encoding="utf-8")
    sides = set(re.findall(r"'(\w+)'",
                re.search(r"export type LegSide =([^\n]+)", draw_src).group(1)))
    rights = set(re.findall(r"'(\w+)'",
                 re.search(r"export type LegRight =([^\n]+)", draw_src).group(1)))
    assert sides == set(co.LEG_SIDES), \
        f"leg sides drifted: engine {sorted(sides)} vs backend {sorted(co.LEG_SIDES)}"
    assert rights == set(co.LEG_RIGHTS), \
        f"leg rights drifted: engine {sorted(rights)} vs backend {sorted(co.LEG_RIGHTS)}"

    # -- 3. the engine's own arithmetic --------------------------------------
    app_dir = CODE / "app"
    if not (app_dir / "node_modules" / "typescript").exists():
        print("      (node_modules absent — npm install enables the leg probe)")
        return
    exe = _node_exe()
    assert exe, "no node runtime on PATH — the leg arithmetic cannot be run"

    probe = r"""
import { ChartDraw, legStrikeOnTrend, legWindow, resolveLegDoc, SIDE_INK, tradingDayOffset }
  from './src/renderer/src/components/ChartDraw.ts'
const out = []
const ok = (name, cond, detail) => out.push({ name, cond: !!cond, detail })

// EXTRAPOLATION IS EXACT: a trend from (bar 10, $600) to (bar 18, $604) is
// $0.50/bar, so at bar 30 — twelve bars past the segment's end — it reads
// $610. Inside the segment it interpolates; before it, it extends backwards.
ok('a trend extrapolates past its end', legStrikeOnTrend(10, 600, 18, 604, 30) === 610,
   legStrikeOnTrend(10, 600, 18, 604, 30))
ok('and interpolates within', legStrikeOnTrend(10, 600, 18, 604, 14) === 602, '')
ok('and extends backwards', legStrikeOnTrend(10, 600, 18, 604, 2) === 596, '')
ok('a vertical segment has no price elsewhere — null, not a guess',
   legStrikeOnTrend(10, 600, 10, 604, 30) === null, '')

// The acceptance window in the chain's own units.
const w = legWindow('2026-09-18', 560, 3, 5)
ok('±3 DTE is calendar days on both sides',
   w.expFrom === '2026-09-15' && w.expTo === '2026-09-21', JSON.stringify(w))
ok('±$5 both sides', w.strikeLo === 555 && w.strikeHi === 565, '')
ok('zero tolerance is a single expiration and strike', (() => {
  const z = legWindow('2026-09-18', 560, 0, 0)
  return z.expFrom === '2026-09-18' && z.expTo === '2026-09-18' &&
         z.strikeLo === 560 && z.strikeHi === 560
})(), '')
ok('garbage degrades to null', legWindow('junk', 560, 3, 5) === null, '')

// ---- the engine: hosts drive, snapshots survive ---------------------------
const day = (i) => Math.floor(Date.UTC(2024, 0, 2 + i, 14, 30) / 1000)
const bars = Array.from({ length: 40 }, (_, i) => ({ ts: new Date(day(i) * 1000).toISOString() }))
const mkEngine = (key) => Object.assign(Object.create(ChartDraw.prototype), {
  key, saveTimer: null, destroyed: false, changeCb: null, issue: null,
  tool: 'pointer', selected: [], hidden: false, barsOpt: () => bars,
  render() {}, applyCursor() {},
})
// ---- THE WHITESPACE BUG: drawing past the last candle ---------------------
// Kade: "you can only draw where candles exist, so you cant draw past the last
// candle." That is where every expiration lives, so the lines that drive a leg
// could not be placed where the leg is. The bar lattice now EXTENDS past the
// data, and the two extrapolations — the one legs use for expirations and the
// one drawings use for times — are the same code, because a vline drawn at a
// leg's expiration has to land on that leg's zone.
const wsBars = Array.from({ length: 40 }, (_, i) => ({
  ts: new Date(Date.UTC(2024, 0, 2 + i, 14, 30)).toISOString(),
}))
const ws = mkEngine('WS|1Day')
ws.barsOpt = () => wsBars
const lastT = Math.floor(Date.parse(wsBars[39].ts) / 1000)

ok('an in-range time resolves to its own bar', ws.idxForTime(lastT) === 39, ws.idxForTime(lastT))
// bars[39] is 2024-02-10, a Saturday; 2024-02-14 is three WEEKDAYS later.
const futT = Math.floor(Date.UTC(2024, 1, 14, 9, 0) / 1000)
ok('a future time extends the lattice by trading days',
   ws.idxForTime(futT) === 42, ws.idxForTime(futT))
ok('and the index inverts back to that date',
   new Date(ws.timeAtIdx(42) * 1000).toISOString().slice(0, 10) === '2024-02-14',
   new Date(ws.timeAtIdx(42) * 1000).toISOString())
ok('a future slot inherits the bars own time of day (14:30)',
   ws.timeAtIdx(42) % 86400 === lastT % 86400, ws.timeAtIdx(42) % 86400)

// THE BUG ITSELF, named by the contrast: nearestBarTime CLAMPS a future time
// back onto the last candle — correct for pinning a bar's OHLC, fatal for
// placing a line — and snapDrawTime does not.
ok('nearestBarTime still clamps to the last candle (unchanged contract)',
   ws.nearestBarTime(futT) === lastT, ws.nearestBarTime(futT))
ok('snapDrawTime does NOT clamp: a line can be placed past the data',
   ws.snapDrawTime(futT) > lastT, `${ws.snapDrawTime(futT)} vs ${lastT}`)
ok('and it lands on the extended lattice, not wherever the cursor was',
   ws.snapDrawTime(futT) === ws.timeAtIdx(42), ws.snapDrawTime(futT))
ok('in range it is still the nearest real bar',
   ws.snapDrawTime(lastT - 100) === lastT, ws.snapDrawTime(lastT - 100))

// A FRACTIONAL in-range index takes the NEAREST bar, not the one below it —
// the cursor is between two candles far more often than on one.
ok('an in-range fractional index rounds to the nearest bar',
   ws.timeAtIdx(38.6) === lastT, `${ws.timeAtIdx(38.6)} vs ${lastT}`)
ok('and rounds down below the midpoint',
   ws.timeAtIdx(38.4) === Math.floor(Date.parse(wsBars[38].ts) / 1000), '')

// AND IT HAS A HORIZON. The day-stepping helpers underneath are O(days), and
// timeAtX calls them on every crosshair move over the whitespace — scrolled far
// enough right, an unbounded version stopped answering entirely: the e2e hung
// on a CDP eval that never returned while the app itself looked healthy. There
// is nothing real out there to name, so null is the honest answer.
const t0 = Date.now()
ok('an absurd future index is refused, not walked to',
   ws.timeAtIdx(1e9) === null, ws.timeAtIdx(1e9))
ok('and an absurd future time likewise',
   ws.idxForTime(4e9) === null, ws.idxForTime(4e9))
ok('both answer immediately (an O(days) walk would not)', Date.now() - t0 < 250,
   `${Date.now() - t0}ms`)

// The agreement that makes an intersection possible at all.
ok('a leg expiration and a drawn time map to the SAME index',
   ws.idxForDate('2024-02-14') === ws.idxForTime(futT),
   `${ws.idxForDate('2024-02-14')} vs ${ws.idxForTime(futT)}`)

// A DAY IS NOT A BAR on an intraday chart: one trading day past the data is
// 78 five-minute bars, not one. A daily fixture cannot see this — 390/390 is
// 1, so dropping the conversion entirely looks correct there.
const m5 = mkEngine('WS|5Min')
const m5Bars = Array.from({ length: 12 }, (_, i) => ({
  ts: new Date(Date.UTC(2024, 0, 3, 14, 30 + i * 5)).toISOString(), // Wed
}))
m5.barsOpt = () => m5Bars
const m5Last = m5Bars.length - 1
ok('one trading day ahead on a 5Min chart is 78 bars',
   m5.idxForTime(Math.floor(Date.UTC(2024, 0, 4, 14, 30) / 1000)) === m5Last + 78,
   m5.idxForTime(Math.floor(Date.UTC(2024, 0, 4, 14, 30) / 1000)))
ok('and it inverts back to that day',
   new Date(m5.timeAtIdx(m5Last + 78) * 1000).toISOString().slice(0, 10) === '2024-01-04',
   new Date(m5.timeAtIdx(m5Last + 78) * 1000).toISOString())

// ---- presets: deterministic placement from bar data alone -----------------
const { PRESETS, placePreset, expirationForDte } =
  await import('./src/renderer/src/presets.ts')
const condor = PRESETS.find((p) => p.key === 'iron_condor')
const placed = placePreset(condor, 600, '2026-08-05')
ok('the condor is four legs', placed.length === 4, placed.length)
ok('short put below, long put further below',
   placed[0].strike === 570 && placed[1].strike === 564 &&
   placed[0].side === 'short' && placed[1].side === 'long' &&
   placed[0].right === 'P' && placed[1].right === 'P',
   JSON.stringify(placed.slice(0, 2).map((l) => `${l.side} ${l.right} ${l.strike}`)))
ok('short call above, long call further above',
   placed[2].strike === 630 && placed[3].strike === 636 &&
   placed[2].side === 'short' && placed[3].right === 'C', '')
ok('one shared expiration across the group',
   new Set(placed.map((l) => l.expiration)).size === 1, placed[0].expiration)
// 2026-08-05 + 45cd = 2026-09-19, a SATURDAY - must roll back to Friday.
ok('a weekend-landing expiration rolls back to Friday',
   placed[0].expiration === '2026-09-18', placed[0].expiration)
ok('wings carry the wider strike tolerance',
   placed[1].strikeTol > placed[0].strikeTol,
   `${placed[0].strikeTol} vs ${placed[1].strikeTol}`)
ok('a dead spot places nothing, not a crash', placePreset(condor, 0, '2026-08-05') === null, '')
ok('expirationForDte handles Sunday too',
   expirationForDte('2026-08-05', 46) === '2026-09-18', expirationForDte('2026-08-05', 46))

const e = mkEngine('LEGS|1Day')
const b = e.bucket()
b.drawings.push(
  { id: 'h1', kind: 'hline', points: [{ time: day(0), price: 580 }] },
  { id: 'tr', kind: 'trend',
    points: [{ time: day(10), price: 600 }, { time: day(18), price: 604 }] }
)
// THE LEG IS MINTED THROUGH THE API, not hand-pushed. A hand-pushed fixture id
// ('lg1') collided with the id the fresh module counter mints for the first
// addLeg — two legs, one name — and deleteLeg then swept both, making every
// slot assertion below pass VACUOUSLY under any mutation. Same lesson as the
// constraints round, inverted: fixtures must travel the path real objects do.
// Note 2024-01-02 is a Tuesday, so day() indices ARE calendar days here only
// while inside the same week; the resolution below uses in-range dates, where
// the real lattice answers, not the weekday extrapolation.
const legA = e.addLeg({ side: 'short', right: 'P',
  expiration: new Date(day(30) * 1000).toISOString().slice(0, 10),
  strike: 550, dteTol: 3, strikeTol: 5, hostId: 'tr' }).id
ok('the minted id is unique in the bucket',
   b.legs.filter((l) => l.id === legA).length === 1, JSON.stringify(b.legs.map((l) => l.id)))

const r1 = e.legResolved(b.legs[0])
ok('a trend host drives the strike at the LEG\'S expiration',
   Math.abs(r1.strike - 610) < 1e-9, r1.strike)   // bar 30 on the $0.50/bar line
ok('and reports what is hosting it', r1.hosted === 'trend', r1.hosted)

// Move the trend (as constraint propagation or a drag would): the leg follows.
b.drawings[1].points[1].price = 608   // now $1/bar
const r2 = e.legResolved(b.legs[0])
ok('moving the trend re-derives the strike', Math.abs(r2.strike - 620) < 1e-9, r2.strike)

// syncLegs folds the resolved values into the snapshot at commit time…
e.commit()
ok('commit folds the resolved strike into the snapshot',
   Math.abs(b.legs[0].strike - 620) < 1e-9, b.legs[0].strike)
// …which is what the leg lives on when the host dies.
b.drawings = b.drawings.filter((d) => d.id !== 'tr')
e.commit()
const r3 = e.legResolved(b.legs[0])
ok('a deleted host leaves the leg exactly where it was',
   Math.abs(r3.strike - 620) < 1e-9 && r3.hosted === null, JSON.stringify(r3))
ok('and the leg itself SURVIVES the deletion — measures policy, not pruning',
   b.legs.length === 1, b.legs.length)

// ---- KADE'S SENTENCE, AS ARITHMETIC --------------------------------------
// "if I want to go further DTE the strike must also drop with the trend line."
// Two hosts on ONE leg: a vline gives the expiration, a FALLING trend gives the
// strike AT that expiration. Move the vline alone and the strike must fall by
// the trend's slope times the bar delta. Nothing else in the suite can see
// this: with a single host, kind decided the role and the combination was
// unrepresentable.
const e2 = mkEngine('TH|1Day')
e2.barsOpt = () => bars
const b2 = e2.bucket()
// A trend falling $2/bar, drawn across bars 10..20 and EXTRAPOLATED past both.
b2.drawings.push({ id: 'tr2', kind: 'trend',
  points: [{ time: day(10), price: 700 }, { time: day(20), price: 680 }] })
b2.drawings.push({ id: 'vl2', kind: 'vline',
  points: [{ time: day(30), price: 0 }] })
const legT = e2.addLeg({ side: 'short', right: 'P',
  expiration: new Date(day(30) * 1000).toISOString().slice(0, 10),
  strike: 1, dteTol: 3, strikeTol: 5,
  timeHostId: 'vl2', priceHostId: 'tr2' }).id
const t1 = e2.legResolved(b2.legs.find((l) => l.id === legT))
// bar 30 on a $2/bar decline from 700@bar10 -> 700 - 2*20 = 660
ok('two hosts drive one leg: strike is the trend price AT the vline\'s date',
   Math.abs(t1.strike - 660) < 1e-9, t1.strike)
ok('and both roles are reported separately',
   t1.timeHosted === 'vline' && t1.priceHosted === 'trend',
   `${t1.timeHosted}/${t1.priceHosted}`)
ok('while `hosted` still summarises as the PRICE host for existing consumers',
   t1.hosted === 'trend', t1.hosted)

// PUSH THE EXPIRATION OUT — move ONLY the vline. The strike must follow the
// trend DOWN. Reading the STORED expiration here instead of the freshly
// resolved one leaves the strike a commit behind, which is the whole bug this
// ordering exists to prevent.
b2.drawings.find((d) => d.id === 'vl2').points[0].time = day(35)
const t2 = e2.legResolved(b2.legs.find((l) => l.id === legT))
ok('further DTE moves the expiration', t2.expiration > t1.expiration,
   `${t1.expiration} -> ${t2.expiration}`)
ok('AND THE STRIKE DROPS WITH THE TREND: 5 bars x -$2 = -$10',
   Math.abs(t2.strike - 650) < 1e-9, t2.strike)

// A trend endpoint drawn PAST THE LAST CANDLE must still drive the leg. It
// stopped: legResolved looked its endpoints up in the exact loaded-bar hash,
// which extended-lattice times are not in, so the leg silently froze.
b2.drawings.find((d) => d.id === 'tr2').points[1].time =
  e2.timeAtIdx(bars.length + 10)
const t3 = e2.legResolved(b2.legs.find((l) => l.id === legT))
ok('a trend with an endpoint past the last candle still drives its leg',
   t3.priceHosted === 'trend', `${t3.priceHosted} strike=${t3.strike}`)

// Typing one value releases ONE binding, not both.
e2.updateLeg(legT, { strike: 642 })
const t4 = e2.legResolved(b2.legs.find((l) => l.id === legT))
ok('typing a strike detaches the PRICE host only',
   t4.priceHosted === null && t4.timeHosted === 'vline',
   `${t4.timeHosted}/${t4.priceHosted}`)
ok('so the vline still drives the expiration after a typed strike',
   t4.expiration === t2.expiration, t4.expiration)

// An hline host drives the strike and leaves expiration alone. Bound through
// priceHostId, the current vocabulary — this leg's legacy hostId was already
// consumed by syncLegs' migrate-on-evidence during the commit above, which is
// the point of that migration and is asserted directly further down.
b.legs[0].priceHostId = 'h1'
const r4 = e.legResolved(b.legs[0])
ok('an hline host drives the strike', r4.strike === 580, r4.strike)
ok('and does not touch the expiration', r4.expiration === b.legs[0].expiration, '')

// Typing a strike into a hosted leg means "stop riding the line".
e.updateLeg(legA, { strike: 555 })
ok('typing a strike unbinds a strike-driven leg',
   b.legs[0].priceHostId === undefined && b.legs[0].strike === 555,
   JSON.stringify({ priceHostId: b.legs[0].priceHostId, strike: b.legs[0].strike }))

// ---- THE SIDE IS THE LINES' ORDER ----------------------------------------
// Kade: "having the top line go below the bottom line would swap from buy to
// sell or sell to buy with a noticeable color change." There is no side
// control and no stored answer that could disagree with the picture: the two
// strike lines' vertical order IS the side.
const e4 = mkEngine('SIDE|1Day')
e4.barsOpt = () => bars
const b4 = e4.bucket()
const legS = e4.addLeg({ side: 'short', right: 'P',
  expiration: new Date(day(20) * 1000).toISOString().slice(0, 10),
  strike: 600, dteTol: 3, strikeTol: 10 }).id
const L = () => b4.legs.find((l) => l.id === legS)
const s0 = e4.legResolved(L())
ok('a leg is born with four bounding lines',
   !!(L().strikeHostA && L().strikeHostB && L().timeHostA && L().timeHostB),
   JSON.stringify(L()))
ok('and its region comes off those lines, not a typed tolerance',
   s0.bounds !== null && Math.abs(s0.bounds.strikeHi - 610) < 1e-9 &&
   Math.abs(s0.bounds.strikeLo - 590) < 1e-9, JSON.stringify(s0.bounds))
ok('a SELL is born with strike line A above B', s0.side === 'short', s0.side)

// DRAG A THROUGH B. Nothing else changes — no field is set, no toggle pressed.
const dA = b4.drawings.find((d) => d.id === L().strikeHostA)
dA.points[0].price = 585   // now BELOW B (590)
const s1 = e4.legResolved(L())
ok('dragging the top strike line below the bottom one FLIPS the side to BUY',
   s1.side === 'long', s1.side)
ok('and the region is still the pair, lo/hi re-read from the lines',
   Math.abs(s1.bounds.strikeLo - 585) < 1e-9 &&
   Math.abs(s1.bounds.strikeHi - 590) < 1e-9, JSON.stringify(s1.bounds))
ok('the two sides get visibly different ink',
   SIDE_INK.long !== SIDE_INK.short, `${SIDE_INK.long}/${SIDE_INK.short}`)

// Dragging it back flips back: the gesture is symmetric, not a latch.
dA.points[0].price = 615
ok('and dragging it back above flips back to SELL',
   e4.legResolved(L()).side === 'short', e4.legResolved(L()).side)

// A PRESET must mint lines too. addLegGroup was the path the UI actually uses
// for a strategy, and it shipped without minting: the acceptance region drew
// from the fallback window, which for a freshly placed leg carries the SAME
// numbers as its lines would, so the chip looked perfect while no line existed.
// Asserting the DRAWING COUNT is what tells those two apart.
const e5 = mkEngine('GRP|1Day')
e5.barsOpt = () => bars
const b5 = e5.bucket()
const expG = new Date(day(25) * 1000).toISOString().slice(0, 10)
e5.addLegGroup([
  { side: 'short', right: 'P', expiration: expG, strike: 600, dteTol: 3, strikeTol: 5 },
  { side: 'long', right: 'P', expiration: expG, strike: 590, dteTol: 3, strikeTol: 5 },
])
ok('a preset mints lines for every leg it places', b5.drawings.length > 0,
   b5.drawings.length)
ok('every leg in the group is bounded by real lines',
   b5.legs.every((l) => e5.legResolved(l).bounds !== null),
   JSON.stringify(b5.legs.map((l) => !!e5.legResolved(l).bounds)))
// ONE expiration pair for the whole strategy: the legs share a date, so they
// share the lines that carry it and grabbing one rolls the structure.
ok('the group shares ONE expiration pair rather than stacking duplicates',
   b5.drawings.filter((d) => d.kind === 'vline').length === 2,
   b5.drawings.filter((d) => d.kind === 'vline').length)
ok('while each leg keeps its own strike pair',
   b5.drawings.filter((d) => d.kind === 'hline').length === 4,
   b5.drawings.filter((d) => d.kind === 'hline').length)
ok('and the shared vlines are the SAME ids on both legs',
   b5.legs[0].timeHostA === b5.legs[1].timeHostA &&
   b5.legs[0].timeHostB === b5.legs[1].timeHostB, '')
ok('every minted line is flagged as leg-owned',
   b5.drawings.every((d) => d.legOwned === true),
   JSON.stringify(b5.drawings.map((d) => d.legOwned)))

// THE FETCH WINDOW IS THE LINES. It was still built from strike +- tolerance,
// so a leg stretched wide by its lines went on asking for the narrow window it
// was born with — wide zone on the chart, a handful of contracts in the panel.
// getState is where the panel reads it, so that is where this is asserted.
const wideLeg = e4.getState().legs.find((l) => l.id === legS)
ok('the fetch window comes off the lines, not the birth tolerance',
   Math.abs(wideLeg.window.strikeLo - 590) < 1e-9 &&
   Math.abs(wideLeg.window.strikeHi - 615) < 1e-9,
   JSON.stringify(wideLeg.window))
// And the SIDE the panel prints is the derived one: a panel reading the stored
// field printed BUY over a leg the chart was drawing as SELL. Asserted while
// the two DISAGREE — the stored field still says 'short' from birth — because
// comparing them where they coincide proves nothing, and a mutation putting
// the stored field back survived exactly that mistake.
dA.points[0].price = 585            // flipped: derived long, stored still short
const flipped = e4.getState().legs.find((l) => l.id === legS)
ok('the reported side is the LINES answer, not the stored one',
   flipped.side === 'long' && L().side === 'short',
   `reported=${flipped.side} stored=${L().side}`)
dA.points[0].price = 615            // put it back for anything downstream

// A LINE INSIDE A LEG'S REGION IS STILL GRABBABLE. Hot zones answer at
// distance 0 and a region is a big one, so every line crossing it became
// unpickable exactly where you reach for it — Kade: "makes it impossible to
// move the top vertical line". hitAny is DOM-free, so the real picking runs
// here against a stubbed pane.
const e6 = mkEngine('PICK|1Day')
e6.barsOpt = () => bars
e6.paneSizeSafe = () => ({ width: 800, height: 400 })
e6.yForPrice = (p) => 400 - (p - 500) * 2      // 600 -> y 200, 610 -> y 180
e6.xForTime = () => 300
e6.xAtIdx = () => 300
const b6 = e6.bucket()
b6.drawings.push({ id: 'hl6', kind: 'hline', points: [{ time: day(5), price: 610 }] })
// A region covering the line's y, as a leg's acceptance band would.
e6.hotZones = [{ left: 100, top: 100, w: 400, h: 200, kind: 'leg', id: 'lgX' }]
b6.legs.push({ id: 'lgX', side: 'long', right: 'P', expiration: '2026-09-18',
               strike: 600, dteTol: 3, strikeTol: 5, slot: 0 })
const onLine = e6.hitAny(250, 180)     // exactly on the hline, inside the region
ok('a line inside a leg region picks the LINE, not the region',
   onLine && onLine.kind === 'drawing' && onLine.id === 'hl6',
   JSON.stringify(onLine && { kind: onLine.kind, id: onLine.id }))
const inRegion = e6.hitAny(250, 260)   // inside the region, far from any line
ok('and the region still picks the leg away from any line',
   inRegion && inRegion.kind === 'leg', inRegion && inRegion.kind)

// ---- THE LEGACY FOLD: documents saved before the two-host split ----------
// A stored leg carries ONE hostId whose role followed the drawing's kind. It
// must keep working with no rewrite of the document and no DOC_VERSION bump —
// and then migrate on EVIDENCE, once the host has resolved against a live
// drawing so its role is known rather than guessed.
const e3 = mkEngine('LEGACY|1Day')
e3.barsOpt = () => bars
const b3 = e3.bucket()
b3.drawings.push({ id: 'oldh', kind: 'hline', points: [{ time: day(5), price: 512 }] })
b3.drawings.push({ id: 'oldv', kind: 'vline', points: [{ time: day(12), price: 0 }] })
b3.legs.push({ id: 'lgOldP', side: 'long', right: 'C', expiration: '2026-09-18',
               strike: 1, dteTol: 3, strikeTol: 5, slot: 0, hostId: 'oldh' })
b3.legs.push({ id: 'lgOldT', side: 'long', right: 'C', expiration: '2026-09-18',
               strike: 400, dteTol: 3, strikeTol: 5, slot: 1, hostId: 'oldv' })
b3.legs.push({ id: 'lgOldX', side: 'long', right: 'C', expiration: '2026-09-18',
               strike: 404, dteTol: 3, strikeTol: 5, slot: 2, hostId: 'gone' })
const lp = e3.legResolved(b3.legs[0])
const lt = e3.legResolved(b3.legs[1])
ok('a legacy hline hostId still drives the strike', lp.strike === 512, lp.strike)
ok('a legacy vline hostId still drives the expiration',
   lt.expiration === new Date(day(12) * 1000).toISOString().slice(0, 10), lt.expiration)
e3.commit()
ok('and migrates on evidence: the hline host became priceHostId',
   b3.legs[0].priceHostId === 'oldh' && b3.legs[0].hostId === undefined,
   JSON.stringify(b3.legs[0]))
ok('the vline host became timeHostId',
   b3.legs[1].timeHostId === 'oldv' && b3.legs[1].hostId === undefined,
   JSON.stringify(b3.legs[1]))
// A DANGLING legacy id keeps its ambiguity rather than guessing a role: there
// is no drawing to ask, so a guess could silently make a strike out of an
// expiration the next time a line with that id came back.
ok('a dangling legacy hostId is left alone, not guessed at',
   b3.legs[2].hostId === 'gone' &&
   b3.legs[2].priceHostId === undefined && b3.legs[2].timeHostId === undefined,
   JSON.stringify(b3.legs[2]))

// Slots: first free slot is reused so the palette does not drift. The scene
// must DISCRIMINATE reuse from slot-by-count: after deleting the slot-0 leg,
// one leg (slot 1) remains, so first-free gives 0 while length gives 1.
const legB = e.addLeg({ side: 'long', right: 'C', expiration: '2026-09-18', strike: 600,
                        dteTol: 3, strikeTol: 5 }).id
ok('a second leg takes the next free color slot',
   b.legs.find((l) => l.id === legB).slot === 1, JSON.stringify(b.legs.map((l) => l.slot)))
e.deleteLeg(legA)
ok('deleting removes exactly the named leg',
   b.legs.length === 1 && b.legs[0].id === legB, JSON.stringify(b.legs.map((l) => l.id)))
const legC = e.addLeg({ side: 'long', right: 'P', expiration: '2026-09-18', strike: 590,
                        dteTol: 3, strikeTol: 5 }).id
ok('the freed slot 0 is reused rather than drifting the palette',
   b.legs.find((l) => l.id === legC).slot === 0, JSON.stringify(b.legs.map((l) => l.slot)))

// ---- THE HEATMAP: mid, capital base, annualisation ------------------------
const og = await import('./src/renderer/src/optgrid.ts')

// A ZERO BID HAS NO MID. The single most load-bearing rule on the surface:
// averaging a fabricated 0 against a real ask paints a confident half-price
// exactly out in the wings, which is where the eye-catching kinks live.
ok('a normal book has a mid', og.midOf(3.40, 3.50) === 3.45, og.midOf(3.40, 3.50))
ok('a ZERO bid has no mid', og.midOf(0, 0.05) === null, og.midOf(0, 0.05))
ok('a missing bid has no mid', og.midOf(null, 0.05) === null, og.midOf(null, 0.05))
ok('a missing ask has no mid', og.midOf(1.0, null) === null, '')
ok('a crossed book is not averaged', og.midOf(2.0, 1.0) === null, og.midOf(2.0, 1.0))
ok('cellState separates no-bid from no-quote',
   og.cellStateOf({ bid: 0, ask: 0.05 }) === 'no-bid' &&
   og.cellStateOf({ bid: 1, ask: null }) === 'no-quote' &&
   og.cellStateOf({ bid: 1, ask: 2 }) === 'priced', '')

// THE CAPITAL BASE IS THE STRIKE, PER SHARE. Premium and strike are both
// per-share quotes, so the x100 appears on both sides of premium/capital and
// cancels; carrying it bought nothing but a units mistake waiting to happen.
ok('the capital base is the bare strike', og.capitalFor(560) === 560, og.capitalFor(560))
ok('a nonsense strike has no base', og.capitalFor(0) === null, og.capitalFor(0))

// THE POINT OF THE DENOMINATOR: a higher strike must be pitted against a lower
// one on rate, not on bare premium. These are the real quotes from the panel —
// the 770 put pays 1.32 more than the 766, which LOOKS like the better trade
// until each is divided by the capital it ties up.
const r770 = 13.39 / 770
const r766 = 12.07 / 766
ok('the richer bare credit is also the better RATE here',
   r770 > r766, `${(r770 * 100).toFixed(3)}% vs ${(r766 * 100).toFixed(3)}%`)
// And the case that matters more, constructed rather than quoted: a LARGER
// credit on a LARGER strike can still be the worse trade. 12.20 beats 12.07 on
// the bare number and loses on the rate, which is the whole reason the
// denominator exists and the reason a bare-premium heatmap misleads.
ok('a bigger credit on a bigger strike can still be the worse rate',
   12.20 > 12.07 && 12.20 / 780 < r766,
   `${((12.20 / 780) * 100).toFixed(3)}% vs ${(r766 * 100).toFixed(3)}%`)

// COMPOUNDED OVER SESSIONS, because "is one 15-DTE better than rolling the
// 5-DTE three times" is a compounding question, and a position can only be
// rolled when the market opens.
ok('a year is 252 sessions, not 365 days', og.TRADING_DAYS_PER_YEAR === 252, '')
// LINEAR, not compounded. Compounding quoted 831%/yr on a real spread — a rate
// that assumes eight consecutive winning rolls, so it could never be collected.
const a21 = og.annualise(0.01, 21)          // ~30 calendar days
ok('1% over 21 sessions scales to 12%/yr, linearly',
   Math.abs(a21 - 0.12) < 1e-9, a21)
ok('and it stays LINEAR: half the horizon is exactly twice the rate',
   Math.abs(og.annualise(0.01, 10.5) - 2 * a21) < 1e-9, og.annualise(0.01, 10.5))
ok('a rich short-tenor return no longer explodes past all meaning',
   og.annualise(0.316, 31) < 3.0, og.annualise(0.316, 31))
ok('a non-positive horizon has no annual rate', og.annualise(0.01, 0) === null, '')
// WEEKENDS ARE WHERE THE TWO CONVENTIONS DIVERGE, and it is at short tenors —
// exactly where the surface is most tempting. 2026-08-06 is a Thursday.
ok('a span over one weekend counts its sessions, not its dates',
   og.tradingDaysTo('2026-08-06', '2026-08-14') === 6,
   og.tradingDaysTo('2026-08-06', '2026-08-14'))
ok('a span over two weekends holds FEWER sessions per calendar day',
   og.tradingDaysTo('2026-08-06', '2026-08-21') === 11,
   og.tradingDaysTo('2026-08-06', '2026-08-21'))
// 0DTE is one session of risk, not zero — it must annualise, not divide by nothing.
ok('a same-day expiry is one session, never zero',
   og.tradingDaysTo('2026-08-06', '2026-08-06') === 1, '')
// TWO WALKS, ONE ANSWER. optgrid repeats ChartDraw's weekday count because it
// must load under plain node and ChartDraw's extensionless imports will not.
// Duplication is normally drift waiting to happen, so pin them together: change
// one and this fails until the other follows.
for (const [from, to] of [['2026-08-06', '2026-08-14'], ['2026-08-06', '2026-08-21'],
                          ['2026-01-01', '2026-03-31'], ['2026-08-06', '2026-08-08']]) {
  ok(`both trading-day walks agree over ${from}..${to}`,
     og.tradingDaysTo(from, to) === Math.max(1, tradingDayOffset(from, to)),
     `${og.tradingDaysTo(from, to)} vs ${tradingDayOffset(from, to)}`)
}
// A 0DTE credit scaled by 252 sessions is large but FINITE and honest — under
// the old compounding it was a number with hundreds of digits.
ok('a 0DTE 5% credit scales to 1260%/yr, not to a nonsense',
   Math.abs(og.annualise(0.05, 1) - 12.6) < 1e-9, og.annualise(0.05, 1))
// The cap still guards the tail: past it the figure is noise, so it is reported
// AS capped rather than plotted.
ok('a genuinely absurd rate is CAPPED', og.annualise(0.5, 1) === og.ANNUAL_CAP,
   og.annualise(0.5, 1))
ok('and prints as capped rather than as a number',
   og.fmtAnnual(og.ANNUAL_CAP) === '>10000%', og.fmtAnnual(og.ANNUAL_CAP))
ok('no mid means no yield', og.annualYield(null, 56000, 30) === null, '')

// THE GRID.
const today = '2026-08-06'
const gc = (strike, expiration, right, bid, ask, delta) => ({
  occ_symbol: `SPY${expiration.replace(/-/g, '')}${right}${strike}`,
  expiration, strike, right, bid, ask, delta,
})
const g = og.buildGrid([
  gc(560, '2026-09-18', 'P', 3.40, 3.50, -0.27),
  gc(555, '2026-09-18', 'P', 2.40, 2.50, -0.21),
  gc(560, '2026-08-21', 'P', 1.40, 1.50, -0.19),
  gc(550, '2026-09-18', 'P', 0, 0.05, -0.02),   // zero bid: priced-out
], { today, side: 'short' })
ok('strikes descend, so the chain reads top-down',
   JSON.stringify(g.strikes) === '[560,555,550]', JSON.stringify(g.strikes))
ok('columns ascend by DTE',
   g.columns.map((c) => c.dte).join(',') === '15,43', g.columns.map((c) => c.dte).join(','))
ok('a pair with no contract is ABSENT, not zero',
   g.cells.get(og.cellKey(555, '2026-08-21')) === undefined, '')
ok('the zero-bid cell exists but carries no mid and no yield', (() => {
  const c = g.cells.get(og.cellKey(550, '2026-09-18'))
  return c && c.state === 'no-bid' && c.mid === null && c.annual === null
})(), JSON.stringify(g.cells.get(og.cellKey(550, '2026-09-18'))))
// The ramp is stretched over PRICED cells only — a zero-bid cell folded into
// the extremes would drag the whole surface toward a yield nobody can collect.
ok('the ramp extremes ignore unpriced cells',
   g.annualLo !== null && g.annualHi !== null && g.annualLo > 0,
   `${g.annualLo} .. ${g.annualHi}`)
ok('ramp position is 0..1 across the priced range',
   og.rampPosition(g.annualHi, g.annualLo, g.annualHi) === 1 &&
   og.rampPosition(g.annualLo, g.annualLo, g.annualHi) === 0, '')
ok('a flat surface shades nothing rather than painting one cell best',
   og.rampPosition(5, 5, 5) === null, '')
ok('credit is the gain token, debit the loss token',
   og.sideInk('short') === 'var(--gain)' && og.sideInk('long') === 'var(--loss)', '')

// ---- PAYOFF: known-answer structures, checked against hand arithmetic -----
const pf = await import('./src/renderer/src/payoff.ts')
const PL = (side, right, strike, premium) => ({ side, right, strike, premium })
const near = (a, b, tol = 1e-6) => Math.abs(a - b) < tol

// LONG CALL. Debit 5, breakeven 105, loss capped at the premium, profit runs.
{
  const r = pf.analyse([PL('long', 'C', 100, 5)])
  ok('long call is a debit', near(r.net, -5), r.net)
  ok('long call max loss is the premium and NOTHING more',
     r.maxLoss.bounded && near(r.maxLoss.value, -5), JSON.stringify(r.maxLoss))
  ok('long call profit is UNLIMITED, not a large number',
     r.maxProfit.bounded === false && r.maxProfit.value === null, JSON.stringify(r.maxProfit))
  ok('long call breaks even at strike + premium',
     r.breakevens.length === 1 && near(r.breakevens[0], 105), JSON.stringify(r.breakevens))
}
// NAKED SHORT CALL. The mirror, and the case a "max loss" number must refuse.
{
  const r = pf.analyse([PL('short', 'C', 100, 5)])
  ok('naked short call is a credit', near(r.net, 5), r.net)
  ok('naked short call LOSS is unlimited — never a big number with a decimal point',
     r.maxLoss.bounded === false && r.maxLoss.value === null, JSON.stringify(r.maxLoss))
  ok('and its profit is capped at the credit',
     r.maxProfit.bounded && near(r.maxProfit.value, 5), JSON.stringify(r.maxProfit))
}
// CASH-SECURED PUT. Downside is bounded because the underlying stops at zero,
// which is exactly why only the upside can ever run away.
{
  const r = pf.analyse([PL('short', 'P', 100, 3)])
  ok('short put max loss is bounded by a zero underlying',
     r.maxLoss.bounded && near(r.maxLoss.value, -97), JSON.stringify(r.maxLoss))
  ok('short put breaks even at strike - credit',
     near(r.breakevens[0], 97), JSON.stringify(r.breakevens))
}
// PUT CREDIT SPREAD, 5 wide, 1.50 credit: the defined-risk workhorse.
{
  const r = pf.analyse([PL('short', 'P', 100, 2.50), PL('long', 'P', 95, 1.00)])
  ok('spread nets the difference of the two premiums', near(r.net, 1.5), r.net)
  ok('spread max profit is the credit', r.maxProfit.bounded && near(r.maxProfit.value, 1.5), '')
  ok('spread max loss is width minus credit, and it is BOUNDED',
     r.maxLoss.bounded && near(r.maxLoss.value, -3.5), JSON.stringify(r.maxLoss))
  ok('spread breaks even at short strike - credit',
     r.breakevens.length === 1 && near(r.breakevens[0], 98.5), JSON.stringify(r.breakevens))
}
// IRON CONDOR: two breakevens, both wings bounded.
{
  const r = pf.analyse([
    PL('long', 'P', 90, 0.50), PL('short', 'P', 95, 1.50),
    PL('short', 'C', 105, 1.50), PL('long', 'C', 110, 0.50),
  ])
  ok('condor nets both credits less both wings', near(r.net, 2.0), r.net)
  ok('condor is bounded on BOTH sides',
     r.maxProfit.bounded && r.maxLoss.bounded, JSON.stringify([r.maxProfit, r.maxLoss]))
  ok('condor max loss is width minus credit', near(r.maxLoss.value, -3.0), r.maxLoss.value)
  ok('condor has two breakevens, one per wing',
     r.breakevens.length === 2 && near(r.breakevens[0], 93) && near(r.breakevens[1], 107),
     JSON.stringify(r.breakevens))
}
// A BREAKEVEN PAST THE LAST STRIKE must still be found — it lives on the tail,
// beyond every evaluated point.
{
  const r = pf.analyse([PL('long', 'C', 100, 20)])
  ok('a breakeven beyond the highest strike is still reported',
     r.breakevens.length === 1 && near(r.breakevens[0], 120), JSON.stringify(r.breakevens))
}
// NO MID, NO ANSWER. The rule that keeps a fabricated price out of a P&L.
{
  const r = pf.analyse([PL('short', 'P', 100, 2.5), PL('long', 'P', 95, null)])
  ok('a leg with no mid makes the whole structure unpriced',
     r.net === null && r.maxLoss.value === null, JSON.stringify(r))
  ok('and it says WHY, naming the count',
     typeof r.reason === 'string' && r.reason.includes('1 leg'), r.reason)
}
// RETURN ON RISK: one ratio — max gain over max risk — so a spread, a naked
// short and a heatmap cell are all measured the same way.
{
  const spread = pf.analyse([PL('short', 'P', 100, 2.50), PL('long', 'P', 95, 1.00)])
  // 1.50 credit against 3.50 at risk = 42.857% for the period.
  // THE PLAIN RATIO leads — what it pays against what it risks, no assumption
  // about repeating it. 1.50 credit on 3.50 at risk.
  ok('a credit spread returns credit / width-less-credit',
     Math.abs(pf.returnOnRisk(spread) - 0.428571) < 1e-5, pf.returnOnRisk(spread))
  const debit = pf.analyse([PL('long', 'C', 100, 1.50), PL('short', 'C', 105, 0.50)])
  ok('a DEBIT spread uses the same ratio: max gain over the debit risked',
     Math.abs(pf.returnOnRisk(debit) - 4.0) < 1e-9, pf.returnOnRisk(debit))
  // Kade's own spread, the one that read 831%/yr under compounding.
  const real = pf.analyse([PL('short', 'P', 750, 1.52), PL('long', 'P', 746, 0.56)])
  ok('the real spread pays ~31.6% of its risk', Math.abs(pf.returnOnRisk(real) - 0.32) < 0.01,
     pf.returnOnRisk(real))
  // The banner shows this ratio ALONE — no annualised twin. A per-year figure
  // on a single trade was noise: it answered a question nobody asks of one
  // position, and it crowded out the two numbers that matter.
  // No denominator, no percentage — never an invented one.
  ok('an unlimited loss has no return on risk',
     pf.returnOnRisk(pf.analyse([PL('short', 'C', 100, 5)])) === null, '')
  ok('an unlimited gain has no return on risk either',
     pf.returnOnRisk(pf.analyse([PL('long', 'C', 100, 5)])) === null, '')
}
ok('unbounded prints as a word, not a number', pf.fmtExtreme({ bounded: false, value: null })
   === 'unlimited', '')
ok('a credit reads as a credit in contract dollars',
   pf.fmtNet(1.5) === '$150.00 credit', pf.fmtNet(1.5))
ok('a debit reads as a debit', pf.fmtNet(-5) === '$500.00 debit', pf.fmtNet(-5))

// ---- THE COUPLING: a leg and its four lines are ONE object ----------------
// Kade: "deleting a leg leaves the lines we use to filter, and we can delete a
// single line from the filter control without deleting the whole leg."
// Both directions are asserted, and they are NOT symmetric: leg->lines is
// reference-counted (a strategy shares ONE expiration pair, so the first three
// deletions must not strand the fourth leg), while line->legs is total (a leg
// missing one of its four bounds silently reverts to its birth window, so
// there is no partial state worth keeping).
const c = mkEngine('CASCADE|1Day')
const cb = c.bucket()
const owned = () => cb.drawings.filter((d) => d.legOwned).map((d) => d.id)
const grp = c.addLegGroup([
  { side: 'short', right: 'P', expiration: '2026-09-18', strike: 560, dteTol: 3, strikeTol: 5 },
  { side: 'long', right: 'P', expiration: '2026-09-18', strike: 550, dteTol: 3, strikeTol: 5 },
])
ok('a two-leg group mints SIX lines, not eight (the expiration pair is shared)',
   owned().length === 6, JSON.stringify(owned()))
const [L1, L2] = cb.legs.map((l) => l.id)
const sharedV = cb.legs[0].timeHostA
ok('both legs name the same expiration line',
   cb.legs[0].timeHostA === cb.legs[1].timeHostA &&
   cb.legs[0].timeHostB === cb.legs[1].timeHostB, sharedV)

c.deleteLeg(L1)
ok('deleting a leg takes its own strike lines with it',
   owned().length === 4, JSON.stringify(owned()))
ok('...but NOT the expiration pair its sibling is still bounded by',
   owned().includes(sharedV) && cb.legs.length === 1 && cb.legs[0].id === L2,
   JSON.stringify({ owned: owned(), legs: cb.legs.map((l) => l.id) }))
c.deleteLeg(L2)
ok('deleting the last rider finally releases the shared pair',
   owned().length === 0 && cb.legs.length === 0, JSON.stringify(owned()))

// THE OTHER DIRECTION, on a fresh group: killing one bound kills its legs.
c.addLegGroup([
  { side: 'short', right: 'C', expiration: '2026-09-18', strike: 610, dteTol: 3, strikeTol: 5 },
  { side: 'long', right: 'C', expiration: '2026-09-18', strike: 620, dteTol: 3, strikeTol: 5 },
])
const sharedV2 = cb.legs[0].timeHostA
c.selected = [sharedV2]
c.deleteSelected()
ok('deleting a SHARED expiration line takes every leg riding it',
   cb.legs.length === 0 && owned().length === 0,
   JSON.stringify({ legs: cb.legs.length, owned: owned() }))

// One strike line, one leg — the plain case, and the selection must not be
// left pointing at ids that no longer exist.
const solo = c.addLeg({ side: 'short', right: 'P', expiration: '2026-09-18',
                        strike: 500, dteTol: 3, strikeTol: 5 }).id
const soloA = cb.legs[0].strikeHostA
c.selected = [soloA]
c.deleteSelected()
ok('deleting one strike bound removes its leg and all four lines',
   cb.legs.length === 0 && owned().length === 0, JSON.stringify(owned()))
ok('and the selection keeps no ids for objects that are gone',
   c.selected.length === 0, JSON.stringify(c.selected))

// A USER-DRAWN host is not part of the leg and never swept with it — the
// dangle-and-degrade policy the measures use, and the reason deleting a trend
// you drew yourself cannot silently take a strategy with it.
cb.drawings.push({ id: 'mine', kind: 'trend',
  points: [{ time: day(10), price: 600 }, { time: day(18), price: 604 }] })
const bound = c.addLeg({ side: 'short', right: 'P', expiration: '2026-09-18',
                         strike: 570, dteTol: 3, strikeTol: 5, priceHostId: 'mine' }).id
c.deleteLeg(bound)
ok('deleting a leg leaves the line the USER drew for it',
   cb.drawings.some((d) => d.id === 'mine'), JSON.stringify(cb.drawings.map((d) => d.id)))

// 'Clear every drawing' must not manufacture the broken state either.
const keep = c.addLeg({ side: 'short', right: 'P', expiration: '2026-09-18',
                        strike: 540, dteTol: 3, strikeTol: 5 }).id
c.clearDrawings()
ok('clearing drawings spares the lines that ARE a leg',
   cb.legs.length === 1 && owned().length === 4,
   JSON.stringify({ legs: cb.legs.length, owned: owned() }))
ok('and removes the ones the user drew', !cb.drawings.some((d) => d.id === 'mine'), '')
const resolvedKeep = c.legResolved(cb.legs[0])
ok('the spared leg still resolves through its bounds, not a stale snapshot',
   resolvedKeep.bounds !== null, JSON.stringify(resolvedKeep.bounds))
c.clearLegs()
ok('and Clear legs takes the lines with it', owned().length === 0, JSON.stringify(owned()))

// ---- RESOLVING A LEG FROM A STORED DOC ALONE -----------------------------
// The Opt page has a document and no engine, and reading the stored `side`
// showed BUY 769.8 for a chart drawing SELL 756.1. The lines are the answer.
{
  const e = mkEngine('DOC|1Day')
  const eb = e.bucket()
  const id = e.addLeg({ side: 'short', right: 'P', expiration: '2026-09-18',
                        strike: 560, dteTol: 3, strikeTol: 5 }).id
  const leg = eb.legs.find((l) => l.id === id)
  const A = eb.drawings.find((d) => d.id === leg.strikeHostA)
  const B = eb.drawings.find((d) => d.id === leg.strikeHostB)
  ok('a short leg is born with A above B', A.points[0].price > B.points[0].price, '')
  const r1 = resolveLegDoc(leg, eb.drawings)
  ok('resolving from the document alone reads SELL', r1.side === 'short', r1.side)
  ok('and takes the strike from the line MIDPOINT, not the stored field',
     Math.abs(r1.strike - 560) < 1e-9, r1.strike)

  // DRAG A THROUGH B: the side flips, and the stored field never moves.
  A.points[0].price = 540
  B.points[0].price = 580
  const r2 = resolveLegDoc(leg, eb.drawings)
  ok('crossing the strike lines flips the side to BUY', r2.side === 'long', r2.side)
  ok('while the STORED side still says short — which is why it must not be read',
     leg.side === 'short', leg.side)
  ok('and the strike follows the new midpoint', Math.abs(r2.strike - 560) < 1e-9, r2.strike)

  // Move the pair somewhere else entirely: the stored strike is stale, the
  // resolved one is not. This is the 769.8-vs-756.1 case exactly.
  A.points[0].price = 760
  B.points[0].price = 752
  const r3 = resolveLegDoc(leg, eb.drawings)
  ok('a moved pair resolves to where the lines ARE, not where the leg was born',
     Math.abs(r3.strike - 756) < 1e-9, `${r3.strike} (stored ${leg.strike})`)
  ok('and that disagrees with the stored strike, proving the check is not vacuous',
     Math.abs(leg.strike - r3.strike) > 1, `${leg.strike} vs ${r3.strike}`)
  // THE WINDOW IS THE LINES' RECTANGLE — engine parity for any doc-only
  // reader. The Opt page once rebuilt a window from the birth tolerances and
  // showed 8 matches while the chart showed 204 for the same filter.
  ok('the doc resolution carries the lines rectangle as the filter window',
     r3.window !== null &&
     Math.abs(r3.window.strikeLo - 752) < 1e-9 &&
     Math.abs(r3.window.strikeHi - 760) < 1e-9 &&
     typeof r3.window.expFrom === 'string' && typeof r3.window.expTo === 'string',
     JSON.stringify(r3.window))
}

// ---- PER-LEG VISIBILITY ---------------------------------------------------
// The same rider count, over the VISIBLE legs instead of the surviving ones.
const v = mkEngine('VIS|1Day')
const vb = v.bucket()
v.addLegGroup([
  { side: 'short', right: 'P', expiration: '2026-09-18', strike: 560, dteTol: 3, strikeTol: 5 },
  { side: 'long', right: 'P', expiration: '2026-09-18', strike: 550, dteTol: 3, strikeTol: 5 },
])
const [V1, V2] = vb.legs.map((l) => l.id)
const vShared = vb.legs[0].timeHostA
const v1Strike = vb.legs.find((l) => l.id === V1).strikeHostA
ok('nothing is unlit while every leg is visible',
   v.hiddenLineIds(vb).size === 0, JSON.stringify([...v.hiddenLineIds(vb)]))
v.setLegHidden(V1, true)
const unlit = v.hiddenLineIds(vb)
ok('hiding a leg unlights its own strike lines',
   unlit.has(v1Strike), JSON.stringify([...unlit]))
ok('but NOT the expiration pair its visible sibling still rides',
   !unlit.has(vShared), JSON.stringify([...unlit]))
ok('the hidden leg is still a live object that resolves',
   vb.legs.find((l) => l.id === V1).hidden === true &&
   v.legResolved(vb.legs.find((l) => l.id === V1)).bounds !== null, '')
v.setLegHidden(V2, true)
ok('hiding the last visible rider finally unlights the shared pair',
   v.hiddenLineIds(vb).has(vShared), JSON.stringify([...v.hiddenLineIds(vb)]))
v.setLegHidden(V1, false)
ok('showing one leg relights the shared pair for it',
   !v.hiddenLineIds(vb).has(vShared), JSON.stringify([...v.hiddenLineIds(vb)]))

console.log(JSON.stringify(out))
"""
    probe_path = app_dir / ".selftest-legs.mjs"
    try:
        probe_path.write_text(probe, encoding="utf-8")
        r = subprocess.run([exe, str(probe_path)], cwd=app_dir,
                           capture_output=True, text=True, timeout=180)
        assert r.returncode == 0, f"leg probe crashed:\n{(r.stderr or r.stdout)[:1500]}"
        results = json.loads(r.stdout.strip().splitlines()[-1])
    finally:
        probe_path.unlink(missing_ok=True)
    bad_r = [x for x in results if not x["cond"]]
    assert not bad_r, "the leg model is wrong:\n" + "\n".join(
        f"  - {x['name']} (got {x['detail']})" for x in bad_r)
    assert len(results) >= 159, f"the probe lost assertions: only {len(results)} ran"


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

        # An 'on' round-trips with BOTH references and the optional axis.
        rel = {**doc, "constraints": [
            {"id": "cn3", "kind": "on",
             "a": {"id": "dw2", "part": "a"}, "b": {"id": "dw1", "part": "line"}},
            {"id": "cn5", "kind": "lock",
             "a": {"id": "dw2", "part": "b", "axis": "price"}}]}
        rr = client.put("/api/chart-objects", headers=A, json={"key": KEY, "doc": rel})
        assert rr.status_code == 200, rr.text
        got = client.get("/api/chart-objects", headers=A, params={"key": KEY}).json()["doc"]
        assert got["constraints"][0]["b"] == {"id": "dw1", "part": "line"}, got["constraints"]
        assert got["constraints"][1]["a"]["axis"] == "price", got["constraints"]

        # A driving slope round-trips with its value intact. The enum drift
        # check structurally cannot see a dropped scalar, and validate()'s
        # whitelist normalization produces exactly the "works all session, gone
        # after restart, no error anywhere" failure.
        drive = {**doc, "constraints": [
            {"id": "cn7", "kind": "slope", "a": {"id": "dw2", "part": "line"},
             "value": 0.076923}]}
        rr = client.put("/api/chart-objects", headers=A, json={"key": KEY, "doc": drive})
        assert rr.status_code == 200, rr.text
        kept = client.get("/api/chart-objects", headers=A, params={"key": KEY}).json()["doc"]
        assert kept["constraints"][0]["value"] == 0.076923, kept["constraints"]

        bad = {
            "dangling reference": {**doc, "constraints": [
                {"id": "cn4", "kind": "lock", "a": {"id": "nope", "part": "line"}}]},
            "unknown constraint kind": {**doc, "constraints": [
                {"id": "cn4", "kind": "parallel", "a": {"id": "dw1", "part": "line"}}]},
            "unknown entity part": {**doc, "constraints": [
                {"id": "cn4", "kind": "lock", "a": {"id": "dw1", "part": "middle"}}]},
            "constraint id collides with a drawing": {**doc, "constraints": [
                {"id": "dw1", "kind": "lock", "a": {"id": "dw1", "part": "line"}}]},
            # A relation stated with one reference would load as a silent no-op.
            "'on' with no second reference": {**doc, "constraints": [
                {"id": "cn4", "kind": "on", "a": {"id": "dw2", "part": "a"}}]},
            "'on' against itself": {**doc, "constraints": [
                {"id": "cn4", "kind": "on", "a": {"id": "dw2", "part": "a"},
                 "b": {"id": "dw2", "part": "line"}}]},
            "'on' with a dangling host": {**doc, "constraints": [
                {"id": "cn4", "kind": "on", "a": {"id": "dw2", "part": "a"},
                 "b": {"id": "gone", "part": "line"}}]},
            "lock given a second reference": {**doc, "constraints": [
                {"id": "cn4", "kind": "lock", "a": {"id": "dw1", "part": "line"},
                 "b": {"id": "dw2", "part": "line"}}]},
            "slope with no value": {**doc, "constraints": [
                {"id": "cn4", "kind": "slope", "a": {"id": "dw2", "part": "line"}}]},
            "slope on a coordinate rather than a line": {**doc, "constraints": [
                {"id": "cn4", "kind": "slope",
                 "a": {"id": "dw2", "part": "line", "axis": "price"}, "value": 0.5}]},
            "slope with a non-finite value": {**doc, "constraints": [
                {"id": "cn4", "kind": "slope", "a": {"id": "dw2", "part": "line"},
                 "value": None}]},
            "lock carrying a value": {**doc, "constraints": [
                {"id": "cn4", "kind": "lock", "a": {"id": "dw1", "part": "line"},
                 "value": 0.5}]},
            "unknown axis": {**doc, "constraints": [
                {"id": "cn4", "kind": "lock",
                 "a": {"id": "dw1", "part": "line", "axis": "sideways"}}]},
        }
        for name, d in bad.items():
            rr = client.put("/api/chart-objects", headers=A, json={"key": KEY, "doc": d})
            assert rr.status_code == 422, f"{name}: expected 422, got {rr.status_code} {rr.text[:160]}"
        # The message matters, not just the status: a half-stated relation has a
        # specific cause and _ref's generic "must be an entity reference" would
        # send the reader looking in the wrong place.
        rr = client.put("/api/chart-objects", headers=A, json={"key": KEY, "doc": bad["'on' with no second reference"]})
        assert "held against" in rr.text, f"unhelpful refusal for a half-stated 'on': {rr.text[:200]}"

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

    # A snap the user cannot see is a snap they cannot trust: Kade reported
    # exactly that ("the lines dont snap together so its hard to tell when they
    # actually get connected"). Three things carry the feedback and each can be
    # deleted independently, so each is named here.
    # Matched on the call, not on its exact argument list: renderJoints took a
    # second parameter when per-leg visibility landed (a joint whose lines are
    # hidden must go with them), and pinning the arity would have failed a
    # change that kept the guarantee intact.
    assert "this.renderJoints(b" in draw_src, \
        "placed 'on' relations draw no joint marker — a connection would be invisible"
    assert "this.snapToLine(cur.x, cur.y)" in draw_src, \
        "the trend preview no longer previews the snap, so it is only visible after the click"
    assert "this.line(a, snap ?? cur," in draw_src, \
        "the rubber band tracks the raw cursor again, so it trails off the line it will snap to"
    assert draw_src.count("cd-joint") >= 1, "the joint marker is unfindable from the DOM"

    # WIRING, not behaviour: the probe below calls restoreSlopes directly, so it
    # proves the solve is right and says nothing about it being reached. A
    # driven slope that is only restored when something else calls it is a lock
    # that holds until the moment you actually move something.
    assert "this.issue = this.restoreSlopes(grabbed)" in draw_src, \
        "dragging no longer restores driven slopes"
    assert draw_src.count("this.restoreSlopes(") >= 4, \
        "a slope-restoring path was dropped (drag, exact edit, typed value, set-slope)"

    # -- 3. the engine's own arithmetic and behaviour ------------------------
    app_dir = CODE / "app"
    if not (app_dir / "node_modules" / "typescript").exists():
        print("      (node_modules absent — npm install enables the constraint probe)")
        return
    exe = _node_exe()
    assert exe, "no node runtime on PATH — the constraint arithmetic cannot be run"

    probe = r"""
import { ChartDraw, slotsOf, degreesOfFreedom, analyze, propagate }
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
const lockAxis = (id, on, part, axis) => ({ id, kind: 'lock', a: { id: on, part, axis } })

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
// A per-axis lock holds ONE coordinate: the editor's per-field padlocks.
ok('locking just a price leaves the date free',
   degreesOfFreedom(scene, [lockAxis('cn1', 'dw3', 'a', 'price')]).free === 5,
   degreesOfFreedom(scene, [lockAxis('cn1', 'dw3', 'a', 'price')]).free)
ok('and locking just a date leaves the price free',
   degreesOfFreedom(scene, [lockAxis('cn1', 'dw3', 'a', 'time')]).free === 5, '')

// ---- 'on' is an EQUALITY, not a lock --------------------------------------
// Kade: "this is not locking any X or Y but instead forcing the trend line to
// stretch to always be in contact with both h lines." So the endpoint takes the
// line's PRICE and keeps its own TIME — and that surviving freedom is what lets
// the diagonal slide while still touching.
const held = (pointId, part, lineId) =>
  ({ id: 'cn_' + pointId + part + lineId, kind: 'on',
     a: { id: pointId, part }, b: { id: lineId, part: 'line' } })

const bound = [held('dw3', 'a', 'dw1'), held('dw3', 'b', 'dw2')]
const an = analyze(scene, bound)
ok('two on-relations merge two pairs of coordinates', an.classes === 4, an.classes)
ok('so the scene has 4 degrees of freedom, not 6',
   degreesOfFreedom(scene, bound).free === 4, degreesOfFreedom(scene, bound).free)
ok("the endpoint's PRICE is now the h-line's price",
   an.rep.get('dw3:a:p') === an.rep.get('dw1:line:p'), '')
ok("but its TIME is still its own — this is the slide",
   an.rep.get('dw3:a:i') !== an.rep.get('dw1:line:p'), '')
ok('an on-relation pins nothing at all', an.pinned.size === 0, an.pinned.size)

// KADE'S POINT 5: "the locking two lines together must mean that at least one
// line's actual units are not locked or nothing can move." Lock BOTH h-lines
// and the only freedom left is the two endpoint times — the diagonal can slide
// but not change shape. The badge has to be able to say that.
const bothLocked = [...bound, lock('cnA', 'dw1'), lock('cnB', 'dw2')]
ok('locking both hosts leaves only the endpoint times free',
   degreesOfFreedom(scene, bothLocked).free === 2,
   degreesOfFreedom(scene, bothLocked).free)
// A lock must reach THROUGH the equality, in BOTH directions. Locking the host
// is the easy direction (the host's slot is the class representative, so even a
// broken implementation looks right); locking the ENDPOINT is the one that only
// works if the pin is recorded against the class rather than the raw slot.
const reach = analyze(scene, bothLocked)
ok('a lock on the h-line pins the endpoint held to it',
   reach.pinned.has(reach.rep.get('dw3:a:p')), '')
const endLocked = [...bound, lockAxis('cnC', 'dw3', 'a', 'price')]
const reachBack = analyze(scene, endLocked)
ok('and locking the ENDPOINT pins the h-line it rides',
   reachBack.pinned.has(reachBack.rep.get('dw1:line:p')), '')
ok('so that lock removes exactly one degree of freedom',
   degreesOfFreedom(scene, endLocked).free === 3,
   degreesOfFreedom(scene, endLocked).free)

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
b.drawings.push(hline('dw1', 600), hline('dw2', 604), trend('dw3'))
e.addConstraint({ kind: 'on', a: { id: 'dw3', part: 'a' }, b: { id: 'dw1', part: 'line' } })
ok('an on-relation is admitted', b.constraints.length === 1, JSON.stringify(b.constraints))
const dup = e.addConstraint({ kind: 'on', a: { id: 'dw3', part: 'a' }, b: { id: 'dw1', part: 'line' } })
ok('and the same one twice is refused as redundant',
   dup.ok === false && dup.issue.code === 'duplicate', JSON.stringify(dup))

// AN 'on' MUST NOT BLOCK A DRAG. Treating any constraint as a brake would
// forbid exactly the motion the feature exists to allow.
const canDrag = e.movableIds(['dw1'])
ok('a line with something held onto it still drags',
   canDrag.ids.join() === 'dw1' && canDrag.issue === null, JSON.stringify(canDrag))
const pointDrag = e.movableIds(['dw3'])
ok('and so does the trend that is held to it',
   pointDrag.ids.join() === 'dw3' && pointDrag.issue === null, JSON.stringify(pointDrag))

// THE STRETCH, end to end: move the h-line, the attached endpoint follows in
// PRICE and stays put in TIME.
const t0 = b.drawings[2].points[0].time
b.drawings[0].points[0].price = 590
propagate(b.drawings, b.constraints, new Set(['dw1']))
ok('dragging the h-line carries the endpoint held to it',
   b.drawings[2].points[0].price === 590, b.drawings[2].points[0].price)
ok('and does NOT move it in time — the trend stretches, it does not slide',
   b.drawings[2].points[0].time === t0, b.drawings[2].points[0].time)
ok('the far endpoint, held to nothing, is untouched',
   b.drawings[2].points[1].price === 604, b.drawings[2].points[1].price)
// Both members of one class moved at once (a multi-selection drag). They must
// END UP EQUAL: skipping the moved slots would leave the equality false.
b.drawings[0].points[0].price = 575
b.drawings[2].points[0].price = 999
propagate(b.drawings, b.constraints, new Set(['dw1', 'dw3']))
ok('dragging both ends of one equality leaves them agreeing',
   b.drawings[0].points[0].price === b.drawings[2].points[0].price,
   `${b.drawings[0].points[0].price} vs ${b.drawings[2].points[0].price}`)

// ---- lock, and the single toggle ------------------------------------------
e.selected = ['dw2']
e.toggleLockSelected()
ok('L locks the selection', e.getState().lockedIds.join() === 'dw2', e.getState().lockedIds.join())
e.toggleLockSelected()
ok('and L again unlocks it — one key, both directions',
   e.getState().lockedIds.length === 0, JSON.stringify(e.getState().lockedIds))
e.toggleLockSelected()
const lockedDrag = e.movableIds(['dw2'])
ok('a locked drawing cannot be dragged', lockedDrag.ids.length === 0, JSON.stringify(lockedDrag.ids))
ok('and the refusal names it in words',
   lockedDrag.issue && lockedDrag.issue.code === 'blocked' && /locked/i.test(lockedDrag.issue.message),
   JSON.stringify(lockedDrag.issue))
const partly = e.movableIds(['dw2', 'dw1'])
ok('a mixed selection still moves what it can',
   partly.ids.join() === 'dw1' && partly.issue === null, JSON.stringify(partly))

// ---- per-coordinate locks, which is what the editor's padlocks store -------
const eB = mkEngine('SLOT|1Day')
const bB = eB.bucket()
bB.drawings.push(trend('dw5'))
ok('a trend starts with four free coordinates', eB.getState().dof.free === 4,
   JSON.stringify(eB.getState().dof))
const setPrice = eB.setSlotValue({ id: 'dw5', part: 'a', axis: 'price' }, 612.5)
ok('typing a price sets it', setPrice.ok === true && bB.drawings[0].points[0].price === 612.5,
   bB.drawings[0].points[0].price)
ok('and locks that one coordinate only', eB.getState().dof.free === 3,
   JSON.stringify(eB.getState().dof))
ok("so the same point's DATE is still free",
   bB.constraints.length === 1 && bB.constraints[0].a.axis === 'price',
   JSON.stringify(bB.constraints))
// Re-typing your own locked value moves it; it does not refuse itself.
ok('re-typing a value you locked yourself just moves it',
   eB.setSlotValue({ id: 'dw5', part: 'a', axis: 'price' }, 620).ok === true &&
     bB.drawings[0].points[0].price === 620, bB.drawings[0].points[0].price)
eB.clearSlotLock({ id: 'dw5', part: 'a', axis: 'price' })
ok('and the padlock comes off again', eB.getState().dof.free === 4,
   JSON.stringify(eB.getState().dof))

// THE NOTIFICATION KADE ASKED FOR: a value held through an equality by someone
// else's lock refuses, and names the holder.
const eC = mkEngine('HELD|1Day')
const bC = eC.bucket()
bC.drawings.push(hline('dw6', 600), trend('dw7'))
eC.addConstraint({ kind: 'on', a: { id: 'dw7', part: 'a' }, b: { id: 'dw6', part: 'line' } })
eC.selected = ['dw6']
eC.toggleLockSelected()
const refused = eC.setSlotValue({ id: 'dw7', part: 'a', axis: 'price' }, 555)
ok('a coordinate held by another object\'s lock refuses to be set',
   refused.ok === false && refused.issue.code === 'blocked', JSON.stringify(refused))
ok('and the refusal names what is holding it',
   refused.issue && /hline/.test(refused.issue.message), refused.issue && refused.issue.message)
ok('and nothing moved', bC.drawings[1].points[0].price === 600, bC.drawings[1].points[0].price)

// ---- a DRIVEN SLOPE: the first constraint that is not an equality ---------
// Kade: "lock the trend angle / price action per time so when changing a
// horizontal or vertical line's price the trend line stays the same but follows
// the anchor line without changing the slope."
//
// Real bars, because the solve converts price-per-HOUR to price-per-BAR through
// the timeframe: on 1Day, one bar is 390 chart-minutes = 6.5 hours.
const day = (i) => ({ ts: new Date(Date.UTC(2024, 0, 2 + i, 14, 30)).toISOString() })
const bars = Array.from({ length: 40 }, (_, i) => day(i))
const secOf = (i) => Math.floor(Date.parse(bars[i].ts) / 1000)

const eS = mkEngine('SLOPE|1Day')
eS.barsOpt = () => bars
const bS = eS.bucket()
bS.drawings.push(
  { id: 'h1', kind: 'hline', points: [{ time: secOf(0), price: 600 }] },
  { id: 'h2', kind: 'hline', points: [{ time: secOf(0), price: 604 }] },
  { id: 'tr', kind: 'trend',
    points: [{ time: secOf(10), price: 600 }, { time: secOf(18), price: 604 }] }
)
eS.addConstraint({ kind: 'on', a: { id: 'tr', part: 'a' }, b: { id: 'h1', part: 'line' } })
eS.addConstraint({ kind: 'on', a: { id: 'tr', part: 'b' }, b: { id: 'h2', part: 'line' } })

// $4 over 8 bars x 6.5 chart-hours = 52 hours -> 0.076923 $/h, i.e. $0.50/bar.
const s0 = eS.slopeOf('tr')
ok('the live slope is price per hour of CHART time',
   Math.abs(s0 - 4 / 52) < 1e-9, s0)
ok('two on-relations leave 4 degrees of freedom',
   eS.getState().dof.free === 4, JSON.stringify(eS.getState().dof))

eS.setSlopeLock('tr', s0)
ok('driving the slope removes one more degree of freedom',
   eS.getState().dof.free === 3, JSON.stringify(eS.getState().dof))

// THE ASK: move an h-line and the trend keeps its slope, running the far end
// out to match rather than tilting.
bS.drawings[1].points[0].price = 610          // h2 dragged +$6 (was 604)
propagate(bS.drawings, bS.constraints, new Set(['h2']))
const iss = eS.restoreSlopes(new Set(['h2']))
ok('the endpoint on the dragged line follows it',
   bS.drawings[2].points[1].price === 610, bS.drawings[2].points[1].price)
ok('the SLOPE is unchanged', Math.abs(eS.slopeOf('tr') - s0) < 1e-9, eS.slopeOf('tr'))
// $10 gap at $0.50/bar = 20 bars, so the far end runs from bar 18 to bar 30.
ok('and the far end ran out to match — bar 10 to bar 30',
   bS.drawings[2].points[1].time === secOf(30),
   `${(bS.drawings[2].points[1].time - secOf(0)) / 86400} days in`)
ok('while the near end, the one drawn first, is the anchor',
   bS.drawings[2].points[0].time === secOf(10), '')
ok('and the untouched h-line never moved',
   bS.drawings[0].points[0].price === 600, bS.drawings[0].points[0].price)
ok('a clean solve reports nothing', iss === null, JSON.stringify(iss))

// A slope that falls BETWEEN bars is realised on the nearest one and SAID so —
// never silently rounded into a different number than the chip shows.
bS.drawings[1].points[0].price = 610.25
propagate(bS.drawings, bS.constraints, new Set(['h2']))
const quant = eS.restoreSlopes(new Set(['h2']))
ok('a slope between bars is reported, not silently rounded',
   quant !== null && quant.code === 'quantized', JSON.stringify(quant))

// BLOCKED: pin everything the solve could use and it must refuse in words.
const eT = mkEngine('BLOCK|1Day')
eT.barsOpt = () => bars
const bT = eT.bucket()
bT.drawings.push(
  { id: 'h1', kind: 'hline', points: [{ time: secOf(0), price: 600 }] },
  { id: 'tr', kind: 'trend',
    points: [{ time: secOf(10), price: 600 }, { time: secOf(18), price: 604 }] }
)
eT.addConstraint({ kind: 'on', a: { id: 'tr', part: 'a' }, b: { id: 'h1', part: 'line' } })
eT.setSlopeLock('tr', 0.5)
eT.addConstraint({ kind: 'lock', a: { id: 'tr', part: 'b' } })
eT.addConstraint({ kind: 'lock', a: { id: 'tr', part: 'a', axis: 'time' } })
const stuck = eT.restoreSlopes(new Set(['h1']))
ok('with nothing free to absorb it, the slope lock refuses in words',
   stuck !== null && stuck.code === 'blocked' && /slope/i.test(stuck.message),
   JSON.stringify(stuck))

// A flat slope is a legal system, not an error: the index terms vanish, so a
// PRICE satisfies it. Nothing in the solve divides by the slope.
const eZ = mkEngine('ZERO|1Day')
eZ.barsOpt = () => bars
const bZ = eZ.bucket()
bZ.drawings.push({ id: 'tr', kind: 'trend',
  points: [{ time: secOf(4), price: 600 }, { time: secOf(12), price: 604 }] })
eZ.setSlopeLock('tr', 0)
ok('slope 0 is solved, not refused or divided by',
   bZ.drawings[0].points[1].price === bZ.drawings[0].points[0].price,
   `${bZ.drawings[0].points[0].price} vs ${bZ.drawings[0].points[1].price}`)
ok('and it left the times alone', bZ.drawings[0].points[1].time === secOf(12), '')

// ---- dangling constraints are pruned by commit() --------------------------
bC.drawings = bC.drawings.filter((d) => d.id !== 'dw6')
eC.commit()
ok('deleting a drawing takes every constraint naming it',
   bC.constraints.length === 0, JSON.stringify(bC.constraints))

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
    assert len(results) >= 54, f"the probe lost assertions: only {len(results)} ran"


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
