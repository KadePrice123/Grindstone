"""Offline verification gate for the dashboard project (M0 scope).

Grows with each milestone; the sentinel count must be bumped whenever a check
is added so a crash mid-run can never look like a pass.
Run: python selftest.py   (from code/)
"""
from __future__ import annotations

import json
import re
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


@check("env template carries key names only, no values")
def _env_example():
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = line.strip().lstrip("#").strip()
        if "=" in line:
            _, _, value = line.partition("=")
            assert len(value.strip()) == 0 or value.strip().startswith("https://") or value.strip() in ("sandbox",), \
                f".env.example line carries a value: {line!r}"


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


@check("ipc proxy: canonical paths only, and tokens scrubbed by default")
def _ipc_invariants():
    src = (CODE / "app" / "src" / "main" / "api.ts").read_text(encoding="utf-8")
    # A path that is not already canonical must be rejected, not rewritten —
    # otherwise '/api/auth/login?x=1' reaches the same route while dodging
    # our token handling (review 2026-08-01, high).
    assert "url.pathname !== path" in src, "IPC proxy does not enforce canonical paths"
    assert "redirect: 'error'" in src, "a 307 could silently change the routed path"
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


@check("frontend: sources present; typecheck when toolchain available")
def _frontend():
    app_dir = CODE / "app"
    for rel in ("package.json", "electron.vite.config.ts",
                "src/main/index.ts", "src/main/sidecar.ts", "src/main/api.ts",
                "src/preload/index.ts", "src/renderer/index.html",
                "src/renderer/src/App.tsx"):
        assert (app_dir / rel).exists(), f"missing app source {rel}"
    pkg = json.loads((app_dir / "package.json").read_text(encoding="utf-8"))
    assert pkg["name"] == "grindstone"

    tsc = app_dir / "node_modules" / "typescript" / "bin" / "tsc"
    node = CODE.parent.parent.parent / "runtimes" / "node" / "node.exe"
    if not (tsc.exists() and node.exists()):
        print("      (toolchain absent — file checks only; full typecheck needs node_modules)")
        return
    r = subprocess.run([str(node), str(tsc), "--noEmit"], cwd=app_dir,
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, f"tsc failed:\n{(r.stdout or r.stderr)[:1500]}"


def main() -> int:
    passed = 0
    total = len(CHECKS)
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
