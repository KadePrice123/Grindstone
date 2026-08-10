"""The notepad: every data point the user (or the agent) has grabbed.

The holding area for the Get/Post primitive (docs/DATA_EXCHANGE.md). Lives in
the BACKEND because the backend is the only component both app instances see —
a pad held in renderer or main-process state is invisible to the agent, and
vice versa. Agent grabs land here too, stamped `workspace: 'agent'`, visible
to the user like everything else.

Three rules carry the design:

**Validated on EVERY write, including edits.** An edited payload must still be
the typed thing its kind claims — a chain with rows removed is still a chain —
or every post target downstream breaks on data that was fine when grabbed.

**Secrets cannot enter.** The notepad is read by the agent BY DESIGN, which
makes it an exfiltration path if a credential ever lands in it. The scrub here
is the layer a CDP-driven agent cannot bypass; the renderer-side allowlists
are belts (DATA_EXCHANGE.md §5).

**Caps refuse loudly.** 32 entries, 256 KB each. Over-cap says the count;
silent eviction would make "where did my grab go" a support question.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from typing import Any

MAX_ENTRIES = 32
MAX_ENTRY_BYTES = 256 * 1024
MAX_LABEL = 80
MAX_NOTE_CHARS = 20_000

KINDS = ("chart-doc", "drawing", "leg", "chain", "contract",
         "form", "backtest-spec", "note")

WORKSPACES = ("user", "agent")

#: Credential-shaped strings that must never enter the pad. The Alpaca key
#: shapes are the ones this app actually handles; the generic assignments
#: catch pasted env files. Deliberately matched against the WHOLE serialized
#: payload, not per-field — a secret hiding in a note is still a secret.
_SECRET_SHAPES = (
    re.compile(r"\bPK[A-Z0-9]{16,}\b"),            # Alpaca live/paper key id
    re.compile(r"\bAK[A-Z0-9]{16,}\b"),
    # `secret[_-]?key` spelled out: \b(secret)\b never matches "secret_key"
    # because the underscore is a word character — the gate caught exactly
    # that miss before this shipped.
    re.compile(r"(?i)(secret[_-]?key|\bsecret\b|api[_-]?key|password|token)"
               r"\s*[:=]\s*\S{16,}"),
)


class NotepadError(ValueError):
    """Refused, with the reason. Routes surface it as a 422."""


def _fail(msg: str) -> None:
    raise NotepadError(msg)


# ------------------------------------------------------------------ schema
SCHEMA = """
CREATE TABLE IF NOT EXISTS notepad (
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    id       TEXT NOT NULL,
    payload  TEXT NOT NULL,
    label    TEXT NOT NULL DEFAULT '',
    added_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (user_id, id)
);
"""


# --------------------------------------------------------------- validation
def _require(cond: bool, msg: str) -> None:
    if not cond:
        _fail(msg)


def _clean_provenance(p: Any) -> dict[str, Any]:
    _require(isinstance(p, dict), "payload has no provenance object")
    ws = p.get("workspace")
    _require(ws in WORKSPACES,
             f"provenance.workspace must be one of {WORKSPACES}, got {ws!r}")
    _require(isinstance(p.get("capturedAt"), str) and p["capturedAt"],
             "provenance.capturedAt is required — a payload describes "
             "capture-time state and must say when that was")
    out = {"workspace": ws, "capturedAt": p["capturedAt"]}
    for opt in ("page", "key", "symbol", "timeframe", "axis", "user"):
        v = p.get(opt)
        if v is not None:
            _require(isinstance(v, str) and len(v) <= 120,
                     f"provenance.{opt} must be a short string")
            out[opt] = v
    return out


def _clean_contract_row(r: Any, where: str) -> dict[str, Any]:
    """One contract, all 13 backend fields. The frontend interfaces carry 9;
    validating against the BACKEND envelope is what stops four greeks that
    are already on the wire from silently vanishing (DATA_EXCHANGE.md §2)."""
    _require(isinstance(r, dict), f"{where}: contract must be an object")
    _require(isinstance(r.get("occ_symbol"), str) and r["occ_symbol"],
             f"{where}: contract has no occ_symbol")
    _require(isinstance(r.get("expiration"), str),
             f"{where}: contract has no expiration")
    _require(isinstance(r.get("strike"), (int, float)),
             f"{where}: contract has no strike")
    _require(r.get("right") in ("C", "P"),
             f"{where}: contract right must be C or P")
    out = {k: r[k] for k in ("occ_symbol", "expiration", "strike", "right")}
    for f in ("bid", "ask", "last", "iv", "delta", "gamma", "theta",
              "vega", "rho", "mid", "annual", "dte"):
        v = r.get(f)
        if v is not None:
            _require(isinstance(v, (int, float)), f"{where}: {f} must be numeric")
            out[f] = v
    return out


def _clean_data(kind: str, data: Any) -> Any:
    """Per-kind structural validation. Deliberately SHALLOW for chart docs —
    chartobjects.validate is the authority on that shape and runs when the
    payload is ever posted back to a chart; duplicating its 300 lines here
    would give the two validators a chance to disagree."""
    _require(isinstance(data, dict), "payload.data must be an object")
    if kind == "note":
        text = data.get("text")
        _require(isinstance(text, str), "a note has a text field")
        _require(len(text) <= MAX_NOTE_CHARS,
                 f"note is {len(text)} chars, over the {MAX_NOTE_CHARS} cap")
        return {"text": text}
    if kind == "chain":
        rows = data.get("contracts")
        _require(isinstance(rows, list) and rows,
                 "a chain payload carries a non-empty contracts list")
        clean_rows = [_clean_contract_row(r, f"contracts[{i}]")
                      for i, r in enumerate(rows)]
        out = {"contracts": clean_rows}
        for f in ("underlying", "source", "reason"):
            if isinstance(data.get(f), str):
                out[f] = data[f]
        if isinstance(data.get("query"), dict):
            out["query"] = data["query"]
        return out
    if kind == "contract":
        return _clean_contract_row(data, "contract")
    if kind in ("chart-doc", "drawing", "leg", "form", "backtest-spec"):
        # Structural floor only (see docstring). Forms additionally promise
        # scalar values — the adapter allowlist enforces field CHOICE, this
        # enforces field SHAPE.
        if kind == "form":
            _require(isinstance(data.get("formKind"), str) and data["formKind"],
                     "a form payload names its formKind")
            vals = data.get("values")
            _require(isinstance(vals, dict), "a form payload carries values")
            for k, v in vals.items():
                _require(isinstance(v, (str, int, float, bool)) or v is None,
                         f"form value {k!r} is not a scalar — nested structures "
                         f"do not round-trip a form")
        return data
    _fail(f"unknown payload kind {kind!r}")


def validate_payload(payload: Any) -> dict[str, Any]:
    _require(isinstance(payload, dict), "payload must be an object")
    _require(payload.get("v") == 1, f"unknown payload version {payload.get('v')!r}")
    kind = payload.get("kind")
    _require(kind in KINDS, f"kind must be one of {KINDS}, got {kind!r}")
    clean = {
        "v": 1,
        "kind": kind,
        "data": _clean_data(kind, payload.get("data")),
        "provenance": _clean_provenance(payload.get("provenance")),
    }
    blob = json.dumps(clean)
    _require(len(blob) <= MAX_ENTRY_BYTES,
             f"payload is {len(blob)} bytes, over the {MAX_ENTRY_BYTES} cap")
    for shape in _SECRET_SHAPES:
        m = shape.search(blob)
        if m:
            _fail("payload contains a credential-shaped string and was refused "
                  "— the notepad is readable by the agent by design, so a "
                  "secret stored here is a secret exported")
    return clean


# ------------------------------------------------------------------- store
def list_entries(db: sqlite3.Connection, user_id: int) -> list[dict[str, Any]]:
    """Newest first. Full payloads — the notepad page renders from this, and
    32×256KB has a worst case the IPC layer already tolerates for charts."""
    rows = db.execute(
        "SELECT id, payload, label, added_at FROM notepad WHERE user_id=?"
        " ORDER BY added_at DESC", (user_id,)).fetchall()
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload"])
        except ValueError:
            continue  # a corrupt row is skipped, never a crash
        out.append({"id": r["id"], "payload": payload,
                    "label": r["label"], "added_at": r["added_at"]})
    return out


def summaries(db: sqlite3.Connection, user_id: int) -> list[dict[str, Any]]:
    """What the post wheel is built from: id, kind, label — never payloads.
    Wheel segments are deliberately payload-free."""
    return [{"id": e["id"], "kind": e["payload"]["kind"],
             "label": e["label"] or _default_label(e["payload"])}
            for e in list_entries(db, user_id)]


def _default_label(p: dict[str, Any]) -> str:
    kind, d, prov = p["kind"], p["data"], p["provenance"]
    sym = prov.get("symbol") or d.get("underlying") or ""
    if kind == "chain":
        return f"chain {sym} ×{len(d['contracts'])}".strip()
    if kind == "contract":
        return f"{d['strike']:g}{d['right']} {d['expiration'][5:]}"
    if kind == "note":
        return (d["text"].strip().splitlines() or ["note"])[0][:32] or "note"
    return f"{kind} {sym}".strip()


def add(db: sqlite3.Connection, user_id: int, payload: Any,
        label: str = "") -> dict[str, Any]:
    clean = validate_payload(payload)
    n = db.execute("SELECT COUNT(*) FROM notepad WHERE user_id=?",
                   (user_id,)).fetchone()[0]
    if n >= MAX_ENTRIES:
        _fail(f"the notepad holds {n} of {MAX_ENTRIES} entries — remove some "
              f"first (nothing is evicted silently)")
    entry_id = uuid.uuid4().hex[:12]
    db.execute(
        "INSERT INTO notepad (user_id, id, payload, label) VALUES (?,?,?,?)",
        (user_id, entry_id, json.dumps(clean), str(label)[:MAX_LABEL]))
    return {"id": entry_id, "payload": clean, "label": str(label)[:MAX_LABEL]}


def get(db: sqlite3.Connection, user_id: int, entry_id: str) -> dict[str, Any] | None:
    r = db.execute("SELECT id, payload, label, added_at FROM notepad"
                   " WHERE user_id=? AND id=?", (user_id, entry_id)).fetchone()
    if r is None:
        return None
    try:
        payload = json.loads(r["payload"])
    except ValueError:
        return None
    return {"id": r["id"], "payload": payload, "label": r["label"],
            "added_at": r["added_at"]}


def edit(db: sqlite3.Connection, user_id: int, entry_id: str,
         payload: Any = None, label: str | None = None) -> dict[str, Any]:
    """Edit revalidates — the whole point. A chain with rows removed is still
    a chain; a chain edited into nonsense is refused with the reason."""
    existing = get(db, user_id, entry_id)
    if existing is None:
        _fail(f"no notepad entry {entry_id!r}")
    new_payload = existing["payload"] if payload is None else validate_payload(payload)
    if payload is not None:
        _require(new_payload["kind"] == existing["payload"]["kind"],
                 f"an edit cannot change the kind ({existing['payload']['kind']} "
                 f"→ {new_payload['kind']}) — grab the other thing instead")
    new_label = existing["label"] if label is None else str(label)[:MAX_LABEL]
    db.execute("UPDATE notepad SET payload=?, label=? WHERE user_id=? AND id=?",
               (json.dumps(new_payload), new_label, user_id, entry_id))
    return {"id": entry_id, "payload": new_payload, "label": new_label}


def remove(db: sqlite3.Connection, user_id: int, entry_id: str) -> bool:
    cur = db.execute("DELETE FROM notepad WHERE user_id=? AND id=?",
                     (user_id, entry_id))
    return (cur.rowcount or 0) > 0
