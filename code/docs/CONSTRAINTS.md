# Sketch constraints — the design

SolidWorks/Fusion-style dimensioning for chart drawings. Produced 2026-08-05 by
a design panel: five parallel readers over the engine, three independent
architectures, three judges (correctness / UX fidelity / codebase fit), one
synthesis. **This file is the settled architecture. Read it before changing
anything about constraints, and do not relitigate what it decided.**

Kade's ask, verbatim, which every part of this answers:

> The goal is to be able to easily and intuitively create accurate measurements
> and solidify a drawing. [...] two horizontal lines with a diagonal line between
> them, constraining slope between the two horizontal lines means moving either
> horizontal line up or down increases or decreases the length of the diagonal
> line to match; since the diagonal is not constrained to a time point with a
> vertical line or point it can slide across the time axis on the horizontal
> lines. We need everything to easily snap together and editing where a line sits
> shifts the whole drawing based on constraints. [...] Setting any dimension of an
> item will lock that dimension until the user changes it; if that dimension
> can't change because of a locking constraint we must notify them.

## Three decisions Kade made on 2026-08-05 — closed

1. When quantizing to a whole bar forces a line the user did NOT grab to move,
   **move it and say so**; never clamp the drag. It stays continuous and always
   lands on a legal configuration.
2. When the solver must move one end of a diagonal, **the end drawn first is the
   anchor** and the second trails. A `lock` on either endpoint overrides it.
   No swap control in v1.
3. A cluster that can float gets **no implicit anchor**. It needs an explicit
   `lock`, and the badge honestly reads "1 free" until it has one — an invisible
   anchor would read as a bug the first time it stopped something moving.

## Why the whole system is affine, and why that matters

Chart time is a prefix sum over candles where every step contributes one candle
unless a real gap is SHORTER than one (`chartMinutePrefix`, gate-asserted). So on
clean bars chart time is exactly proportional to bar index. In the coordinates
(price, chart-time) every permitted constraint — point-on-hline, point-on-vline,
gap, span, slope, parallel-via-shared-value — is LINEAR.

That is not luck. It follows from the refusals already in NOTES: banning angles,
cross-axis distance and circles is precisely what removes every `sqrt(dp^2+dh^2)`
and `atan2`. **The anisotropy rule bought linearity.** The one genuinely
nonlinear case is constraining a point onto a FREE diagonal, which is bilinear;
the Gauss-Newton loop below exists to walk that and the `iters === 1` gate
assertion proves it never has to on an affine scene.

## Rejected, with reasons

- **Least-norm / minimum-movement resolution of leftover DOF.** The obvious
  approach, and it produces the WRONG answer for the scenario above: minimising
  a weighted norm moves H1 — a line the user never touched — by a
  weight-dependent amount, because the null vector that moves H1 also partially
  cancels the diagonal's extension. It also splits the extension across both
  endpoints instead of extending the far one. The ordering rule below reproduces
  Kade's sentence exactly; a norm cannot.
- **A metric comparing dollars to bars.** Not needed, because nothing minimises
  anything. The two axis scales survive only inside the rank TOLERANCE, so they
  are intrinsic and view-independent and the same drag gives bit-identical
  geometry at every zoom level.
- **An expression language over named values.** Its parser and reference
  extractor would have to agree across ChartDraw.ts and chartobjects.py on
  BEHAVIOUR, and selftest's drift mechanism is a regex over a one-line union
  compared to a Python tuple — it structurally cannot check that. Literal named
  scalars only.

---

## Recommendation
**PIVOT — a rank-revealing solver with a deterministic pivot preference.**

Start from GAUSS (2 of 3 lenses), because only its rank machinery is correct as specified. Design 1's step 8 infers a global rank deficiency from a singular *diagonal SCC block*, which is invalid — a singular block means the chosen square submatrix is singular, not the m×n matrix — and its own showcase configuration proves it: with rows {on, on, slope, run} the matching that pairs r3→τ_a, r4→τ_b gives det = (s/60)(1) − (−s/60)(−1) = 0 for every s, so its formula returns DOF 3 while row reduction (r3 + (1/120)·r4 = (0,0,0,−1,0,1)) gives rank 4 and DOF 2. The repair is re-matching, and step 4 is never re-run. Design 3's DOF is genuinely exact but purchases that by narrowing what can be said: relations must be oriented by hand, cycles are refused where a solver solves them, and `barAtChartMinutes` "lands on a bar by construction" is quantization renamed — it silently realises 0.5208 $/h for a typed 0.50 with no residual code anywhere.

But GAUSS as written has a flaw the panel did not name, and it is at the centre of Kade's sentence. **Least-norm does not reproduce "the diagonal grows to match."** GAUSS's Step 4 claims adding a multiple of v₁ "costs norm without touching p₂, so the coefficient is 0" — false, because v₁ = (1,0,0,1,−2,0) also has an i_b component that partially cancels the 4-bar extension. Minimising ‖(a,2,0,a,4−2a,2)‖²_W gives a = 4w_i/(w_p+2w_i) ≠ 0, so H1 — a line the user never touched — moves, and the amount depends on the weights, which GAUSS makes view-dependent at drag time. Pure least-norm also splits the extension between both endpoints of the diagonal instead of extending the far one.

PIVOT fixes this by grafting Cascade's real contribution — *ordering* — into GAUSS's rank machinery, without needing acyclicity, matching, or SCC repair:

> Rank-revealing QR chooses a maximal independent set of columns to be **pivots** (dependent, solved). Every non-pivot column is **free and held at its seed value**. Which columns get picked is decided by a total, deterministic **preference order**, not by column magnitude and not by a norm.

Three consequences, all of them improvements over every input design:

1. **There is no norm anywhere.** Not at drag time, not on load, not on a value edit. Free variables keep their seeds; the square dependent system has a unique solution. Both GAUSS's pixel-vs-intrinsic weight split and Cascade's least-norm anisotropy worry evaporate — the two scale factors survive only inside the *rank tolerance*, so they are intrinsic and view-independent and the same drag produces bit-identical geometry at every zoom level. This is strictly stronger than what any of the three claimed.
2. **Kade's prose is reproduced exactly, and it is explainable.** The preference is: held/pinned columns last; then BFS hops in the constraint graph from the grabbed entity, ascending; then trend endpoint `b` before endpoint `a` ("the end you drew second is the one that trails"); then axis, phase-dependent; then id. Dragging H2 gives pivots {p_b, i_b, p_a} and free {i_a, p₁, p₂}, so H1 stays at $600, the diagonal's start stays at bar 100, and its end goes to bar 112. Verified below.
3. **Cycles still solve, `on`-a-trend still works, and s = 0 is a non-event.** Nothing here requires the system to be affine or acyclic. The slope row's index partials are ±value·c(i)/60, so at s = 0 the row degenerates to p_b − p_a = 0 and stays independent — no division by s exists in the file, which is why I *reject* Cascade's admission-time refusal of a zero slope (see UX).

Absorbed from Cascade: the τ prefix sum framed as the **change of variable in which the slope residual is affine** (not merely an O(1) cache), which is what licenses the `iters === 1` gate assertion; per-frame quantization residual as first-class reported state; relation glyphs as clickable canvas objects; SolidWorks-correct colour direction; the `commit()` seam; the null-basis-as-sentences construction; and the refusal-quotes-the-implied-number discipline. Absorbed from Sheet: the O(1) occupancy prefilter ahead of the rank test, so most user-visible refusals never touch a float tolerance; the `range` issue code so `nearestBarTime` is never called silently; the `is_empty()`/`adoptIds`/`seen` housekeeping; a **named-value layer** (literal scalars only — no expression language) that makes "these points act as variables" real and gives `parallel` an affine substitute; and the read-outputs/write-inputs trade API shape.

Explicitly refused from Sheet: the expression language. Its parser, `ROLES_FOR`, `WRITES` and reference-extractor would have to agree across ChartDraw.ts and chartobjects.py on *behaviour*, and `selftest.py`'s entire drift mechanism is `re.findall` over a one-line union compared to a Python tuple — it structurally cannot check that a TS parser and a Python extractor agree on what `@dw3.slope` depends on. That would be the first agreement in this repo the gate cannot enforce, in a file whose docstring says "the gate enforces the agreement rather than trusting it."

---

## Model

## Solver variables — what is and is not a parameter

`chartobjects.py:38` already decides this: *"hline/vline are one — the line IS one coordinate and the point only places its handle."*

| kind | solver variables | never a variable |
|---|---|---|
| `hline` | 1 — `p` | `points[0].time` (handle) |
| `vline` | 1 — `i` | `points[0].price` (handle) |
| `trend` | 4 — `ia, pa, ib, pb` | — |
| `circle` | 0, excluded | both points (`ellipsePx` L1240 is the in-tree anisotropy proof) |
| `Measure` | 0 | `place.at` is a handle; a dimension owns one row and no geometry |
| `InspectPin` | 0 | a pin's position IS its bar (L653) |

If a handle coordinate enters the vector, every hline carries a permanently free DOF, nothing reaches "fully defined", and the whole status signal is dead. One guard in `varsOf()`.

**Time is bar INDEX, continuous during the solve, quantized only at commit.** `indexToCoordinate` (vendored lightweight-charts mjs:6134) is affine in index, not seconds; an epoch second (~1.7e9) beside a price (~600) is a 1e7 condition number from units alone; and a fractional index lerps to a real pixel while a second inside a weekend gap does not exist.

## New TypeScript declarations (ChartDraw.ts)

All three new unions are **single-line**, matching the `DrawKind` convention `selftest.py:2583` already parses with `([^\n]+)`. None of `DrawKind`, `MeasureAnchor` or `MeasurePlace` is touched, so the fragile `(.*?)\n\n` block-grabs at :2585/:2587 keep working untouched.

```ts
export type ConstraintKind = 'on' | 'coincident' | 'samePrice' | 'sameTime' | 'lock'
export type EntityPart = 'line' | 'a' | 'b'
export type ValueUnit = 'price' | 'bars' | 'slope'

/** Which coordinate-carrying part of a drawing a constraint names. 'line' is
 *  the whole hline/vline — it IS one coordinate, so it has no endpoints, which
 *  is the same fact POINTS_FOR states on the backend. 'a'/'b' are a trend's two
 *  points. Circles are never referenced. */
export interface EntityRef { id: string; part: EntityPart }

/** A geometric relation. NO value, ever — a relation cannot be demoted to a
 *  reference because there is nothing to report, which is why SolidWorks
 *  refuses an over-defining relation outright and offers Make-Driven only for a
 *  dimension. That asymmetry is why relations and dimensions are separate
 *  objects here.
 *
 *  'on'         a trend endpoint lies on an hline (1 row: equal price) or a
 *               vline (1 row: equal index). ONE row, not two — that single
 *               difference from 'coincident' IS the sliding diagonal, and it is
 *               why the two get different glyphs.
 *  'coincident' two endpoints are the same point: 2 rows.
 *  'samePrice'  1 row.   'sameTime'  1 row.
 *  'lock'       pins whatever is there. VALUELESS on purpose: a stored absolute
 *               price on a |% key would reference bars[0].close of the first
 *               visible line — a base that is in no document and moves on every
 *               refetch. A valueless lock cannot rot.
 *
 *  Refused by design and absent on purpose: parallel-without-a-value, equal
 *  length, tangent, perpendicular, anything angular, anything touching a
 *  circle. The affine substitute for 'parallel' is a NAMED ChartValue of unit
 *  'slope' driving both diagonals. */
export interface Constraint {
  id: string
  kind: ConstraintKind
  a: EntityRef
  /** Absent for 'lock'. */
  b?: EntityRef
  /** 'lock' on a trend endpoint only: which coordinate is pinned. Absent = both.
   *  Must be absent for part 'line' — the line has one coordinate and it is
   *  implied. Reuses the same two axis words as MeasurePlace. */
  axis?: 'price' | 'time'
}

/** A named scalar. This is what "these points act as variables" actually means,
 *  and it is deliberately NOT an expression: a literal number, a unit, a name.
 *  Two diagonals driven by one 'slope' value are parallel and the system stays
 *  affine; a trade card reads and writes these by name.
 *
 *  bars and hours are NOT interconvertible and there is no 'hours' unit,
 *  because chartMinutes charges min(real gap, one candle) per step (L1155), so
 *  the bars->hours factor is path-dependent, not scalar. A span dimension
 *  therefore drives BARS. 'slope' is $ (or percentage points) per hour of chart
 *  time — the one sanctioned axis-mixing quantity, and it names both units. */
export interface ChartValue {
  id: string
  /** /^[A-Za-z_][A-Za-z0-9_]{0,15}$/ , unique per chart, case-sensitive. */
  name: string
  unit: ValueUnit
  v: number
}

export interface Measure {
  id: string
  a: MeasureAnchor
  b: MeasureAnchor
  place?: MeasurePlace
  /** DRIVING by literal. WHICH number is driven is chosen by `place`, exactly
   *  as measureRows L2179-2195 already chooses which row to print:
   *    axis 'time'  -> the price gap   ($, or percentage points on a |% key)
   *    axis 'price' -> the span        (BARS)
   *    absent       -> the slope       ($ or % per hour of CHART time)
   *  So there is no "which quantity?" picker anywhere: you type into the box
   *  showing the number you were already reading. Absent = the reference
   *  dimension it is today, and presence IS the driving flag — a reference
   *  dimension has no value field that could go stale. */
  value?: number
  /** DRIVING by reference to a ChartValue.name. Mutually exclusive with
   *  `value`; the validator rejects both. */
  valueRef?: string
}

export interface ChartDoc {
  version?: number
  drawings: Drawing[]
  measures: Measure[]
  pins: InspectPin[]
  /** Both absent on every document written before this shipped — _list()
   *  returns [] for a missing key, which is what keeps CHART_DOC_VERSION at 1. */
  constraints?: Constraint[]
  values?: ChartValue[]
}
```

## Solver-side types (module-level exports, no DOM, no `import { api }`)

```ts
/** One scalar unknown. axis 'i' is BAR INDEX (fractional during a solve). */
export interface VarRef { id: string; part: EntityPart; axis: 'i' | 'p' }

export interface Row {
  /** Constraint or measure id — the blame handle and the paint handle. */
  src: string
  /** 0/1 for a two-row constraint. */
  part: number
  /** dResidual/dVar by slot; <= 6 nonzeros in practice. */
  grad: Float64Array
  /** Residual at the current x, in this row's own physical unit. */
  r: number
  /** That unit's magnitude, for row equilibration. */
  unit: number
}

export interface Problem {
  vars: VarRef[]
  /** SEED — the current on-screen configuration. Never zero, never defaults. */
  x: Float64Array
  /** Per-variable scale: sigmaP for 'p', 1 bar for 'i'. Used ONLY by the rank
   *  tolerance — no objective is ever minimised, so this cannot change the
   *  answer, only the verdict's conditioning. */
  scale: Float64Array
  rows: (x: Float64Array) => Row[]
}

/** Columns held at fixed values: the dragged handle, frozen indices at commit.
 *  A `lock` is NOT here — it is a Row, so it participates in rank and blame. */
export interface Held { slots: number[]; values: Float64Array }

export interface Analysis {
  rank: number
  dof: number
  /** Dependent columns, in the order QR took them. */
  pivots: number[]
  /** Non-pivot columns: the free set, held at seed. */
  free: number[]
  /** One vector per free column, each with that column = 1 and the rest
   *  back-substituted. Sparse and one-to-one with a sentence. Never SVD. */
  nullBasis: Float64Array[]
  /** Rows the factorization found dependent. */
  dependent: string[]
  /** ||R^-1 r||inf at x. Full rank with this above tol is a CONFLICT. */
  residual: number
}

export type SolveIssue =
  | { code: 'blocked';   slot: VarRef; heldAt: number; blame: string[]; text: string }
  | { code: 'range';     slot: VarRef; side: 'before' | 'after'; byBars: number; text: string }
  | { code: 'quantized'; measureId: string; asked: number; achieved: number; text: string }
  | { code: 'moved';     ids: string[]; text: string }
  | { code: 'conflict';  src: string; implied: number | null; blame: string[]; text: string }
  | { code: 'redundant'; src: string; implied: number; text: string }
  | { code: 'duplicate'; src: string; other: string; text: string }
  | { code: 'unit';      want: ValueUnit; got: ValueUnit; text: string }
  | { code: 'dangling';  ids: string[]; text: string }
  | { code: 'toobig';    vars: number; text: string }

export interface SolveResult { x: Float64Array; iters: number; ok: boolean; issues: SolveIssue[] }

export type EntityStatus = 'loose' | 'partial' | 'fixed' | 'conflict' | 'dangling'

export interface EntityState {
  id: string
  status: EntityStatus
  /** Per-COORDINATE freedom: a coordinate is free iff some null-basis vector
   *  has a nonzero component on it. Feeds the small markers on bound endpoints. */
  freeAxes: ('i' | 'p')[]
}

export interface FreeMotion { text: string; slot: VarRef }

export interface ValueView { name: string; unit: ValueUnit; v: number; usedBy: string[] }

export interface SolveReport {
  /** Summed over CONSTRAINED components only — an untouched chart reads 0, not
   *  "18 free", or the badge is noise from the first drawing. */
  dof: number
  entities: EntityState[]
  motions: FreeMotion[]
  values: ValueView[]
  /** The one line the page shows. Derived fresh in getState(), never pushed —
   *  render() wipes both layers every call, so a notice cannot outlive a frame. */
  notice: SolveIssue | null
  issues: SolveIssue[]
}

export interface DrawState {
  /* ...every existing field unchanged... */
  solve: SolveReport
}
```

`HotZone.kind` widens to `'measure' | 'pin' | 'relation'` (internal, not persisted).

## The residual rows, exactly

`H(ia, ib)` is signed chart-hours from the prefix sum `C[]`; `c(i)` is the local per-step chart-minute cost.

| kind / target | residual | rows | unit |
|---|---|---|---|
| `on` → hline | `p_ref − p_H` | 1 | $ |
| `on` → vline | `i_ref − i_V` | 1 | bars |
| `on` → trend | `(p−p1)(i2−i1) − (i−i1)(p2−p1)` | 1 | $·bars |
| `coincident` | `i_a − i_b` ; `p_a − p_b` | 2 | bars, $ |
| `samePrice` / `sameTime` | `p_a − p_b` / `i_a − i_b` | 1 | $ / bars |
| `lock` line | `p − p°` or `i − i°` | 1 | $ / bars |
| `lock` endpoint | `i − i°` ; `p − p°` (or one, per `axis`) | 1–2 | bars, $ |
| dim `axis:'time'`, value V | `p_B − p_A − V` | 1 | $ |
| dim `axis:'price'`, value N | `i_B − i_A − N` | 1 | bars |
| dim free, value s | `(p_B − p_A) − s·H(i_A, i_B)` | 1 | $ |

Only `on`→trend is nonlinear, and it is one bilinear scalar row removing exactly 1 DOF — the correct count for point-on-entity, and why the diagonal still slides.

Anchor → expression resolution mirrors `resolveAnchor` L1529-1569 so a driving dimension reads the same geometry the chip prints: `line`→hline contributes `p_H`; `line`→vline contributes `i_V`; `line`→trend contributes `(1−u)·p0 + u·p1` with the stored `u` as a **constant** (affine, and matching L1561's pixel lerp exactly because both projections are affine); `vertex`-style endpoint reads come from `EntityRef`, not from an anchor. `candle` and `free` anchors contribute constants — **and a candle anchor is a hard time constraint on the coordinate it names**, removing 1 DOF and refusable when it over-defines. Decided once, here, so this does not reproduce FreeCAD #15850 (discrete DOF invisible to the Jacobian, under-determined sketches reported as fully constrained).

`u` is read **only** for a trend. For hline/vline anchors `snapAnchor` L1504 stores `lineHit.u`, which is a fraction of the pane width (`hitSegPx` L1196 spans `0..pane.width`) — a persisted, viewport-dependent, meaningless number that `resolveAnchor` L1549-1557 already ignores. The solver ignores it too.

## chartobjects.py — the lockstep edit, same commit

```python
CONSTRAINT_KINDS = ("on", "coincident", "samePrice", "sameTime", "lock")
ENTITY_PARTS = ("line", "a", "b")
VALUE_UNITS = ("price", "bars", "slope")

MAX_NAME = 16
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,15}$")


def _ref(v: Any, what: str) -> dict[str, Any]:
    if not isinstance(v, dict):
        _fail(f"{what} must be an object")
    rid = v.get("id")
    if not isinstance(rid, str) or not (1 <= len(rid) <= MAX_ID):
        _fail(f"{what}.id must be an id, got {rid!r}")
    part = v.get("part")
    if part not in ENTITY_PARTS:
        _fail(f"{what}: unknown part {part!r}")
    return {"id": rid, "part": part}


def _constraint(c: Any, what: str) -> dict[str, Any]:
    if not isinstance(c, dict):
        _fail(f"{what} must be an object")
    kind = c.get("kind")
    if kind not in CONSTRAINT_KINDS:
        _fail(f"{what}: unknown constraint kind {kind!r}")
    out = {"kind": kind, "a": _ref(c.get("a"), f"{what}.a")}
    if kind == "lock":
        if c.get("b") is not None:
            _fail(f"{what}: a lock names one entity, not two")
        axis = c.get("axis")
        if axis is not None:
            if axis not in PLACE_AXES:
                _fail(f"{what}: unknown lock axis {axis!r}")
            if out["a"]["part"] == "line":
                _fail(f"{what}: a line has one coordinate — its lock takes no axis")
            out["axis"] = axis
    else:
        out["b"] = _ref(c.get("b"), f"{what}.b")
    return out
```

`validate()` gains, after the pins loop:

```python
    values: list[dict[str, Any]] = []
    names: set[str] = set()
    for v in _list(doc, "values"):
        if not isinstance(v, dict):
            _fail("each value must be an object")
        vid = _id(v.get("id"), "value", seen)
        name = v.get("name")
        if not isinstance(name, str) or not NAME_RE.match(name):
            _fail(f"value {vid}: name must match {NAME_RE.pattern}, got {name!r}")
        if name in names:
            _fail(f"value {vid}: duplicate name {name!r}")
        names.add(name)
        unit = v.get("unit")
        if unit not in VALUE_UNITS:
            _fail(f"value {vid}: unknown unit {unit!r}")
        values.append({"id": vid, "name": name, "unit": unit,
                       "v": _num(v.get("v"), f"value {vid}.v")})

    constraints: list[dict[str, Any]] = []
    for c in _list(doc, "constraints"):
        cid = _id(c.get("id"), "constraint", seen)
        constraints.append({"id": cid, **_constraint(c, f"constraint {cid}")})
```

and in the measure loop, after `place`:

```python
        if m.get("value") is not None and m.get("valueRef") is not None:
            _fail(f"measure {mid}: a driving dimension takes a literal or a name, not both")
        if m.get("value") is not None:
            out["value"] = _num(m["value"], f"measure {mid}.value")
        if m.get("valueRef") is not None:
            ref = m["valueRef"]
            if not isinstance(ref, str) or ref not in names:
                _fail(f"measure {mid}: valueRef {ref!r} names no value")
            out["valueRef"] = ref
```

(the values loop runs **before** the measures loop so `names` is populated.)

Then `clean`, `empty_doc()` and `is_empty()` all widen:

```python
    clean = {"version": DOC_VERSION, "drawings": drawings, "measures": measures,
             "pins": pins, "constraints": constraints, "values": values}

def is_empty(doc):
    return not (doc["drawings"] or doc["measures"] or doc["pins"]
                or doc["constraints"] or doc["values"])
```

**`is_empty` is the one that silently destroys data if missed**: `put()` L237-242 DELETES the row when it is true, so a chart carrying only named values would have its row removed on the next autosave.

**Deliberately NOT validated on the backend:** referential integrity of `EntityRef.id`. `clickTrim` L1824-1847 mints fresh `mkId('dw')` ids for every surviving span and kills the original *plus every donor it split*, so an ordinary editing gesture would otherwise make the document unsavable. A constraint whose referent is gone **dangles** — dropped from the solve, rendered dim, never deleted. Same degradation `resolveAnchor` L1543 already performs for anchors.

---

## Solver

## 0. The coordinate change that makes the slope row affine

`chartMinutes` L1143-1158 is a per-step sum of `min(real gap, per)`. That is a prefix sum in disguise. Hoist it into `barsIdx()` L1090-1099 under the **same array-identity invalidation**:

```
C[0] = 0
C[k+1] = C[k] + (gap_k > 0 && gap_k < per ? gap_k : per)     // gap_k = (times[k+1]-times[k])/60
```

Identical arithmetic to the loop, so `_chart_time`'s existing assertions keep their numbers.

- **Forward:** `chartMinutes(a,b) = C[ib] − C[ia]`, **O(1) instead of O(bars-between)**. This pays down a shipped cost: `measureRows` L2161 calls it once per measure per render on charts NOTES records at 8435 bars.
- **Fractional:** `H(i) = C_lerp(i)/60` chart-hours; `dH/di = c(⌊i⌋)/60`.

This is not merely a cache — **it is the coordinate in which the slope residual is linear**, which is not true in bar index and not true in epoch seconds. `C` is exactly linear on clean regular bars (`gap == per` fails `gap < per`, so every step contributes `per`) and bends only where a real gap is *shorter* than one candle (half-day sessions, extended-hours feeds, resampled series). That bend is the only nonlinearity in the entire affine vocabulary, and it is what the Gauss–Newton loop exists to walk. On a clean chart the first step is exact, which is the `iters === 1` gate assertion.

**`nearestBarTime` L1160-1174 is never called by the solver.** It clamps to `ts[0]`/`ts[last]` and never reports that it did — a direct violation of "we must notify them." The solver uses:

```ts
/** Bar index nearest i, or which side it fell off and by how much. Never clamps
 *  silently: running out of loaded history is a VALIDITY failure with its own
 *  message, not a solver failure. */
function barIndexOf(i: number, n: number):
  { i: number } | { side: 'before' | 'after'; byBars: number }
```

## 1. Two factorizations, two purposes

- **Verdict** (rank, DOF, admission, dependency, blame): **full column-pivoted Householder QR** on the two-sided equilibrated `J̃ = R⁻¹ J S`. Maximum numerical robustness; nothing about the geometry depends on which columns it picks, because rank is order-independent.
- **Solve** (which coordinates move): **QR with a prescribed column order** — the preference below — skipping any column whose post-elimination norm falls below tolerance. Determinism over magnitude-robustness, because the *answer* depends on this choice and a user must not see the drag behaviour change with the price scale.

They agree on rank. That split is deliberate and is the honest resolution of the "column pivoting is more robust but magnitude-driven" tension.

**Equilibration.** `S = diag(scale)` per column, `R = diag(unit)` per row. Every entry of `J̃` is then dimensionless and O(1): an `on` row gives ±1; the slope row gives ±1 on prices and ∓(value·h_bar·σ_i/σ_p) on indices, which for realistic slopes is 0.1–10. Singular-value gap between a real relation and noise is ~1e12, so the tolerance `1e-9·‖J̃‖∞·max(m,n)` is not a fudge sitting in a grey zone.

- `σ_i = 1 bar`.
- `σ_p = median over loaded bars of (high − low)`, floored at `1e-6·median(close)`. A typical bar's range — the natural isotropy a candlestick chart already asserts. It is a **data** quantity, not a view quantity, so it is stable across pan, zoom and autoscale drift. On `|%` keys divide by `bars[0].close` and ×100 to match the axis (`Chart.tsx:298`).

**There are no pixel weights and no drag-time scale swap.** GAUSS needed them because it minimised a norm. PIVOT minimises nothing, so the scales survive only inside a rank tolerance and the solve is bit-identical at every zoom level.

## 2. The preference order — the design's core

```
key(v) = [ heldRank(v),          // 0 normal · 1 held (pinned handle, frozen index)
           bfs(v),               // hops in the constraint graph from the grabbed
                                 //   entity (0 = the grab); +inf when nothing is
                                 //   grabbed; for a value edit, from the edited
                                 //   dimension's two anchors
           endRank(v),           // trend endpoint 'b' = 0 · endpoint 'a' = 1 · line = 2
           axisRank(v),          // DRAG:   'i' = 0, 'p' = 1
                                 // COMMIT / VALUE EDIT / LOAD: 'p' = 0, 'i' = 1
           v.id ]                // total, deterministic
sort ascending; QR takes pivots greedily from the front.
```

Read it as four sentences:

1. **Held columns last** → QR pivots on a pinned handle only when forced, and being forced *is* the blocked state.
2. **BFS from the grab** → the things nearest what you grabbed absorb the change; distant hosts (H1 in Kade's scene, at 3 hops) fall to the free set and stay put. This is what makes "H1 doesn't move" a property of the algorithm rather than an accident of a weight.
3. **Endpoint `b` before endpoint `a`** → the end you clicked second is the one that trails; the start is the anchor. Deterministic, explainable, *and shown* ("the diagonal's start bar is free"). Reversible with a `lock`.
4. **Axis preference is PHASE-DEPENDENT, and this is where I split the panel's must-steal.** Cascade's "price-first" is right at **commit** — prices are continuous and the lattice never touches them, so preferring price pivots maximises the number of exactly-satisfied constraints. It is *wrong during the drag*: with prices eager, dragging H2 would freeze the diagonal's length and move H1 instead, which is the opposite of Kade's sentence. So indices are eager while the cursor is down, prices are eager when it lifts.

## 3. Solving

```
solve(problem, held, maxIters):
  order  <- preference sort of columns
  loop up to maxIters:
     rows <- problem.rows(x)
     J~   <- R^-1 J S,  restricted to NON-HELD columns
     QR with prescribed order -> pivots P (|P| = rank), free F = rest
     x[F] <- seed values (unchanged); x[held] <- held values
     solve  R11 dx_P = -(R^-1 r + R12 dx_F)      // dx_F = 0 by construction
     backtracking line search: halve up to 8x, accept first decrease of ||R^-1 r||_2
     if ||R^-1 r||inf < 1e-7: converged
  if two consecutive iterations fail to decrease -> Levenberg-Marquardt on that
     component, lambda = 1e-3*||J~||inf, x10 per failure
```

`R11` is square and invertible by construction (pivots are a maximal independent column set), so with free variables held at seed the solution is **unique — there is no norm, no minimiser, no arbitrary member of a family.** On an affine, consistent system the first step is exact and `iters === 1`.

Hard caps: **8 iterations during a live drag, 24 at commit.** No timer, no rAF, no loop that can run long — `selftest.py:1325` fails the build on any `setInterval` in this file anyway.

**Seeded from the current on-screen configuration, never from zero.** That is what makes dragging continuous rather than jumpy, and it becomes load-bearing the moment a bilinear `on`-a-trend admits two branches (SolveSpace states this explicitly: the initial position selects the branch).

**Connected components.** Union-find over constraint incidence at topology-change time. A drag touches only the grabbed entity's component; every other component is untouched, unfactorized, and allocates nothing. Typical component ≤ 12 variables and ≤ 10 rows: prescribed-order QR is ~2mn² ≈ 2.9 kflop, ×4 iterations ≈ 12 kflop per mousemove — at 120 Hz, 0.01% of a core. `MAX_PER_KIND = 500` means the pathological one-component case is n = 2000 (≈8 Gflop, unshippable), so **a constraint that would merge two components past `MAX_COMPONENT_VARS = 120` is refused with a reason** (n = 120 is 3.4 Mflop ≈ 2 ms). An explicit limit, not a hope.

## 4. Dragging — hard pin, then project the desire

At `onDown`, in addition to today's `orig: Map<string, Pt[]>` L677-681, capture the component, the held slots, and the analysis. `dimDrag` L533 gets the snapshot it currently lacks (there is no way to revert an abandoned dimension drag today: `endDrag(false)` L648 just nulls it and the mutated `place` stays).

Per mousemove, with `δ = (dx/barSpacing, dy·pricePerPx)`:

```
E   <- 2 x n selector for the grabbed handle's (i, p) columns
N   <- null basis from the analysis
Rch <- column space of E*N          // the REACHABLE SET at the handle, dim 0/1/2
d_ok <- projection of delta onto Rch
blocked_px <- ||delta - d_ok|| in pixels
held <- { grabbed slots := x_handle + d_ok }
solve(...)
```

**Project the DESIRE, not the solution.** Projecting the whole displacement onto `null(J)` produces the quarter-speed trail (in a DOF-2 cluster, `⟨d,v⟩/‖v‖²·v = ¼v`). Projecting `δ` onto the reachable set means a handle constrained to an h-line **slides along that h-line at exactly cursor speed** while the blocked component is dropped. `Rch` is a genuine subspace of R², not two independent axes, so a handle constrained to a diagonal slides along the diagonal — which per-axis driver-dropping cannot express.

Three outcomes, and confusing the first two is a lie in both directions:

- `dim Rch = 2` → the handle tracks the cursor exactly; whatever is welded to it comes along. If H2 is pinned to H1 by gap × slope, dragging H2 **carries H1**. That is not blocked; it is the constraint made legible through motion, worth more than any tooltip.
- `dim Rch = 1` → the handle slides along its permitted direction at full speed. A soft-edge notice only if `blocked_px > 3` sustained across two frames.
- `dim Rch = 0` → genuinely blocked. Geometry does not move, a dashed `STROKE_DANGER` ghost runs from the clamp to the cursor (the idiom `render()` L2007-2032 already uses for the trim preview), the blame set paints red, and the notice fires.

**Blame** = leave-one-out rank tests over the component's rows: constraint *i* is in the blame set iff removing it makes the desired direction reachable. One rank test per row; at ten rows that is trivially affordable and gives exact single-constraint attribution, not SketchXpert's "cycle through candidate deletions."

## 5. Commit — indices quantize, prices absorb, and the residual is REPORTED

GAUSS reverts to the last good `x` on a commit conflict, which undoes a drag that tracked cleanly. Replace with:

```
endDrag(true):
  1. hold the grabbed handle at its final dragged value
  2. round each index variable to the nearest bar via barIndexOf()
     - a 'before'/'after' result is a RANGE issue: do not clamp, do not
       nearestBarTime. Roll the drag back to the last in-range frame, report:
       "the diagonal's end needs bar 128; the chart ends at bar 118 (10 short)."
  3. local search: for each index variable (cap 4; beyond that just round),
     try {round-1, round, round+1}, re-solving prices each time, and take the
     combination minimising the summed |residual| of the DRIVING dimensions.
     3^k with k<=4 = 81 square solves of <=12 vars, well under a frame.
  4. freeze the chosen indices as held columns, re-solve with axisRank = price
     first, so free PRICES absorb the sub-bar remainder exactly.
  5. if a residual survives (every price in the component is also held),
     land on the best bar anyway and emit a 'quantized' issue carrying
     {measureId, asked, achieved}. NEVER revert a drag that tracked cleanly.
  6. if step 4 moved an entity the user did not grab by more than one
     sigma_p/4, emit a 'moved' issue naming it.
  7. one render(), one emit().
```

Step 6 is the fix for GAUSS's conceded "the visible side effect will read as a bug to someone the first time": H1 dropping $0.20 is correct behaviour, and it stops reading as a bug the moment the chart says *"H1 moved to $599.80 so the diagonal's end could land on bar 111."*

The residual bound is `1/(2N)` relative on an N-bar run and is timeframe-independent, so the escape offered in the message is real: **set the run in bars instead and the slope becomes exact**, because the run is a lattice row and the gap it then determines is a price, which is continuous.

## 6. Admission — two tiers, so most refusals never touch a tolerance

**Tier 0 — O(1) prefilter (stolen from Sheet, adapted).** Before any matrix:

- *Occupancy*: this coordinate already carries a `lock` → `duplicate`, message *"H1's price is already locked at $600.00."*
- *Signature*: canonicalise each candidate row as its sorted `(varSlot, coefficient/leadCoefficient)` list. An identical signature already present → `duplicate`, message *"this gap is already dimensioned ($4.00)."* Catches two `on`s from one endpoint to one line, two gap dimensions on one pair, and `samePrice` + a slope of 0.

**Tier 1 — rank increase**, on a speculative copy of the component:

```
rank(J + row) == rank(J) + 1                       -> ACCEPT as driving
rank unchanged, residual within tol                -> REDUNDANT: refuse as driving,
                                                      offer reference, QUOTE the value
rank unchanged, residual out of tol                -> CONFLICT: refuse, quote the implied
                                                      value, name the blame set
```

The implied value is not guessed — it is the current evaluation of the quantity the dimension would have measured, which `measureRows` already computes. That is the difference between *"the run is already 8 bars — set by the $4.00 gap and the 0.0769 $/h slope; it cannot be 10"* and Fusion's *"this dimension would over-constrain the sketch."*

**A multi-row relation is admitted on rank increase ≥ 1 with the dependent sub-rows consistent** — always true for a `lock`, which pins the current value — and reported as *partially redundant*, informational. A single-row **dimension** stays strict, because it carries a value that can disagree. This asymmetry is the one Cascade gets wrong (its "exactly 1 per row" rule falsely refuses a `coincident` whose price half is already implied by an `on`).

**Verdicts are cached at creation and never re-litigated on load**, so the tolerance's soft edge cannot make a drawing change colour on a reload for no perceivable reason.

**s = 0 is accepted, not refused.** I reject Cascade's admission-time refusal: it refuses a well-posed system. The slope row's index partials are `±value·c(i)/60`, which vanish at 0, so the row degenerates to `p_B − p_A = 0` and — I checked — stays independent of the two `on` rows. Rank is unchanged, DOF is unchanged, and only the *free set* changes. The correct response is to say so: *"slope 0 spans no time — the diagonal's start and end bars are now both free."* Nothing divides by `s` anywhere in PIVOT, which is why the landmine that forces both other designs into a special case is a non-event here.

## 7. When the solver runs — the complete list

(1) a live drag mousemove; (2) a value commit; (3) a constraint/dimension/value add, edit or delete; (4) once at hydrate completion, to colour entities.

It does **not** run on `subscribeVisibleLogicalRangeChange` L598, `subscribeCrosshairMove` L634, or the `ResizeObserver` L750. Pan, zoom, hover and resize change pixels, not parameters; the solved geometry is already correct and the cached `Analysis` (keyed by a module-level `topologyRev`) still describes it. `if (id === this.hoverId) return` L623 and the no-repaint-at-rest invariant are untouched. That separation is only possible because nothing angular and nothing pixel-derived is stored.

## 8. The one refactor the solver forces

Every mutator today ends with its own `this.render(); this.emit()`. A solve that moves twelve drawings must not fire twelve renders and twelve debounce resets.

```ts
/** Mutate, then solve, then paint and persist ONCE. Every bucket mutator routes
 *  through here. emit() is the only persistence hook in the engine, so a solve
 *  that skips it is a change that is never saved. */
private commit(): void {
  bumpRev(this.key)
  this.report = solveKey(this.key, this.barsCache())
  this.render()
  this.emit()
}
```

Fourteen call sites, mechanical, and it centralises a rule the engine currently keeps by hand.

---

## Interaction

## Creation — three paths in, zero new tool ids

`DRAW_TOOL_IDS` L105-112 is untouched. That avoids the whole fourteen-seam registration cascade: no `wheelCatalog.ts` entry, no `wheels.py:CHART_TOOLS` edit, no `wheels.DOC_VERSION` bump (which regenerates every user's wheel from defaults — the file's own words: "honest data loss"), no new `handleClick` switch arm inside the 1200-char window `selftest.py:1458` is measuring, no `ARMABLE` edit duplicated across two pages.

**1. Inferred at placement — snapping mints the constraint.** `clickTwoPoint` L1624 and `clickHline`/`clickVline` already have the picking they need: run `hitTest(x, y, /*linesOnly*/ true)`, the exact call `snapAnchor` L1479 makes. An endpoint landing within `HIT_PX` (8) of an hline mints `on`. This is what makes "everything easily snaps together" mean something durable — a snap that leaves no object behind un-snaps on the first drag.

> **An inferred constraint that fails admission is dropped SILENTLY.** Only an explicitly requested one is refused out loud. The user did not ask, so refusing would be noise, and SolidWorks inferencing does not nag. One branch, and it removes most of the feature's potential chatter.

**2. Ctrl-select two entities + a hotkey (D3).** `downAdditive` L647 already treats shift/ctrl/meta identically, `toggleSelect` L1672 preserves selection order, `getState().selected` L918 is already the ordered id list, and `hitAny` already discriminates the kind. Free keys — the renderer claims only Escape/Delete/Backspace and the pages claim nothing:

`O` on · `C` coincident · `P` samePrice · `T` sameTime · `L` lock · `D` dimension between the two selected · `N` name this value

All added to the engine's existing `window` keydown, all `preventDefault()`ed, all behind the same INPUT/TEXTAREA/contentEditable guard L723 — and **appended after the Escape ladder, not inserted into it**, because `selftest.py:1551` slices the Escape block to `[:1400]` and requires `setTool('pointer')` inside it; a new rung above the last would turn the gate red on correct code.

**3. Inline one-shot toolbar buttons**, following the `deleteAction` / `Clear M` pattern rather than `PLACE_TOOLS`. Titles must not share a prefix with any existing button: `e2e/run.mjs:937-942` matches `title.startsWith(prefix)` across *every* `<button>` on the page, so two similar titles silently hijack each other's clicks and surface as a chart bug.

## Relations are visible, clickable canvas objects

This is the single biggest graft from Cascade, and it is what makes the thing legible as a sketch rather than as invisible machinery.

| relation | glyph | why it must differ |
|---|---|---|
| `on` | small **open square** straddling the host line at the endpoint | 1 equation — the diagonal still slides |
| `coincident` | filled dot inside a ring | 2 equations — the slide is gone |
| `samePrice` / `sameTime` | paired short parallel ticks at both members | |
| `lock` | small filled triangle at the pinned coordinate | |

Drawn on the existing SVG layer, each pushing a `HotZone {kind:'relation'}` so it can be clicked and deleted. Zones are pushed **after** measures and pins, because `hitAny` L1315-1324 returns the first array-order match and chips are HTML on top of the SVG — a glyph hidden under a chip should lose the pick, since it is hidden anyway.

The glyph distinction carries the design's most important semantics for free. If a snap silently produced a coincidence where the user meant on-line, the slide DOF vanishes for reasons nothing on screen explains.

## Colouring — per entity AND per coordinate, SolidWorks direction

GAUSS had this inverted (fully-defined loud, under-defined dim), so constraining a line made it fade. Corrected:

| status | stroke | note |
|---|---|---|
| in **no** constraint (component of one) | today's `STROKE` at 1.5, **unchanged** | every existing chart looks identical |
| constrained, ≥1 free coordinate | `STROKE` (`var(--accent)`) | SolidWorks blue: work remains |
| fully determined (0 free coordinates) | new `STROKE_FIXED = 'var(--text)'` | SolidWorks black; inverts correctly (`#e8eaed` dark, `#1c1e21` light) |
| implicated in a conflict | `STROKE_DANGER` + `HALO_DANGER` L345 | the engine's existing "this is what will break" ink |
| dangling reference | `MEASURE_STROKE` at 0.55 opacity | the golden-brown slot, no new var |

**Per-coordinate markers** (from Sheet, via Lens 2): a small filled square at each *non-free* coordinate of a partially-constrained drawing. Computed exactly from the null-basis support — a coordinate is free iff some basis vector has a nonzero component on it. It answers the question the user actually has: not *"is this line loose?"* but *"what about it is still loose, and therefore which way will it move when I grab it?"*

**DOF badge**, Inventor-style: a `cd-chip cd-dof` in the pane corner reading `3 free` / `fully defined`, counting **constrained components only** so an untouched chart reads nothing rather than "18 free". Plus `data-draw-dof` on the chart container (`Chart.tsx:417-434`) — the only DOM-observable per-object fact today is the `cd-ax-time` class trick, so a numeric attribute is the cheapest honest e2e handle.

Click the badge and it expands to `SolveReport.motions`, one sentence per null-basis vector, each with exactly one named free variable because the basis is built by setting one free column to 1 and back-substituting:

```
H1's price is free
H2's price is free — the diagonal's end slides 2 bars per dollar to hold slope
The diagonal's start bar is free — it slides along both lines
```

That last construction is Cascade's, and it is what *guarantees* one readable sentence per free variable rather than a rotated mixture. No shipping CAD product does this.

## Value entry — D4, in the dead `selectedMeasures` seam

`DrawState.selectedMeasures` L242 is computed at L912-914 and consumed by nothing; both pages gate `DrawEditor` on `selection.length > 0`, so selecting a dimension opens no panel at all today while `data-draw-selected` reads 1. Zero-conflict landing site.

Change the page condition to `selection.length > 0 || selectedMeasures.length > 0` and render a `MeasureEditor` **in the existing `.draw-editor-float` slot** when the drawing channel is empty. No third position class: `charttools.css:56-63` has exactly two slots and its own comment records they were chosen to avoid a shoving match. **When a drawing and a dimension are both selected, DrawEditor wins** and carries a one-line `1 dimension also selected — press Esc to narrow` row. Stated so it is not discovered as a stacking bug.

`MeasureEditor` reuses `Field` (DrawEditor.tsx:68-134) verbatim, with `place` choosing the label, unit and parser — no "which quantity?" picker anywhere, because `place` already chose which number the chip prints:

| `place` | label | parse |
|---|---|---|
| `axis:'time'` | `Gap` | `parsePrice` (strips one `$`, `Number.isFinite`) |
| `axis:'price'` | `Span` | integer bars, ≥ 1 |
| absent | `Slope` | number, unit from `percentMode()` |

Three `Field` behaviours are load-bearing and must survive: `stopPropagation()` on Escape (DrawEditor.tsx:124) — without it Escape falls through to the engine's three-rung ladder and disarms the tool mid-entry; the `key={\`${id}:${value}\`}` remount idiom (L185/194) so a solve resets the box; and the `.bad` red-border-preserve-the-draft behaviour, exactly right for a refused value. Plus `autoFocus` on first render (the `IndicatorSettings.tsx:80` precedent) — D4's "a real focused `<input>`", and why in-chip digit capture is refused: the engine's keydown guard L723 is **tag-based**, so with a dimension selected, Backspace in a chip runs `deleteSelected()` and deletes the thing being dimensioned.

Under the field: a `Ref` toggle swapping the literal for a named `ChartValue` picker, and two buttons — **Reference** (drop the driver, keep the readout) and **Release** (delete the constraint). Reference is SolidWorks' Make-Driven as a persistent control instead of a modal.

## Named values, and what `parallel` becomes

Press `N` on a selected dimension (or on a value in the panel) to mint a `ChartValue`. A values strip lives in the same float panel: name, unit, number, and "used by 2 dimensions."

**There is no `parallel` relation and there will not be one.** It is bilinear, it introduces a shared unknown, and it is the cheapest way out of every guarantee here. The substitute is one gesture: name one diagonal's slope `S`, then set the other's slope to `S`. Two rows, both affine, and the shared quantity becomes a first-class named object the user can edit once to move both lines — which is also how SolidWorks global variables express the same intent, and is the bridge to trade linkage. When the user asks for `parallel`, the refusal says exactly this and offers to do it.

## Notification — one channel, riding the existing subscriber

`SolveReport.notice` on `DrawState`. No `onNotice`, no second callback: `changeCb` is assigned rather than appended (L925), so a second subscriber would silently replace the page's.

The page renders `notice.text` in a `.draw-cons-note` span beside the existing `.draw-save-err`, sharing its `color: var(--loss)` rule (`charts.css:19-23`). The in-house precedent for a *refusal* rather than an error is `ChartsPage.tsx:388-394` — `'12 symbols max — remove one to add another'`: reason and escape in one string, in a span that already exists. There is no toast anywhere in this app and this design does not add one.

Templates, every one with a number and an escape:

> **Conflict.** `The run is already 8 bars — set by the $4.00 gap and the 0.0769 $/h slope. It cannot be 10. Change the gap, change the slope, or keep this as a reference.`

> **Redundant.** `This gap is already $4.00, set by the slope and the run. Kept as a reference dimension.`

> **Occupied (O(1) prefilter).** `H1's price is already locked at $600.00 — computed twice. Release that lock first.`

> **Blocked drag.** `H2 is held at $604.00 — H1 is locked at $600.00 and the gap is dimensioned at $4.00. Unlock H1 or change the gap.`

> **Range.** `The diagonal's end needs bar 128; the chart ends at bar 118, ten bars short. Load more history or reduce the slope.`

> **Quantized.** `0.0741 $/h achieved — 0.0769 falls between bars 110 and 111. Set the run in bars instead and the slope is exact.`

> **Absorbed.** `H1 moved to $599.80 so the diagonal's end could land on bar 111.`

> **Refused by design.** `Parallel has no value to store. Name this slope and set the other diagonal to the same name — then editing it once moves both.`

## Trim — dangle loudly, do not refuse

Sheet refuses trim on any constrained drawing, which is a hard regression on a shipped gesture. GAUSS dangles silently, which is honest but unhelpful. Take the middle:

`computeTrim` already runs from `render()` L2006 on every crosshair move while trim is armed. Paint every relation and dimension that will dangle in `STROKE_DANGER` **in that live preview**, so the loss is visible *before* the click, and emit a `dangling` notice naming the count on commit. Dangling constraints are dropped from the solve, rendered dim, and never deleted — a re-attach affordance lands in Stage 5.

## Trade linkage — the API shape, from Stage 4 onward

```ts
readValues(): ValueView[]                      // named values + every reference dimension's live number
setValue(name: string, v: number): SolveIssue | null
```

Trades **read outputs** (any coordinate, any reference dimension) and **write inputs** (named values and driving literals only). Writing a coordinate directly is either a no-op (it is determined) or an unattributable over-definition. And only **DOF-0** geometry has stable variable values: an under-defined coordinate is whatever the last drag left it at, so an entity a trade references carries a `fixed`-status requirement and the panel badges it.

## Two live bugs fixed on the way through

Both are in code this feature must touch anyway.

1. **`moveDimension` L1748-1749 persists a poisoned `place`.** It assigns `{axis:'time', at: 0}` *before* resolving the real coordinate, and L1755/1759 `return` on failure with the mutation applied. Drag a free dimension into the whitespace right of the last bar — where `timeAtX` returns null for the entire region — and it locks at epoch 0, `dimSeg` returns null, the measure vanishes, and `endDrag` emits so the broken value reaches the database (`_place` accepts it; 0 is a finite whole second). Fix: compute first, assign once.
2. **`place` can be set but never cleared or flipped.** The locking branch is guarded by `if (!m.place)` and nothing anywhere assigns `undefined`, so a time-locked dimension dragged vertically is silently inert forever. `MeasureEditor` gets an axis toggle and an Unlock — which a driving dimension needs regardless, since changing the axis changes which row it contributes.

---

## Persistence

## Version stays at 1. No migration, because there is nothing to migrate.

`chartobjects.get()` L225-228 returns `empty_doc()` on any version mismatch and `put()` L237-242 then **deletes the row** when the doc is empty. A bump without a migration therefore reads as "all my drawings are gone" and the very next autosave destroys the row that still held them. `selftest.py:2600-2603` additionally pins `CHART_DOC_VERSION === DOC_VERSION`, so they can only move together — meaning the moment they do, every key blanks at once.

Nothing here requires it. Every addition is optional and additive:

| direction | behaviour |
|---|---|
| old row → new code | `constraints`/`values` keys absent → `_list()` L134-137 already returns `[]`. `value`/`valueRef` absent → the reference dimension it already was. Loads byte-identically, renders identically. |
| new row → new code | full fidelity |
| new row → downgraded client | constraints and values are lost — real but silent and non-destructive. Bumping to 2 would instead make that same downgrade read **every** chart as blank and then delete the row. Version 1 is strictly the safer failure mode. |

## What is stored, and what deliberately is not

**Stored:** the constraint graph; named values; driving literals and driving references; and the **solved geometry** in `Drawing.points`, unchanged.

Storing the solved geometry is what makes load free: `r(x_stored) ≈ 0`, so the first Gauss–Newton step is exactly zero and `solve()` returns `x` untouched. Load, hydrate, pan, zoom, resize and every render therefore observe **no solve and no scale factor at all**. Reproducibility is achieved by idempotence, not by asserting that a mixed-unit quantity is physical. And because PIVOT holds free variables at seed rather than minimising, even a *re-*solve from a value edit is deterministic and view-independent, so the same document produces the same geometry on any machine at any zoom.

**Not stored:** a driven/reference dimension's value. It is a solver output; a stored output contradicts its own geometry the moment anything upstream moves. `measureRows` already recomputes it every render. This is *not* the same as the `line` anchor's `time`/`price` snapshot, which is a dangling-reference fallback and is correct — `_anchor` L112-118 already documents it as such.

**Not stored:** DOF, rank, null basis, component membership, admission verdicts, notices. All derived, cached in memory against a module-level `topologyRev`, recomputed once at hydrate.

**Not stored:** a `lock`'s value. It pins whatever is there. A stored absolute price on a `|%` key would reference `bars[0].close` of the first visible line (`Chart.tsx:298`) — a base that is in no document, never versioned, and moves on every refetch or depth change. A valueless lock cannot rot. (A driving *gap* on a `|%` key is in percentage points against that same shifting base, which is no worse than every `Pt.price` on that key already is — but it is why no absolute price is ever stored.)

## Housekeeping that breaks a real thing if missed

Four items, all from Sheet's list, all cheap and all destructive when skipped:

1. **`is_empty()` must count `constraints` and `values`.** `put()` DELETES the row when it is true, so a chart holding only named values (a trade-sizing sheet drawn before any line) would have its row silently removed on the next autosave. This is the one that loses data.
2. **`_id`'s shared `seen` set must cover both new collections.** Ids stay globally unique across five collections now; `mkId`'s single counter already guarantees it, and the flat `selected: string[]` depends on it.
3. **`adoptIds()` L309-316 must spread `b.constraints` and `b.values`.** Without it the first constraint minted after a reload is `cn1` — the id a restored one already has, and the two would select, drag and delete as one. NOTES records that the persistence gate check was a **false green** the first time it was written precisely because its fixture used stored ids that could not collide.
4. **`deleteSelected` / `clickDelete` / `clearDrawings` must cascade** to constraints whose `EntityRef` is doomed. Named values referencing a dead entity are **not** deleted — they dangle and report, which is SolidWorks' golden-brown and is better than silent deletion.

## What the backend can and cannot assert

`validate()` is a **whitelist normalizer**: `put()` stores the reconstructed `clean` dict and `get()` re-validates on read, so any field added on the TypeScript side and not added here is silently deleted on the round-trip — working all session, gone after restart, with no error anywhere. Both files, one commit, or nothing.

**Can assert, cheaply, with no market data:** vocabulary; `EntityPart` legality per `DrawKind` (a `lock` on a `'line'` part of a `trend` is nonsense; `part: 'a'` on an `hline` is nonsense; any part of a `circle` is refused); value-name uniqueness and format; `valueRef` resolves; `value` and `valueRef` are not both present; unit agreement between a `valueRef` and its dimension's `place`-implied unit; and the O(1) **occupancy** rule (no coordinate carries two `lock`s).

**Cannot assert:** rank. A rank test needs the bar series to evaluate the `on`-a-trend rows and the slope rows' `H(i)`, and the backend has no bars. The affine subset has a constant Jacobian and *could* be rank-tested server-side, but a partial guarantee stated as a full one is worse than an honest boundary. So: **solvability is a creation-time invariant held by the engine's admission test, and the validator holds the vocabulary and structural invariants only.** That is a smaller promise than chartobjects.py's docstring might invite, and it is the true one.

---

## Kade's scenario, traced

Chart key `SPY|1Day`, so `TF_MINUTES['1Day'] = 390` min and **one bar is 6.5 chart-hours**. I use the daily key deliberately: it exercises the bars↔chart-hours conversion instead of choosing a timeframe where the factor is 1.

Entities: **H1** (`dw1`, hline), **H2** (`dw2`, hline), **D** (`dw3`, trend).
Parameter vector `x = (p₁, p₂, iₐ, pₐ, i_b, p_b)` — **n = 6**, not 8: H1's and H2's handle *times* are not variables.

Start: H1 = $600, H2 = $604, D from bar 100 ($600) to bar 108 ($604).

---

### Steps 1–2 — snap both ends

`clickTwoPoint`'s first click lands within `HIT_PX` of H1 → mints `on({dw3,'a'} → {dw1,'line'})`. Second click on H2 → `on({dw3,'b'} → {dw2,'line'})`.

```
r₁ = pₐ − p₁      = (−1,  0,  0,  1,  0,  0)
r₂ = p_b − p₂     = ( 0, −1,  0,  0,  0,  1)
```

rank 2, **DOF = 4**. Point-*on*-entity is 1 row, not the 2 of point-point coincidence — that single difference is the entire slide, which is why the two glyphs differ.

### Step 3 — drive the slope

D's chip already reads `+$0.08/h`: rise $4 over 8 bars × 6.5 = 52 chart-hours → 4/52 = **0.076923 $/h**. Select D, type `0.0769` in `Slope`.

```
r₃ = (p_b − pₐ) − 0.076923·6.5·(i_b − iₐ)
   = (p_b − pₐ) − 0.5·(i_b − iₐ)          →  ( 0, 0, +0.5, −1, −0.5, +1)
```

**$0.50 per bar.** Admission: r₁ and r₂ have zero index components, r₃ does not → rank 2 → 3. Accepted. **DOF = 6 − 3 = 3.**

Null basis, one vector per free column, back-substituted:

| | (p₁, p₂, iₐ, pₐ, i_b, p_b) | sentence in the DOF badge |
|---|---|---|
| v₁ | (1, 0, 0, 1, **−2**, 0) | *H1's price is free — A rides with it and the far end pulls back 2 bars per dollar* |
| v₂ | (0, 1, 0, 0, **+2**, 1) | *H2's price is free — the far end runs on 2 bars per dollar* |
| v₃ | (0, 0, 1, 0, **1**, 0) | *The diagonal's start bar is free — it slides along both lines* |

Check v₂: `p_b = 1, pₐ = 0`, so `1 − 0.5·i_b = 0 ⇒ i_b = 2`. One dollar on H2 buys two bars of run, i.e. $0.50/bar ✓. **Kade's prose is a theorem about this system, printed verbatim.**

---

### Step 4 — drag H2 up $2.00

Held: `p₂`. `E·N` at p₂ = (0, 1, 0) → column space is all of R → `dim Rch = 1` on a 1-D handle, feasible, `δ_ok = +2.00`. Exact tracking.

Preference order. BFS from the grabbed entity `dw2`: p₂ = 0 (held → last), p_b = 1 (via r₂), then {pₐ, iₐ, i_b} = 2 (via r₃), then p₁ = 3 (via r₁). endRank: endpoint b (i_b, p_b) = 0, endpoint a (iₐ, pₐ) = 1, line = 2. axisRank at drag phase: `i` before `p`.

> **p_b, i_b, iₐ, pₐ, p₁, [p₂ held]**

QR takes greedily: `p_b` ✓ (r₂). `i_b` ✓ (r₃, coefficient −0.5). `iₐ` — after r₂→p_b and r₃→i_b, only r₁ remains and it has no `iₐ` column → skip. `pₐ` ✓ (r₁). Rank 3.

**Pivots {p_b, i_b, pₐ}. Free {iₐ, p₁} held at seed, p₂ held at the cursor.**

```
p₂  ← 606.00                                  (cursor)
p_b ← p₂ = 606.00                             (r₂)
pₐ  ← p₁ = 600.00                             (r₁, p₁ unchanged at seed)
i_b ← iₐ + (p_b − pₐ)/0.5 = 100 + 12 = 112    (r₃)
```

> **D now spans bar 100 → 112, rise $6.00, run 12 bars, slope 0.076923 $/h. H1 has not moved. The diagonal's start has not moved.**

Exactly Kade's sentence, from an ordering rule rather than a norm. Pull H2 down to $602 → i_b = 104, the diagonal shrinks. Change the slope value to 0.1538 $/h (1.0 $/bar) → i_b = 104.

*Why this needed the ordering.* Pure least-norm — GAUSS's stated method — does not give this. Solutions with Δp₂ = 2 are `2v₂ + a·v₁ + c·v₃`; minimising `w_p(2a² + 8) + w_i(4 − 2a)²` gives `a = 4w_i/(w_p + 2w_i) ≠ 0`, so H1 moves by a weight-dependent amount and the extension is split between both endpoints. GAUSS's walkthrough asserts a = 0 on the grounds that v₁ "costs norm without touching p₂," missing that v₁'s −2 on i_b partially cancels the extension.

### Step 4b — drag H2 by $1.30, where the lattice bites

Same pivots. `p_b ← 605.30`, `pₐ ← 600`, `i_b ← 100 + 5.30/0.5 = 110.6`. Commit:

1. `barIndexOf(110.6)` → in range, nearest 111.
2. Local search over {110, 111, 112} with prices re-solved: 111 gives an exact match (below), so it wins.
3. Freeze `iₐ = 100`, `i_b = 111`; re-solve with **axisRank = price first**. Free now {iₐ, i_b, p₂-held}, pivots {p_b, pₐ, p₁}:

```
p_b ← p₂ = 605.30                                  (r₂)
pₐ  ← p_b − 0.5·(111 − 100) = 605.30 − 5.50 = 599.80   (r₃)
p₁  ← pₐ = 599.80                                  (r₁)
```

Check: rise $5.50 over 11 bars × 6.5 = 71.5 chart-hours → 5.50/71.5 = **0.076923 $/h exactly.** The constraint holds on a real bar; the sub-bar remainder was absorbed by a continuous price.

4. H1 moved $0.20 and the user did not grab it, so:

> `H1 moved to $599.80 so the diagonal's end could land on bar 111.`

That one sentence is the difference between correct behaviour and a bug report.

*And if H1 had been locked* (`r₆: p₁ = 600`): step 3's price re-solve has no free price, so the local search picks the best integer run instead. Rise is fixed at $5.30, so 0.0769 $/h wants `5.30/0.0769 = 68.9 h = 10.6 bars`; 11 bars gives `5.30/71.5 = 0.0741`, 10 gives `0.0815`. Best is 11, and the drag **lands there and reports** rather than reverting:

> `0.0741 $/h achieved — 0.0769 falls between bars 110 and 111. Set the run in bars instead and the slope is exact.`

The escape is real: a driven run is a lattice row, and the gap it then determines is a price, which is continuous.

### Step 5 — slide the diagonal

Grab D's body. Held = all four of D's slots at cursor+δ.

- **Horizontal (+3 bars).** `E·N` spans the index directions via v₃ → feasible. Pivots resolve so both endpoints translate together: 100→103, 112→115, prices unchanged by r₁/r₂. **Rigid slide along both h-lines** — v₃ exercised, no special case written.
- **Vertical (+$2 on both ends).** The price drivers force pₐ, p_b out of the pivot set; BFS from `dw3` puts p₁ and p₂ at distance 1, so they become pivots: `p₁ ← pₐ = 602`, `p₂ ← p_b = 606`, indices unchanged. **The entire figure translates in price — both horizontal lines come with it.** That is "editing where a line sits shifts the whole drawing based on constraints," and it needed nothing but a re-ordered QR.

---

### Step 6 — add the gap dimension, $4.00

Vertical dimension between H1 and H2, `place.axis:'time'`, typed `4.00`.

```
r₄ = p₂ − p₁ − 4 = (−1, +1, 0, 0, 0, 0)
```

Independent? Solve `αr₁ + βr₂ + γr₃ = r₄`: the index components force γ = 0, then α = 1, β = −1, leaving a `pₐ` component of α − γ = 1 ≠ 0. Not representable. **rank 3 → 4, accepted, DOF = 2.** Free motions collapse to *stack* v_s = (1,1,0,1,0,1) and *slide* v₃.

**Drag H2 now.** `E·N` at p₂: v_s has 1 → feasible, exact tracking. BFS order: p_b(1), then i_b/iₐ/pₐ(2), then p₁(3), p₂ held. QR: p_b ✓, i_b ✓ (r₃), pₐ ✓ (r₁), then `iₐ` — dependent (r₃ and r₄ can't both bind it) → skip → p₁ ✓ (r₄). Pivots {p_b, i_b, pₐ, p₁}, free {iₐ, p₂}.

```
p₂  ← 606          p_b ← 606      p₁ ← p₂ − 4 = 602     pₐ ← p₁ = 602
i_b ← 100 + (606 − 602)/0.5 = 108
```

> **H2 tracks the cursor exactly and H1 comes with it. The run stays 8 bars.** The cluster moves rigidly — which is correct CAD behaviour and is *not* a blocked drag. Reporting "blocked" here would be a lie.

### Step 7 — the over-definition Kade will actually hit

Add a horizontal dimension for the run, driving, **10 bars**.

```
r₅ = (i_b − iₐ) − N = (0, 0, −1, 0, +1, 0)
```

Tier-0 prefilter: no duplicate signature, no occupancy → falls through. Tier 1:

> **r₃ = −r₁ + r₂ + r₄ − 0.5·r₅**

Verify: `−r₁ + r₂ + r₄ = (1,0,0,−1,0,0) + (0,−1,0,0,0,1) + (−1,1,0,0,0,0) = (0,0,0,−1,0,1)`; `−0.5·r₅ = (0,0,+0.5,0,−0.5,0)`; sum = `(0,0,0.5,−1,−0.5,1) = r₃` ✓. RHS: `−0 + 0 + 4 − 0.5·N`, which equals r₃'s rhs of 0 exactly when **N = 8**.

Rank stays 4. **DOF = 2, not the 6 − 5 = 1 a counter reports.** {gap, slope, run} satisfy `run × 6.5 × slope = gap`; any two determine the third.

> `The run is already 8 bars — set by the $4.00 gap and the 0.0769 $/h slope. It cannot be 10. Change the gap, change the slope, or keep this as a reference.`

Type `8` instead and the verdict flips from *conflict* to *redundant*: same refusal as driving, but the offer is "kept as a reference dimension," with no complaint about the value. It costs 0 DOF and the badge still reads `2 free`. **A counter cannot tell these two apart; only a rank test can.**

### Step 8 — lock H1, and the drag genuinely blocks

Press `L` on H1 → `r₆: p₁ − 600 = 0`. Rank 5, **DOF = 1**, and the sole null vector is the slide v₃ = (0,0,1,0,1,0) — p₂'s component is 0 in every null vector.

Grab H2. `E·N` at p₂ = {0} → `dim Rch = 0` → **blocked.** Geometry does not move; a dashed red ghost tracks the cursor.

Blame by leave-one-out rank tests:

| remove | p₂ reachable? |
|---|---|
| r₁ (on H1) | no — p₂ = p₁ + 4 = 604 still fixed |
| r₂ (on H2) | no — same |
| r₃ (slope) | no — r₄ + r₆ still pin p₂ |
| **r₄ (gap)** | **yes** |
| **r₆ (lock)** | **yes** |

> `H2 is held at $604.00 — H1 is locked at $600.00 and the gap is dimensioned at $4.00. Unlock H1 or change the gap.`

Two constraints named, both actually load-bearing, neither of them the slope or the run — which the leave-one-out test correctly excludes even though a hand-waved answer would list all five. That is Kade's "if that dimension can't change because of a locking constraint we must notify them," produced by the feasibility test rather than by a special case.

### Step 9 — parallel, without a `parallel` relation

Draw a second diagonal E between the same two lines. Select D's slope, press `N`, name it `S` (unit `slope`, v = 0.076923). Select E's slope dimension, toggle to `Ref`, choose `S`.

Both diagonals now carry `(p_b − pₐ) − S·H(iₐ,i_b) = 0` with `S` a constant. They are parallel, the system stayed affine, editing `S` once moves both, and a trade card can read or write `S` by name. There is no bilinear row and no shared unknown anywhere.

### Step 10 — s = 0

Set `S = 0`. The slope rows' index partials are `±S·c(i)/60 = 0`, so each degenerates to `p_b − pₐ = 0` and — checked — stays independent of the two `on` rows. Rank unchanged, DOF unchanged; only the **free set** changes.

> `Slope 0 spans no time — the diagonal's start and end bars are now both free.`

Nothing divides by `S` anywhere. Cascade must refuse this at admission because `Δt = δ/s` blows up; PIVOT has no such expression, and refusing would refuse a perfectly well-posed system (a flat diagonal between two h-lines simply forces `p₁ = p₂`, which the gap dimension then conflicts with — and *that* conflict is what gets reported, correctly, by the rank test).

---

## Staging

Five stages. Each ships alone, each is useful alone, each has a gate check that goes red when its own invariant is broken.

## Stage 1 — chart-minute coordinates, range-checked bars, `commit()`

No new vocabulary, no new stored kind, no schema change, no solver.

- Build `C[]` (the chart-minute prefix sum) beside `barsCacheTimes` in `barsIdx()` L1090, under the same array-identity invalidation. Rewrite `chartMinutes(a,b)` as `C[ib] − C[ia]`, signature and semantics byte-identical.
- Add `barIndexOf(i, n)` — the never-clamping bar lookup — and route every solver-facing time quantization through it. `nearestBarTime` stays for the handle/edit paths it already owns.
- Add the `commit()` seam and route all fourteen mutators through it.
- Fix `moveDimension`'s `at: 0` poison (compute first, assign once).
- Add the `place` axis toggle and Unlock to `MeasureEditor`'s future slot — today `place` can only ever be set.

**Independently useful:** deletes an O(bars-between) scan that `measureRows` L2161 runs once per measure per render on 8435-bar charts, and stops the engine persisting an invisible epoch-0 dimension whenever a free measure is drag-locked over the right-hand whitespace.

**Gate:** `_chart_time` already runs the real `chartMinutes` under plain node and asserts 5Min ≡ 1Hour ≡ 120, 30×5Min ≡ 10×15Min ≡ 150, the overnight break costing 25 not 1465, sign reversal, and `TF_MINUTES['1Day'] === 390`. **Every one of those must pass unchanged against the prefix-sum implementation** — that is the whole proof the refactor is faithful, and it already exists. Add one case with a compressed intraday gap (where `C` bends) and one asserting `barIndexOf` reports `after` rather than clamping.

## Stage 2 — `lock`, with no matrix at all

`ConstraintKind` (one kind live), `EntityRef`, `EntityPart`, the `constraints` collection, the lockstep `chartobjects.py` edit, `is_empty`/`adoptIds`/`seen` housekeeping, the O(1) occupancy prefilter, the `lock` glyph and its hot zone, `data-draw-dof` and the DOF badge, the bound-coordinate check in `onDown`, and the blocked-drag notice.

A `lock` row is trivially independent unless duplicated, and duplication is caught by the O(1) occupancy test — so **DOF = n − (locked coordinates) is exact with no rank test, no Jacobian, no tolerance.** This is Sheet's Stage 1, and the panel is right that it is the best first ship.

**Independently useful:** *"set a price and it stays set; grab it and the chart tells you why it will not move."* That is Kade's locking-and-notification sentence, complete, with no solver and no expression language.

**Gate:** new `@check("chart constraints: …")`, `checkpoint.json` → `SELFTEST OK 44/44` in the same commit. Vocabulary drift by the one-line regex idiom; a second `lock` on one coordinate returns `duplicate`; the persistence round-trip with a `cn` id; and a mutation test on `adoptIds` (delete the sweep, confirm the check goes red — NOTES records this exact check family shipping as a false green once).

## Stage 3 — the kernel, `on`, and the relations

`analyze()`/`solve()` as module-level exported pure functions in the `slopePerHour` idiom. Union-find components. `on`, `coincident`, `samePrice`, `sameTime`. Inferred-at-placement snapping via `hitTest(x, y, true)`, silently dropping an inferred constraint that fails admission. The preference order, the two-tier pinned drag, the reachable-set projection, leave-one-out blame. Per-entity and per-coordinate colouring. Free-motion sentences on the badge. The ctrl-select + hotkey relations. `dimDrag` gains its missing snapshot. The trim preview paints about-to-dangle relations red.

**Independently useful, and it is most of Kade's ask with no number typed anywhere.** A diagonal snapped to two h-lines follows them, slides freely, and the whole figure translates when you drag its body.

**Gate:** node-level solver check in the `_chart_time` shape — construct the six-parameter scene, assert rank 3 / DOF 3, assert the three null vectors are exactly (1,0,0,1,−2,0) / (0,1,0,0,2,1) / (0,0,1,0,1,0), assert the H2 drag gives `i_b = 112` **with H1 unmoved** (this is the assertion that catches a reversion to least-norm), and assert `iters === 1` on every affine fixture.

## Stage 4 — driving dimensions

`Measure.value` on both sides; `setMeasureValue(id, value | null)`; the two-tier admission test with the implied-value messages; `MeasureEditor` in the `.draw-editor-float` slot reading the previously-dead `selectedMeasures` channel; `Field` reused for gap / span / slope with `autoFocus`; Reference and Release; the commit quantization algorithm with its local search, `range`, `quantized` and `moved` issues.

This is D1, D4 and D5 together, and they are one stage because a value with nowhere to be typed is not shippable and an input with no admission test behind it is worse than no input.

**Gate:** post a doc with a `value` and assert it round-trips — the enum drift check structurally cannot see a dropped scalar, and `validate()`'s whitelist normalization produces exactly the "works all session, gone after restart, no error anywhere" failure. Then the arithmetic: gap + slope + run on one figure yields rank 4 not 5, the run is refused, and the refusal string contains `8`; a `range` overrun reports rather than clamps; the H1-locked drag reports `quantized 0.0741` rather than reverting.

**E2E:** two hlines, a diagonal snapped to both, a typed slope, then drag one hline and assert `data-draw-count` is still 3 while the trend's far endpoint moved and `data-draw-dof` fell. A selection count cannot tell a driven drawing from an undriven one, and that distinction is the whole claim — the `cd-ax-time` class trick is the precedent.

## Stage 5 — named values, and the trade seam

`ChartValue`, `Measure.valueRef`, the `N` hotkey, the values strip in the panel, `parallel`-via-shared-slope with its refusal message, `readValues()`/`setValue()`, the DOF-0 requirement badge on any entity a trade references, and a re-attach affordance for dangling constraints.

**Independently useful:** the end goal — and by this stage it is a projection of a table that already exists rather than a new feature.

**Gate:** unit disagreement between a `valueRef` and its dimension's `place`-implied unit is a 422; two dimensions on one `slope` value produce parallel diagonals under a drag; `setValue` on a name that drives nothing returns an issue rather than silently succeeding.

## Explicitly out of scope

D6 (configurable hotkeys — a shell change routing through main's `before-input-event`, not a chart change), D8 (log scale — the solver is log-safe because it works in data space, but the trend anchor's `u` lerp is not), circles at any stage, `parallel` as a relation, equal-length, tangency, and anything angular.

---

## How this gets tested

## What the offline gate can prove, running the real code

`_chart_time` already establishes the pattern: import `ChartDraw.ts` under plain node ≥22.18 and drive the real methods via `Object.create(ChartDraw.prototype)`, because only the constructor touches the DOM. `selftest.py:2607` forbids `from '../api'` for exactly this reason. Everything in the solver is a module-level export with no DOM, no bundler and no chart handle, so it is the easiest thing in this file to test that way.

**Arithmetic (extend `_chart_time`, no count change):**
- Every existing assertion passes unchanged against the prefix-sum `chartMinutes` — 5Min ≡ 1Hour ≡ 120, 30×5Min ≡ 10×15Min ≡ 150, overnight = 25 not 1465, sign reversal, `TF_MINUTES['1Day'] === 390`. This is the faithfulness proof and it is already written.
- **Do not assert bit-identity.** Subtracting two prefixes is not the same float accumulation as summing `lo..hi`. It happens to be exact here because every term is an integer minute count, but the assertion must not depend on that accident: accumulate `C` in index order and compare with a tolerance of `1e-9 × per`.
- `C` bends where a real gap is shorter than one candle: a half-day fixture must give the same answer as the loop.
- `barIndexOf` reports `{side:'after', byBars:10}` for an index past the last bar and never returns a clamped index.

**Vocabulary drift (extend `_chart_persistence`, no count change):**
```py
ckinds = set(re.findall(r"'(\w+)'", _block(r"export type ConstraintKind =([^\n]+)", "ConstraintKind")))
parts  = set(re.findall(r"'(\w+)'", _block(r"export type EntityPart =([^\n]+)", "EntityPart")))
units  = set(re.findall(r"'(\w+)'", _block(r"export type ValueUnit =([^\n]+)", "ValueUnit")))
assert ckinds == set(co.CONSTRAINT_KINDS)
assert parts  == set(co.ENTITY_PARTS)
assert units  == set(co.VALUE_UNITS)
```
All three declarations must therefore stay **on one line**, exactly like `DrawKind`. Note what this deliberately avoids: no behaviour is duplicated across the language boundary, only enums — which is the whole reason the expression language was refused, since `re.findall` cannot check that a TS parser and a Python extractor agree on what a reference depends on.

**Round-trip, and the scalar the enum check cannot see:**
Post a doc containing a `constraint`, a `value`, a `Measure.value` and a `Measure.valueRef`, and assert **all four come back**. `validate()` rebuilds `clean` and `get()` re-validates on read, so a field added on the TypeScript side only is silently deleted — the enum drift check is structurally blind to it, and NOTES records this exact check family shipping as a false green once. Fixture ids must be what a real first session mints (`dw1/ms2/pin3/cn4/vl5`), not stored ids that cannot collide.

Plus: `value` and `valueRef` together → 422; a `valueRef` naming nothing → 422; a `lock` with `part:'line'` carrying an `axis` → 422; a raw `NaN` still → 422 (existing).

**Solver (new `@check("chart constraints: …")`, `checkpoint.json` → `SELFTEST OK 44/44`):**
1. Kade's six-parameter scene: rank 3, DOF 3.
2. Null basis is **exactly** (1,0,0,1,−2,0) / (0,1,0,0,2,1) / (0,0,1,0,1,0), each with one free column = 1.
3. Drag H2 by +$2.00 → `i_b === 112` **and `p₁ === 600` unchanged**. This is the assertion that pins the preference order; a reversion to least-norm moves p₁ and fails it.
4. `iters === 1` on every affine fixture. This is the assertion that keeps the numeric kernel honest about costing nothing on the linear case, which is the entire objection to using one.
5. gap + slope + run: rank 4 **not 5**, DOF 2 not 1, run refused, refusal text contains `8`. Break-test: replace the rank test with `n − m` counting and confirm the check goes red.
6. Three hlines + three gap dimensions: rank 2, DOF 1, third dimension redundant. The purest form of the same trap, and the one a counter always gets wrong.
7. H1 locked + gap $4: dragging H2 is blocked, and `blame` is **exactly** `{lock, gap}` — not the slope and not the run. Break-test: replace leave-one-out with "every row in the component" and confirm red.
8. `s = 0`: rank unchanged, DOF unchanged, free set changes, no division anywhere (assert no `NaN`/`Infinity` in `x`).
9. Commit quantization: the $1.30 drag lands `i_b === 111`, `p₁ === 599.80`, slope residual `< 1e-9`, and emits a `moved` issue naming `dw1`. With H1 locked, it lands 111 and emits `quantized {asked: 0.0769, achieved: 0.0741}` and **does not revert**.
10. A `range` overrun emits `{code:'range', side:'after'}` and rolls back to the last in-range frame — assert the geometry did not land on the last bar, which is what a `nearestBarTime` regression would produce.

## What needs the node probe rather than the pure gate

The engine's own load/save behaviour, in the shape `_chart_persistence` part 3 already uses: hydrate a bucket carrying constraints and values, assert `adoptIds` moved `nextId` past `cn4`/`vl5`, assert a solve on the hydrated doc is a **no-op** (`iters === 0`, `x` unchanged), and assert `is_empty` is false for a doc carrying only `values` so `put()` does not delete the row.

## What needs e2e

The DOM is the only place three claims are observable:
- **A constraint drove geometry.** Two hlines, a diagonal snapped to both, a typed slope; drag one hline; assert `data-draw-count` is still `3`, `data-draw-dof` fell, and the trend's far endpoint changed. A selection count cannot tell a driven drawing from an undriven one.
- **A relation is a clickable object.** Click the `on` glyph, assert `data-draw-selected` becomes `1`, press Delete, assert `data-draw-dof` rose by exactly 1.
- **A blocked drag notifies and moves nothing.** Lock H1, dimension the gap, drag H2 across a third of the pane; assert `.draw-cons-note` is present, its text contains `$604.00`, and the hline's rendered y is unchanged.

`run.mjs` must use `dispatchMouseEvent` and `scrollIntoView` (pinned at `selftest.py:1413-1415`) — real trusted input, not synthetic events. And a new button's `title` must not prefix-collide with any existing one, since `toolBtn` matches `title.startsWith` across every `<button>` on the page.

## Which mutations must go red — the list to run before believing any green

1. Delete the `adoptIds` sweep over `constraints`/`values` → the round-trip check must fail.
2. Drop `constraints` from `is_empty()` → the "chart of only values survives a save" check must fail.
3. Replace `dof = n − rank(J)` with `n − m` → checks 5 and 6 must both fail.
4. Replace the preference-ordered QR with plain least-norm → check 3 must fail on `p₁ !== 600`.
5. Replace `barIndexOf` with `nearestBarTime` → check 10 must fail.
6. Revert instead of reporting on a commit conflict → check 9's second half must fail.
7. Remove the `value` field from `chartobjects.py` only → the round-trip check must fail while the enum drift check stays green (this is the proof the scalar assertion is doing work the enum comparison cannot).
8. Widen the rank tolerance to `1e-3` → check 5 must fail (the dependency becomes "independent enough").
9. Turn `on` into a two-row coincidence → check 1 must report DOF 2 and fail.

Items 3, 4, 5 and 7 are the four that matter most: each corresponds to a defect a lens found in one of the input designs, and if any of them fails to turn the gate red, the check is not testing what it claims to.

---

## Still open — product calls only Kade can make

- When quantization forces a line you did NOT grab to move (Step 4b: H1 drops $0.20 so the diagonal's end can land on bar 111), is 'move it and say so' the right behaviour, or should the drag clamp at the last exactly-satisfiable position and refuse to go further? Moving-with-a-notice keeps the drag continuous and always lands on a legal configuration; clamping keeps every untouched line stationary but makes the cursor detach from the handle at irregular intervals that depend on the slope. I have implemented move-and-report; the alternative is a one-line change to the commit step and it is a taste call about which surprise is worse.

- Should a lone constrained cluster with no `lock` be allowed to float in price? Two h-lines joined by a gap dimension and nothing else have DOF 1 with the whole stack free, so any edit anywhere can translate them both. CAD's answer is an explicit Fix; the alternative is that the first drawing in a cluster silently gets an implicit price anchor at wherever it was drawn. I chose the explicit `lock` because it is reportable and an implicit anchor is invisible and will read as a bug — but it means the badge will often say '1 free' on a figure the user believes is finished.

- The trend endpoint tiebreak: when the solver must move one end of a diagonal, it moves the end you clicked SECOND and leaves the first as the anchor. That reproduces your scenario exactly when the diagonal is drawn left-to-right, and reverses direction when it is drawn right-to-left (the length still tracks correctly, but the far end runs backwards). Do you want a visible 'swap which end is the anchor' control on a selected trend, or is a `lock` on the endpoint you want held sufficient?

- Should trade linkage be BLOCKED on entities that are not fully defined (DOF 0), or merely badged? An under-defined coordinate is a snapshot of the last drag, not a parameter — it will shift when you edit something unrelated, and a trade sized off it will silently re-size. Blocking is the safe answer and the one every CAD product effectively enforces; badging lets you wire up a half-constrained sketch and accept the drift. This decides whether Stage 5 is usable before a drawing is finished.

- On a `|%` chart key, prices are percentage points against `bars[0].close` of the first visible line — a base that is in no document and moves on every refetch or depth change. Gaps, slopes and every relation are safe because differences cancel, and a valueless `lock` is safe by construction. But a driving GAP typed as '+2.5%' is anchored to a number that is not stored. Do you want driving dimensions allowed on `|%` keys (consistent with every `Pt.price` on that key already having the same exposure), or refused there with a message pointing at the `|$` view?
