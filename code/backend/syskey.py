"""The system key: one broker credential the recorder can use with nobody logged in.

The user's TRADING keys live in the password-derived vault (`security.py`) and
are not touched by anything here. This is a different credential with a
different job and a different protection, chosen to match its blast radius:
because a system key should be a paper or data-scoped key that **cannot move
real money**, it may be sealed to the Windows account instead of to a password
the user is not present to type.

**This module must never reach the vault.** It imports nothing from
`security`, `sessions` or `market`, and it holds no DEK. That is not style — a
background process that could unwrap the DEK would hold the keys to live
trading credentials, which is the exact inversion of this feature's premise.
`REQUIREMENTS.md` §6.6 sanctions a DPAPI-wrapped *DEK* for a separate
"skip password on this machine" convenience; **that blob must never be reused
here**, which is why this one carries its own version tag and its own entropy.

WHERE. `%LOCALAPPDATA%\\Grindstone\\system-key.bin`, never `data_dir()`. The
property that a stolen blob is useless on another machine holds *because*
`%LOCALAPPDATA%` does not roam while the DPAPI master key lives in roaming
`%APPDATA%`. Moving this file "for symmetry" would quietly destroy that
guarantee with every test still passing, so the gate asserts the path.

WHO CAN READ IT. Any process running as this Windows user. The entropy constant
is not a barrier — the app ships unpacked, so `strings` recovers it. §7.9
already concedes same-user malware and claims resistance only to a stolen file
and to other local accounts; both of those properties hold here. Nothing more
is claimed.

WHY THERE IS NO INNER ENCRYPTION LAYER. An earlier design sealed the payload
with AES-GCM under a key stored *inside the same blob*. That adds no
confidentiality — the key travels with the ciphertext — and it would read to a
future maintainer as protection that is not there. It was proposed for
deterministic tamper detection, but DPAPI was measured refusing every corrupted
blob on this platform, and a version field in the payload covers entropy
rotation just as well. Plain, versioned JSON under DPAPI is what this is.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import os
import sys
from pathlib import Path
from typing import Any

from .logs import LOG

#: Bumped only if the wrapping scheme changes. A blob whose version this build
#: does not understand is refused and re-enrolment is requested — never guessed.
FORMAT = 1

#: Domain-separates this blob from every other DPAPI blob the app might ever
#: write, so a mix-up cannot silently decrypt the wrong one.
_ENTROPY = b"grindstone/system-key/v1"

_UI_FORBIDDEN = 0x01
#: NEVER PASSED. Machine scope would let every local account on this PC decrypt
#: the key, and it round-trips perfectly for whoever set it — so the mistake is
#: invisible in testing. Present only so the gate can assert it is unused.
_LOCAL_MACHINE = 0x04


class SystemKeyUnavailable(Exception):
    """The key cannot be read. Callers treat this as a normal state."""


# --------------------------------------------------------------- placement
def key_dir() -> Path:
    """`%LOCALAPPDATA%\\Grindstone`, created on demand.

    Deliberately NOT derived from `db.data_dir()`: that resolves inside the
    Drive-synced project tree and is overridable by an env var, either of which
    would put this file somewhere a sync client copies it off the machine."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    if not base:
        base = str(Path.home() / ".local" / "state")
    d = Path(base) / "Grindstone"
    d.mkdir(parents=True, exist_ok=True)
    return d


def key_path() -> Path:
    return key_dir() / "system-key.bin"


def meta_path() -> Path:
    """Secret-free metadata the UI can read without unwrapping anything."""
    return key_dir() / "system-key.json"


# ------------------------------------------------------------------- DPAPI
class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]

    @classmethod
    def of(cls, data: bytes) -> "_BLOB":
        buf = ctypes.create_string_buffer(data, len(data))
        return cls(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    def take(self) -> bytes:
        out = ctypes.string_at(self.pbData, self.cbData)
        ctypes.windll.kernel32.LocalFree(self.pbData)
        return out


def available() -> bool:
    """Whether this platform can seal a key to the logged-on user."""
    return sys.platform == "win32"


def virtualized() -> bool:
    """True when this interpreter's view of %LOCALAPPDATA% may be REDIRECTED.

    The Microsoft Store build of Python carries package identity, and Windows
    silently rewrites its %LOCALAPPDATA% access into
    ``…\\Packages\\PythonSoftwareFoundation.Python.<v>\\LocalCache\\Local``.
    Two processes running *the same* `sys.executable` can therefore see two
    different directories, depending on how each was launched — measured on
    this machine: the sidecar spawned by Electron wrote the real path, while
    the same interpreter started from a shell saw an empty one and reported
    'not enrolled' for a key that existed.

    That is survivable for the app, which reads and writes through one process.
    It is NOT survivable for a scheduled-task recorder, which is a different
    process that must find a key some other process wrote — a coin flip on
    whether it looks in the same place.

    Shipping builds are PyInstaller, which has no package identity and no
    redirection, so this is a development-configuration hazard. It is surfaced
    rather than worked around because silently writing a credential to a path
    that is not the one reported is exactly how an hour disappears."""
    return "WindowsApps" in sys.base_prefix or "\\Packages\\" in sys.prefix


def _protect(plaintext: bytes) -> bytes:
    out = _BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(_BLOB.of(plaintext)),
        None,                      # szDataDescr — MUST stay None: the
                                   # description is stored in CLEARTEXT inside
                                   # the blob (measured), so any label here
                                   # would advertise what the file is.
        ctypes.byref(_BLOB.of(_ENTROPY)),
        None, None,
        _UI_FORBIDDEN,             # and never _LOCAL_MACHINE
        ctypes.byref(out))
    if not ok:
        raise SystemKeyUnavailable("Windows refused to seal the key")
    return out.take()


def _unprotect(blob: bytes) -> bytes:
    out = _BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(_BLOB.of(blob)), None,
        ctypes.byref(_BLOB.of(_ENTROPY)),
        None, None, _UI_FORBIDDEN, ctypes.byref(out))
    if not ok:
        # Several distinct causes collapse to the same error code — a Windows
        # password RESET by an administrator, a profile copied to another
        # machine, a reinstall, AV quarantine, a partial write. Microsoft does
        # not promise the codes are stable, so this does not branch on them:
        # the honest answer to all of them is the same, and it is recoverable
        # by re-entering the key.
        raise SystemKeyUnavailable(
            "the stored system key could not be unsealed on this machine — "
            "enter it again (a Windows password reset or moving the profile "
            "to another PC both do this)")
    return out.take()


# ------------------------------------------------------------------- store
def store(key_id: str, secret_key: str, verdict: str, enrolled_at: str) -> None:
    """Seal the key. Overwrites any previous one."""
    if not available():
        raise SystemKeyUnavailable(
            "unattended recording needs Windows; on this platform sign in and "
            "let the recorder run attended")
    if not key_id or not secret_key:
        raise SystemKeyUnavailable("both the key id and the secret are required")
    payload = json.dumps({
        "format": FORMAT, "broker": "alpaca",
        "key_id": key_id, "secret_key": secret_key,
        "verdict": verdict, "enrolled_at": enrolled_at,
    }).encode("utf-8")
    blob = _protect(payload)
    # Write-then-replace so a crash mid-write cannot leave a truncated blob
    # that reads as tampering.
    tmp = key_path().with_suffix(".tmp")
    tmp.write_bytes(blob)
    os.replace(tmp, key_path())
    meta_path().write_text(json.dumps({
        "broker": "alpaca", "verdict": verdict, "enrolled_at": enrolled_at,
        "key_id_last4": key_id[-4:] if len(key_id) >= 8 else "",
    }, indent=1), encoding="utf-8")
    LOG.info("system key enrolled (%s), sealed at %s", verdict, key_path())


def load() -> dict[str, Any] | None:
    """The sealed key, or None. NEVER raises for the ordinary failures — a
    missing or unreadable key is a state the caller reports, not a crash."""
    p = key_path()
    if not p.is_file():
        return None
    try:
        data = json.loads(_unprotect(p.read_bytes()).decode("utf-8"))
    except SystemKeyUnavailable as e:
        LOG.warning("system key unreadable: %s", e)
        return None
    except Exception:  # noqa: BLE001 — corrupt JSON is the same user-facing state
        LOG.warning("system key payload was unreadable", exc_info=True)
        return None
    if data.get("format") != FORMAT:
        LOG.warning("system key format %r is not %r — re-enrolment needed",
                    data.get("format"), FORMAT)
        return None
    if not data.get("key_id") or not data.get("secret_key"):
        return None
    return data


def status() -> dict[str, Any]:
    """What the UI shows. Reads the metadata file, and only unseals to answer
    'can it still be read on this machine' — which is the question that
    actually matters after a password reset."""
    p = key_path()
    if not p.is_file():
        return {"enrolled": False, "readable": False, "platform_ok": available(),
                "virtualized": virtualized()}
    meta: dict[str, Any] = {}
    if meta_path().is_file():
        try:
            meta = json.loads(meta_path().read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            meta = {}
    return {"enrolled": True, "readable": load() is not None,
            "platform_ok": available(), "virtualized": virtualized(),
            "path": str(p), **meta}


def remove() -> bool:
    had = key_path().is_file()
    key_path().unlink(missing_ok=True)
    meta_path().unlink(missing_ok=True)
    if had:
        LOG.info("system key removed")
    return had
