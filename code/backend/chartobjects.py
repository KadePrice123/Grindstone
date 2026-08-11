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

import datetime as _dt
import json
import math
import re
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
CONSTRAINT_KINDS = ("lock", "on", "slope")
ENTITY_PARTS = ("line", "a", "b")
# Option legs — chart-side FILTERS on an options chain, never orders. The
# expiration is the primitive (a calendar date, not a bar time: expirations
# live past the last candle where bar times do not exist), tolerances are
# calendar days and dollars, and hostId may reference a drawing that binds the
# leg to a line. hostId is allowed to DANGLE (the measures policy, not the
# constraints policy): a leg whose trend was deleted degrades to its stored
# snapshot instead of vanishing with its host and taking the filter along.
LEG_SIDES = ("long", "short")
LEG_RIGHTS = ("P", "C")
MAX_LEGS = 12          # four-leg condors twice over, with headroom
MAX_DTE_TOL_DAYS = 60
MAX_STRIKE_TOL = 500.0

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
        # Minted BY a leg, so the engine may sweep it once nothing rides it.
        # A bare FLAG, not an owner id: the sweep test is rider count, because
        # a condor's four legs share one vline and sweeping it when the first
        # is deleted would strand the other three. Absent on every hand-drawn
        # line, which is precisely why those survive a leg's deletion.
        owned = d.get("legOwned")
        if owned is not None and not isinstance(owned, bool):
            _fail(f"drawing {did}: legOwned must be true/false, got {owned!r}")
        extra = {"legOwned": True} if owned else {}
        drawings.append({**extra, "id": did, "kind": kind,
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
        if kind == "slope":
            # A driving slope carries a VALUE, and it is price per hour of chart
            # time — never an angle. Degrees would rot the moment the price
            # scale drifted, which it does with no user input at all.
            if c.get("value") is None:
                _fail(f"constraint {cid}: a driving slope needs a value")
            out["value"] = _num(c.get("value"), f"constraint {cid}.value")
            if out["a"]["part"] != "line" or out["a"].get("axis") is not None:
                _fail(f"constraint {cid}: a slope drives a whole trend line")
        elif c.get("value") is not None:
            _fail(f"constraint {cid}: a {kind} carries no value")
        constraints.append(out)

    legs: list[dict[str, Any]] = []
    leg_items = _list(doc, "legs")
    if len(leg_items) > MAX_LEGS:
        _fail(f"'legs': at most {MAX_LEGS} per chart, got {len(leg_items)}")
    for lg in leg_items:
        if not isinstance(lg, dict):
            _fail("each leg must be an object")
        lid = _id(lg.get("id"), "leg", seen)
        side = lg.get("side")
        if side not in LEG_SIDES:
            _fail(f"leg {lid}: unknown side {side!r}")
        lright = lg.get("right")
        if lright not in LEG_RIGHTS:
            _fail(f"leg {lid}: right must be P or C, got {lright!r}")
        exp = lg.get("expiration")
        if not isinstance(exp, str):
            _fail(f"leg {lid}: expiration must be a date string")
        try:
            _dt.date.fromisoformat(exp)
        except ValueError:
            _fail(f"leg {lid}: expiration must be YYYY-MM-DD, got {exp!r}")
        strike = _num(lg.get("strike"), f"leg {lid}.strike")
        if strike <= 0:
            _fail(f"leg {lid}: a strike is a positive price, got {strike!r}")
        dte_tol = _num(lg.get("dteTol"), f"leg {lid}.dteTol")
        if not (0 <= dte_tol <= MAX_DTE_TOL_DAYS):
            _fail(f"leg {lid}: dteTol must be 0-{MAX_DTE_TOL_DAYS} days")
        strike_tol = _num(lg.get("strikeTol"), f"leg {lid}.strikeTol")
        if not (0 <= strike_tol <= MAX_STRIKE_TOL):
            _fail(f"leg {lid}: strikeTol must be 0-{MAX_STRIKE_TOL}")
        slot = lg.get("slot")
        if not isinstance(slot, int) or isinstance(slot, bool) or not (0 <= slot < 64):
            _fail(f"leg {lid}: slot must be a small integer, got {slot!r}")
        out_leg: dict[str, Any] = {
            "id": lid, "side": side, "right": lright, "expiration": exp,
            "strike": strike, "dteTol": dte_tol, "strikeTol": strike_tol,
            "slot": slot,
        }
        # A DANGLING host id is legal — the measures policy. Shape-checked only:
        # the leg runs on its stored snapshot when the drawing is gone.
        #
        # THREE id fields, because a leg has TWO hosts. `hostId` is the legacy
        # single binding whose ROLE was inferred from the drawing's kind; that
        # stopped working the moment a vline could drive the expiration while a
        # trend drove the strike, since kind can no longer disambiguate intent.
        # The role is now POSITIONAL — which field the id sits in — so no new
        # vocabulary string exists to keep in lockstep. Legacy docs are folded
        # at READ time by the engine, never rewritten, so DOC_VERSION holds.
        # The FOUR BOUNDING LINES are the filter's interface: two hlines carry
        # the strike range, two vlines the expiration range, and the strike
        # pair's vertical ORDER carries the side — so dragging one through the
        # other flips buy/sell. That ordering lives entirely in the geometry,
        # which is why no side field has to be kept in step with it here.
        for field in ("hostId", "timeHostId", "priceHostId",
                      "strikeHostA", "strikeHostB", "timeHostA", "timeHostB"):
            v = lg.get(field)
            if v is not None:
                if not isinstance(v, str) or not (1 <= len(v) <= MAX_ID):
                    _fail(f"leg {lid}: {field} must be an id, got {v!r}")
                out_leg[field] = v
        # The strategy tag. It was validated nowhere and copied nowhere, so
        # every condor lost its grouping on the first save — the four legs came
        # back as four unrelated legs. The enum harvest cannot see a dropped
        # scalar, which is exactly how it went missing; the gate now asserts an
        # exact round-trip value instead.
        grp = lg.get("group")
        if grp is not None:
            if not isinstance(grp, str) or not (1 <= len(grp) <= MAX_ID):
                _fail(f"leg {lid}: group must be a short tag, got {grp!r}")
            out_leg["group"] = grp
        # Per-leg visibility, stored TRUTHY-ONLY like a drawing's legOwned: the
        # key exists only on a hidden leg, so every document written before this
        # loads byte-identically and an absent key reads as visible — which is
        # both the safe default and the one an older client degrades to.
        #
        # This is PERSISTED although the global drawings-hidden switch is not,
        # and the difference is deliberate: that switch is a LENS over the whole
        # chart and belongs to the view, while this is an attribute OF the leg.
        # charts.gs already draws the same line, persisting its per-symbol eye
        # while treating `isolated` as a lens over it.
        #
        # No DOC_VERSION bump: a bump makes get() return empty_doc(), and the
        # next 400ms autosave then DELETES the row that still held the user's
        # work. An optional field costs nothing here — _list gives [] for an
        # absent list and a leg dict simply lacks the key.
        # THE CHOSEN CONTRACT. A leg is a filter WINDOW and routinely matches
        # dozens of contracts; `pick` is the one the user actually clicked, and
        # it is what turns a filter into a trade the analytics can price. An
        # OCC symbol ('SPY260918P00770000'), not one of this document's ids, so
        # it is validated as a bounded string and NOT run through _id() — it
        # references a contract at the provider, not an object in this doc, and
        # feeding it to the id checker would demand uniqueness against drawings.
        #
        # Deliberately NOT checked against the current chain: the window may be
        # dragged away from the pick, the market closes, a contract expires. A
        # pick that no longer matches degrades to "no pick" at read time in the
        # renderer, which is the measures' dangle-and-degrade policy again —
        # never a validation failure that would reject the whole document.
        pick = lg.get("pick")
        if pick is not None:
            if not isinstance(pick, str) or not (1 <= len(pick) <= MAX_ID):
                _fail(f"leg {lid}: pick must be a contract symbol, got {pick!r}")
            out_leg["pick"] = pick
        hid = lg.get("hidden")
        if hid is not None and not isinstance(hid, bool):
            _fail(f"leg {lid}: hidden must be true/false, got {hid!r}")
        if hid:
            out_leg["hidden"] = True
        legs.append(out_leg)

    # THE CHART'S OWN VIEW STATE. Kade: "every symbol has its own charting
    # data, just like drawing a line just with the chains and strat presets."
    # So the last strategy belongs HERE, in the per-symbol document, next to
    # the drawings it places — not in a global setting that would make SPY
    # and QQQ share one answer.
    #
    # NO DOC_VERSION BUMP, for the reason recorded above `hidden`: a bump
    # makes get() return empty_doc(), and the next 400ms autosave then
    # DELETES the row that still held the user's work. An optional key costs
    # nothing — an older document simply lacks it.
    view: dict[str, Any] = {}
    raw_view = doc.get("view")
    if isinstance(raw_view, dict):
        preset = raw_view.get("preset")
        # Bounded and charset-checked: it is a key from the preset registry,
        # and it comes back from the database into a lookup.
        if isinstance(preset, str) and re.fullmatch(r"[a-z0-9_-]{1,32}", preset):
            view["preset"] = preset

    clean = {"version": DOC_VERSION, "drawings": drawings,
             "measures": measures, "pins": pins, "constraints": constraints,
             "legs": legs}
    if view:
        clean["view"] = view
    size = len(json.dumps(clean))
    if size > MAX_DOC_BYTES:
        _fail(f"chart document is {size} bytes, over the {MAX_DOC_BYTES} limit")
    return clean


def empty_doc() -> dict[str, Any]:
    return {"version": DOC_VERSION, "drawings": [], "measures": [],
            "pins": [], "constraints": [], "legs": []}


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
    # The legs term is REACHABLE, unlike constraints: a leg's hostId may
    # dangle, so a chart can legally hold legs and nothing else. Forgetting it
    # here means put() DELETES the row for a legs-only chart on its own next
    # autosave.
    return not (doc["drawings"] or doc["measures"] or doc["pins"]
                or doc["constraints"] or doc["legs"])


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
