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
        if rel.endswith("selftest.py"):
            continue  # this file names the patterns it hunts
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

    blob = sec.encrypt_secret(dek, "PKTESTFAKEKEYVALUE00", 1, 7, "key_id")
    assert sec.decrypt_secret(dek, blob, 1, 7, "key_id") == "PKTESTFAKEKEYVALUE00"
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

    fake_key = "PKFAKEFAKEFAKEFAKE1234"          # realistic shape, not a real key
    fake_secret = "sEcReTfAkEsEcReTfAkEsEcReTfAkE12345"

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
            "credentials": {"key_id": fake_key, "secret_key": fake_secret}})
        assert r.status_code == 200, r.text
        listed = client.get("/api/accounts", headers=A).json()
        assert listed[0]["key_hints"]["key_id"] == fake_key[-4:]
        assert fake_secret not in json.dumps(listed)

        raw = (Path(tmp) / "app.db").read_bytes()
        assert fake_key.encode() not in raw, "plaintext key id in DB file"
        assert fake_secret.encode() not in raw, "plaintext secret in DB file"

        # unsupported broker degrades honestly, offline
        r = client.post("/api/accounts/test", headers=A,
                        json={"broker": "fidelity", "kind": "live", "credentials": {}})
        assert r.json()["ok"] is False and "Fidelity" in r.json()["error"]

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


@check("sessions: idle expiry and revocation wipe")
def _sessions():
    sys.path.insert(0, str(CODE))
    from backend.sessions import SessionStore

    s = SessionStore(idle_seconds=0)          # everything is instantly stale
    tok = s.create(1, "t", b"\x01" * 32)
    import time as _t
    _t.sleep(0.01)
    assert s.get(tok) is None, "stale session survived"

    s2 = SessionStore()
    t2 = s2.create(1, "t", b"\x02" * 32)
    entry = s2._by_token[t2]
    s2.revoke(t2)
    assert bytes(entry.dek) == b"\x00" * 32, "DEK not wiped on revoke"


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
