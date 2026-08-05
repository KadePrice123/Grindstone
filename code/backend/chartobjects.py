"""Persisted chart drawings, measurements and inspect pins.

One row per (user, chart key), where the key is the engine's own bucket name —
"SPY|1Day|$". The key must carry the timeframe and the axis semantics for the
same reason the in-memory bucket does: a 1Min anchor is unprojectable on a 1Day
axis, and a $ anchor is meaningless on a % axis.

WHY ITS OWN TABLE, not the settings blob. `settings._coerce` caps a json
setting at 8192 bytes and silently substitutes the DEFAULT when it overflows —
for UI state that is the right call, but here it would delete a user's drawings
without a word the moment a chart got busy. A drawing set is a data store, and
this is the smallest table that says so. It is also keyed per chart, which the
one-row settings blob cannot express without an ever-growing nested object.

VALIDATION IS STRICT ON PURPOSE, and it costs something worth naming: the
vocabulary below duplicates ChartDraw.ts's model, so ADDING A DrawKind OR AN
ANCHOR KIND MEANS EDITING BOTH FILES. The gate enforces the agreement rather
than trusting it (selftest `_chart_persistence`). The alternative — waving any
JSON through — trades that edit for a renderer that throws on a doc it cannot
project, and a chart that will not draw is worse than a save that refuses.
"""
from __future__ import annotations

import json
import math
import sqlite3
from typing import Any

DOC_VERSION = 1

# The engine's vocabulary. Kept in lockstep with ChartDraw.ts by the gate.
DRAW_KINDS = ("trend", "hline", "vline", "circle")
ANCHOR_KINDS = ("candle", "line", "free")
PLACE_AXES = ("time", "price")
# 'lock' PINS a coordinate; 'on' EQUATES two of them and leaves both free to
# move together. Both are equalities, which is what lets the engine compute
# degrees of freedom with union-find instead of a matrix. Kinds that are NOT
# equalities (a typed slope or gap) arrive with the factorisation that can
# honour them, not before — accepting a constraint the engine silently ignores
# is a worse lie than not offering it.
CONSTRAINT_KINDS = ("lock", "on")
ENTITY_PARTS = ("line", "a", "b")

# How many points each kind carries. trend/circle are two-point (circle is
# centre + edge); hline/vline are one — the line IS one coordinate and the
# point only places its handle.
POINTS_FOR = {"trend": 2, "hline": 1, "vline": 1, "circle": 2}

MAX_KEY = 120
MAX_ID = 64
MAX_PER_KIND = 500          # per collection, per chart
MAX_DOC_BYTES = 256 * 1024  # ~30x the settings cap this table exists to escape


def _fail(msg: str) -> None:
    raise ValueError(msg)


def _num(v: Any, what: str) -> float:
    """A finite JSON number.

    Python's json module accepts AND emits NaN/Infinity, which are not legal
    JSON — a NaN price would round-trip through SQLite happily and then make
    the renderer's JSON.parse throw, taking out every chart for that key. The
    same NaN also projects to no pixel, so it is unusable even if it parsed.
    """
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        _fail(f"{what} must be a number, got {type(v).__name__}")
    f = float(v)
    if not math.isfinite(f):
        _fail(f"{what} must be finite, got {v!r}")
    return f


def _time(v: Any, what: str) -> int:
    """A UTCTimestamp — whole seconds. The engine's times are bar times and
    lightweight-charts indexes them as integers; a float would never match a
    bar and the anchor would silently degrade to unprojectable."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        _fail(f"{what} must be a timestamp, got {type(v).__name__}")
    f = float(v)
    if not math.isfinite(f) or f != int(f):
        _fail(f"{what} must be whole seconds, got {v!r}")
    # Epoch 0 is 1970 and no market data reaches it, so a non-positive time is
    # never a real bar — it is a placeholder that escaped. moveDimension used to
    # write `{axis:'time', at: 0}` before it knew the coordinate and then return
    # early over the right-hand whitespace, leaving a dimension pinned to 1970:
    # unprojectable, invisible, and once persistence landed, saved. That bug is
    # fixed in the engine; this is the door it should never have got through.
    if f <= 0:
        _fail(f"{what} must be a real bar time, got {v!r}")
    return int(f)


def _id(v: Any, what: str, seen: set[str]) -> str:
    if not isinstance(v, str) or not (1 <= len(v) <= MAX_ID):
        _fail(f"{what}: id must be 1-{MAX_ID} characters, got {v!r}")
    # Globally unique across all three collections — the engine's single mkId
    # counter guarantees it, and the selection is a flat string[] that would
    # otherwise pick two objects at once.
    if v in seen:
        _fail(f"{what}: duplicate id {v!r}")
    seen.add(v)
    return v


def _point(p: Any, what: str) -> dict[str, Any]:
    if not isinstance(p, dict):
        _fail(f"{what}: each point must be an object")
    return {"time": _time(p.get("time"), f"{what}.time"),
            "price": _num(p.get("price"), f"{what}.price")}


def _anchor(a: Any, what: str) -> dict[str, Any]:
    if not isinstance(a, dict):
        _fail(f"{what} must be an object")
    kind = a.get("kind")
    if kind not in ANCHOR_KINDS:
        _fail(f"{what}: unknown anchor kind {kind!r}")
    if kind == "candle":
        return {"kind": "candle", "time": _time(a.get("time"), f"{what}.time")}
    if kind == "free":
        return {"kind": "free", "time": _time(a.get("time"), f"{what}.time"),
                "price": _num(a.get("price"), f"{what}.price")}
    drawing_id = a.get("drawingId")
    if not isinstance(drawing_id, str) or not (1 <= len(drawing_id) <= MAX_ID):
        _fail(f"{what}.drawingId must be an id, got {drawing_id!r}")
    # time/price are the snap-moment snapshot the engine falls back to when the
    # measured line is later deleted or trimmed away, so they are required even
    # though the anchor normally resolves through drawingId.
    return {"kind": "line", "drawingId": drawing_id,
            "u": _num(a.get("u"), f"{what}.u"),
            "time": _time(a.get("time"), f"{what}.time"),
            "price": _num(a.get("price"), f"{what}.price")}


def _place(p: Any, what: str) -> dict[str, Any]:
    if not isinstance(p, dict):
        _fail(f"{what} must be an object")
    axis = p.get("axis")
    if axis not in PLACE_AXES:
        _fail(f"{what}: unknown axis {axis!r}")
    # `at` is the single shared coordinate, so its TYPE follows the axis: a
    # time-locked dimension sits at a bar time, a price-locked one at a price.
    at = (_time(p.get("at"), f"{what}.at") if axis == "time"
          else _num(p.get("at"), f"{what}.at"))
    return {"axis": axis, "at": at}


def _list(doc: dict[str, Any], name: str) -> list[Any]:
    items = doc.get(name)
    if items is None:
        return []
    if not isinstance(items, list):
        _fail(f"'{name}' must be a list")
    if len(items) > MAX_PER_KIND:
        _fail(f"'{name}': at most {MAX_PER_KIND} per chart, got {len(items)}")
    return items


def validate(doc: Any) -> dict[str, Any]:
    """Normalize and validate one chart's objects. Raises ValueError naming the
    actual reason — a silently 'fixed' document is how a user's work disappears
    without explanation."""
    if not isinstance(doc, dict):
        _fail("chart document must be an object")

    seen: set[str] = set()

    drawings: list[dict[str, Any]] = []
    for d in _list(doc, "drawings"):
        if not isinstance(d, dict):
            _fail("each drawing must be an object")
        kind = d.get("kind")
        if kind not in DRAW_KINDS:
            _fail(f"unknown drawing kind {kind!r}")
        did = _id(d.get("id"), f"drawing {kind}", seen)
        pts = d.get("points")
        if not isinstance(pts, list):
            _fail(f"drawing {did}: points must be a list")
        want = POINTS_FOR[kind]
        if len(pts) != want:
            _fail(f"drawing {did}: a {kind} needs {want} point(s), got {len(pts)}")
        drawings.append({"id": did, "kind": kind,
                         "points": [_point(p, f"drawing {did}") for p in pts]})

    measures: list[dict[str, Any]] = []
    for m in _list(doc, "measures"):
        if not isinstance(m, dict):
            _fail("each measure must be an object")
        mid = _id(m.get("id"), "measure", seen)
        out: dict[str, Any] = {"id": mid,
                               "a": _anchor(m.get("a"), f"measure {mid}.a"),
                               "b": _anchor(m.get("b"), f"measure {mid}.b")}
        # Absent place = the free diagonal. Absent and null mean the same
        # thing here: JSON has no 'undefined', so a renderer that spreads an
        # optional field sends null, and refusing it would reject its own
        # output.
        if m.get("place") is not None:
            out["place"] = _place(m["place"], f"measure {mid}.place")
        measures.append(out)

    pins: list[dict[str, Any]] = []
    for p in _list(doc, "pins"):
        if not isinstance(p, dict):
            _fail("each pin must be an object")
        pid = _id(p.get("id"), "pin", seen)
        pins.append({"id": pid, "time": _time(p.get("time"), f"pin {pid}.time")})

    # Constraints reference drawings by id, so unlike every other collection
    # they can DANGLE. The engine prunes on every commit; this refuses a dangling
    # reference at the door too, because a lock pointing at a deleted drawing is
    # invisible, unremovable through the UI, and still counts against the degrees
    # of freedom the user is shown.
    known = {d["id"] for d in drawings}

    def _ref(v: Any, what: str) -> dict[str, Any]:
        if not isinstance(v, dict):
            _fail(f"{what} must be an entity reference")
        target = v.get("id")
        part = v.get("part")
        if part not in ENTITY_PARTS:
            _fail(f"{what}: unknown entity part {part!r}")
        if not isinstance(target, str) or target not in known:
            _fail(f"{what}: names drawing {target!r}, which is not in this document")
        out = {"id": target, "part": part}
        # Absent axis = every coordinate that part owns. Present = just one,
        # which is what an editor's per-field padlock stores.
        axis = v.get("axis")
        if axis is not None:
            if axis not in PLACE_AXES:
                _fail(f"{what}: unknown axis {axis!r}")
            out["axis"] = axis
        return out

    constraints: list[dict[str, Any]] = []
    for c in _list(doc, "constraints"):
        if not isinstance(c, dict):
            _fail("each constraint must be an object")
        kind = c.get("kind")
        if kind not in CONSTRAINT_KINDS:
            _fail(f"unknown constraint kind {kind!r}")
        cid = _id(c.get("id"), f"constraint {kind}", seen)
        out: dict[str, Any] = {"id": cid, "kind": kind,
                               "a": _ref(c.get("a"), f"constraint {cid}.a")}
        if kind == "on":
            # 'on' is a relation BETWEEN two things; one reference cannot state
            # it, and a half-stated relation would load as a silent no-op.
            if c.get("b") is None:
                _fail(f"constraint {cid}: 'on' needs the line it is held against")
            out["b"] = _ref(c.get("b"), f"constraint {cid}.b")
            if out["b"]["id"] == out["a"]["id"]:
                _fail(f"constraint {cid}: a drawing cannot be held against itself")
        elif c.get("b") is not None:
            _fail(f"constraint {cid}: a {kind} names one thing, not two")
        constraints.append(out)

    clean = {"version": DOC_VERSION, "drawings": drawings,
             "measures": measures, "pins": pins, "constraints": constraints}
    size = len(json.dumps(clean))
    if size > MAX_DOC_BYTES:
        _fail(f"chart document is {size} bytes, over the {MAX_DOC_BYTES} limit")
    return clean


def empty_doc() -> dict[str, Any]:
    return {"version": DOC_VERSION, "drawings": [], "measures": [],
            "pins": [], "constraints": []}


def clean_key(key: Any) -> str:
    if not isinstance(key, str) or not (1 <= len(key) <= MAX_KEY):
        _fail(f"chart key must be 1-{MAX_KEY} characters, got {key!r}")
    return key


def is_empty(doc: dict[str, Any]) -> bool:
    # The constraints term is UNREACHABLE while validate() refuses a dangling
    # reference: a constraint must name a drawing in the same document, so a
    # non-empty constraints list implies a non-empty drawings list. Kept as the
    # belt to that pair of braces, and named here so nobody "simplifies" it
    # without noticing it is load-bearing the moment that rule relaxes.
    return not (doc["drawings"] or doc["measures"] or doc["pins"]
                or doc["constraints"])


def get(db: sqlite3.Connection, user_id: int, key: str) -> dict[str, Any]:
    row = db.execute(
        "SELECT doc FROM chart_objects WHERE user_id=? AND key=?",
        (user_id, clean_key(key)),
    ).fetchone()
    if row is None:
        return empty_doc()
    try:
        stored = json.loads(row["doc"])
        if stored.get("version") != DOC_VERSION:
            # A future/older generation is not half-applied: an anchor model we
            # cannot read is not a drawing we can honestly project.
            return empty_doc()
        return validate(stored)
    except (ValueError, TypeError):
        return empty_doc()  # a corrupt row degrades to blank, never crashes


def put(db: sqlite3.Connection, user_id: int, key: str, doc: Any) -> dict[str, Any]:
    k = clean_key(key)
    clean = validate(doc)  # raises ValueError with the reason
    if is_empty(clean):
        # Clearing a chart removes the row rather than storing an empty
        # document, so the table holds charts that HAVE drawings and `list_keys`
        # stays an honest answer to "where did I draw something".
        db.execute("DELETE FROM chart_objects WHERE user_id=? AND key=?", (user_id, k))
        return clean
    db.execute(
        "INSERT INTO chart_objects (user_id, key, doc, updated)"
        " VALUES (?,?,?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))"
        " ON CONFLICT(user_id, key) DO UPDATE SET doc=excluded.doc,"
        " updated=excluded.updated",
        (user_id, k, json.dumps(clean)),
    )
    return clean


def list_keys(db: sqlite3.Connection, user_id: int) -> list[dict[str, Any]]:
    """Every chart this user has drawn on, newest first."""
    rows = db.execute(
        "SELECT key, updated, length(doc) AS bytes FROM chart_objects"
        " WHERE user_id=? ORDER BY updated DESC",
        (user_id,),
    ).fetchall()
    return [{"key": r["key"], "updated": r["updated"], "bytes": r["bytes"]}
            for r in rows]
