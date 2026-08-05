/**
 * ChartDraw v2 — the drawing + measuring engine for every price chart.
 *
 * An SVG overlay (geometry) plus an HTML label layer (measurement chips,
 * candle readouts) sized to pane 0. Objects live in DATA space {time, price}
 * and are projected to pixels only when something changes. Re-render triggers
 * are all event-driven — visible-range changes, ResizeObserver, crosshair
 * moves while a tool is armed, clicks, edits. NO rAF loop, NO setInterval:
 * anything that repaints at rest holds the GPU forever (the spinning-logo
 * lesson). The gate greps this file for polling.
 *
 * ENGINE API (what the pages consume):
 *   new ChartDraw(key, chart, mainSeries, opts?)
 *     opts.bars    Bar[] or () => Bar[] — the bars the chart is showing;
 *                  needed by measure (candle snap, bar counts) and inspect
 *                  (OHLC). A plain array is a snapshot — call setBars() when
 *                  the data refetches. Without bars those features degrade
 *                  honestly: no candle snap, no bar counts, inspect is inert.
 *     opts.percent () => boolean — true when the price axis is % (normalize
 *                  mode); measure labels then show Δ% only. Falls back to the
 *                  charts-page key convention (key ends in '|%').
 *     opts.store   ChartStore — where drawings live across restarts. Omit it
 *                  and the engine is exactly what it was before: everything
 *                  stays in the session Map and dies with the process.
 *   setKey(key)            switch session bucket (symbol/timeframe/scale)
 *   setSeries(series)      re-anchor after Chart.tsx rebuilds its series
 *   setBars(bars)          refresh the bars source on the re-anchor path
 *   setTool(tool)          any DrawTool; arms previews, resets placements
 *   updateDrawing(id, pts) exact-value edit from DrawEditor (times snapped
 *                          to the nearest bar so the point stays projectable)
 *   deleteSelected()       remove every selected drawing
 *   clearSelection()       deselect without deleting (DrawEditor's ×)
 *   clearDrawings()        drawings only (alias clear() kept for old callers)
 *   clearMeasures()        measures + inspect pins only
 *   setDrawingsHidden(b)   hide/show all drawings+measures without deleting
 *   getState()             {tool, drawings, measures, selection, selected,
 *                          hidden} — drawings/measures are COUNTS (they feed
 *                          data-draw-count / data-measure-count directly;
 *                          measures folds in inspect pins, which clearmeasure
 *                          also removes); selection is the selected Drawing
 *                          objects for DrawEditor, selected their ids
 *   onChange(cb)           fires with getState() on ANY state change (and
 *                          once on subscribe, so pages start synced); this is
 *                          how pages feed data-draw-* attrs and DrawEditor
 *   destroy()
 *
 * Buckets are keyed per chart (e.g. "SPY|1Day") in a module-level Map so a
 * tab revisit within the session keeps them. The key must include the
 * timeframe (a 1Min anchor is unprojectable on a 1Day axis) and the axis
 * semantics (a $ anchor is meaningless on a % axis — the charts page keys
 * '|$' / '|%' for exactly that reason).
 *
 * PERSISTENCE (opts.store). The same key addresses a row in the backend's
 * chart_objects table, so the Map is a session cache in front of it, not the
 * home of the data. The Map still WINS while the app is running: it is newer
 * than the server whenever a save is mid-debounce, so a key is loaded at most
 * once per session and a revisit reads memory. What persists is the bucket
 * alone — no selection, no armed tool, no hidden flag, because those describe
 * how you are looking at a chart rather than what you drew on it.
 *
 * TRIM (SolidWorks-style, the arrangement model):
 *   Clicking a line removes ONLY the clicked span — the piece between the
 *   nearest intersections on either side of the click (segment ends when
 *   there is none). All intersection math runs in PIXEL space: intersections
 *   must match what the eye sees, and data-space math breaks the moment the
 *   scale is % or log. The surviving spans come back as independent trend
 *   drawings, and the drawing that provided each cut boundary is split at
 *   that intersection too — so after a trim every visible span is its own
 *   selectable/deletable object (two crossing trends = 4 spans; trim one and
 *   3 remain as 3 separate drawings).
 *   - hline spans become 2-pt trends with equal prices, outer ends bounded
 *     at the DATA extent (first/last loaded bar) — an hline is infinite, the
 *     data is the honest bound.
 *   - vlines have no price extent: spans become equal-time 2-pt trends
 *     (which project as vertical segments), outer ends bounded at the
 *     VISIBLE price range at trim time.
 *   - hlines/vlines with no intersections delete whole on trim, same as a
 *     lone trend.
 *   - circles are excluded from trim v1 entirely (neither trimmable nor a
 *     cut boundary) — ellipse/segment intersection buys little at this stage.
 *   Trend span endpoints are quantized to bar times (times must exist on the
 *   scale to project), and the price is recomputed ON the original pixel
 *   line at the quantized x so the cut edges stay collinear — no kinks.
 *   Spans that quantize away (shorter than a bar / than MIN_SPAN_PX) are
 *   dropped; a boundary drawing whose both halves vanish is kept whole
 *   rather than evaporated by an operation on another line.
 */
import type {
  IChartApi,
  ISeriesApi,
  MouseEventParams,
  SeriesType,
  UTCTimestamp,
} from 'lightweight-charts'
import type { Bar } from './Chart'

// ---------------------------------------------------------------------------
// Public model
// ---------------------------------------------------------------------------

/** No 'select' tool: plain left-click in 'pointer' picks whatever is under it,
 *  so a mode for it would be a mode you always want on. Removed 2026-08-05 —
 *  needing to arm Select first is what "the measurements are not clickable"
 *  actually was. */
export const DRAW_TOOL_IDS = [
  'pointer', 'trend', 'hline', 'vline', 'circle',
  'delete', 'trim', 'measure', 'inspect',
] as const
export type DrawTool = (typeof DRAW_TOOL_IDS)[number]
export function isDrawTool(v: string): v is DrawTool {
  return (DRAW_TOOL_IDS as readonly string[]).includes(v)
}

export interface Pt {
  time: UTCTimestamp
  price: number
}

export type DrawKind = 'trend' | 'hline' | 'vline' | 'circle'

/** trend: 2 pts. hline: 1 pt (price is the line; time only places the
 *  handle). vline: 1 pt (time is the line; price only places the handle).
 *  circle: center + edge, rendered as an ellipse (rx/ry from the projected
 *  deltas, so it tracks zoom in both axes). */
export interface Drawing {
  id: string
  kind: DrawKind
  points: Pt[]
}

/** Measure anchors snap at click time and stay attached:
 *  - candle: the bar's identity; position/price resolve live from its close.
 *  - line: rides the drawing (u = fraction along a trend; hlines pin price,
 *    vlines pin time). time/price are the snap-moment snapshot, used as the
 *    free-anchor fallback if the drawing is later deleted or trimmed away.
 *  - free: a fixed data-space point. */
export type MeasureAnchor =
  | { kind: 'candle'; time: UTCTimestamp }
  | { kind: 'line'; drawingId: string; u: number; time: UTCTimestamp; price: number }
  | { kind: 'free'; time: UTCTimestamp; price: number }

/** Where an axis-locked dimension sits, and which axis it is locked to.
 *
 *  This is the transpose of the line tools that already exist: an hline is one
 *  PRICE (its time only places the handle), a vline is one TIME. A dimension is
 *  the same shape — two prices at one time, or two times at one price:
 *
 *    axis 'time'   VERTICAL at that bar. Ends are the two anchors' PRICES, so
 *                  it measures a price gap. Witness lines run horizontally.
 *    axis 'price'  HORIZONTAL at that price. Ends are the two anchors' TIMES,
 *                  so it measures a span. Witness lines run vertically.
 *    absent        the free diagonal — exactly today's anchor-to-anchor line.
 *
 *  `at` is the SINGLE SHARED COORDINATE, and it is also the drag handle: the
 *  AutoCAD witness offset needs no separate field because the shared coordinate
 *  already is it. Storing a full Pt would duplicate a number the anchors own,
 *  and that copy would be a lie the next time the measured line is dragged.
 *
 *  Nothing angular is stored anywhere, so this cannot rot under zoom, pan or
 *  autoscale drift — a vertical line is a fixed time and a horizontal one a
 *  fixed price, whatever the pixels are doing. */
export type MeasurePlace =
  | { axis: 'time'; at: UTCTimestamp }
  | { axis: 'price'; at: number }

export interface Measure {
  id: string
  a: MeasureAnchor
  b: MeasureAnchor
  /** Absent = the free diagonal. Present = axis-locked, and `at` is where the
   *  dimension line sits. Optional so every existing measure keeps working and
   *  the freehand mode survives untouched. */
  place?: MeasurePlace
}

/** A pinned inspect readout: the bar keeps its identity, OHLC resolves live. */
export interface InspectPin {
  id: string
  time: UTCTimestamp
}

// ---------------------------------------------------------------------------
// Constraints
// ---------------------------------------------------------------------------

/** Two kinds, and the difference between them is the whole model.
 *
 *  'lock' PINS a coordinate: it stops moving.
 *  'on'   EQUATES two coordinates: they become one number, and both are still
 *         free to move — together.
 *
 *  Kade, on why that distinction is the feature: "this is not locking any X or Y
 *  but instead forcing the trend line to stretch to always be in contact with
 *  both h lines." A trend endpoint `on` an hline shares that line's PRICE and
 *  keeps its own TIME, so the h-line can be dragged (the endpoint rides it) and
 *  the endpoint can still slide along it. Locking either would have frozen
 *  exactly what is supposed to move.
 *
 *  Both are EQUALITIES between coordinates, which is why this whole stage needs
 *  no matrix — see analyze(). */
export type ConstraintKind = 'lock' | 'on' | 'slope'

/** Which coordinate-carrying part of a drawing a constraint names. 'line' is a
 *  whole hline/vline — it IS one coordinate, so it has no endpoints, the same
 *  fact POINTS_FOR states on the backend. 'a'/'b' are a trend's two points. */
export type EntityPart = 'line' | 'a' | 'b'

/** Which coordinate of that part. Absent = every coordinate it owns, so a lock
 *  on a trend endpoint with no axis pins the point, and with one pins just its
 *  price or just its date — which is what the editor's per-field padlocks are. */
export type SlotAxis = 'price' | 'time'

export interface EntityRef {
  id: string
  part: EntityPart
  axis?: SlotAxis
}

export interface Constraint {
  id: string
  kind: ConstraintKind
  a: EntityRef
  /** 'on' only: the line the point is held against. */
  b?: EntityRef
  /** 'slope' only: price per HOUR OF CHART TIME, the unit the measure chips
   *  already print. Never an angle — the price scale drifts with no user input
   *  at all, so a stored degree would go false while you merely scroll. */
  value?: number
}

/** Something the engine refused to do, or did with a caveat, in words meant for
 *  a person. Carried on DrawState so a page can show it: a constraint that
 *  silently fails to apply is the one outcome this feature cannot have. */
export interface ConstraintIssue {
  code: 'duplicate' | 'unknown' | 'unsupported' | 'blocked' | 'quantized'
  message: string
}

/** The free coordinates a drawing owns — its solver variables, and the unit DOF
 *  is counted in. A slot id is `<drawingId>:<part>:<axis>`.
 *
 *  Handles are NOT variables. An hline's time and a vline's price only place
 *  the grab handle; if they entered the count, every hline would carry a
 *  permanently free DOF, nothing could ever reach "fully defined", and the whole
 *  status signal would be dead. Circles own none: ellipsePx is this codebase's
 *  own proof the plane is not isotropic, so a circle is not a constrainable
 *  object here. */
export function slotsOf(d: Drawing): string[] {
  if (d.kind === 'hline') return [`${d.id}:line:p`]
  if (d.kind === 'vline') return [`${d.id}:line:i`]
  if (d.kind === 'trend') return [`${d.id}:a:i`, `${d.id}:a:p`, `${d.id}:b:i`, `${d.id}:b:p`]
  return [] // circle
}

const AXIS_CODE: Record<SlotAxis, 'p' | 'i'> = { price: 'p', time: 'i' }

/** The slots one reference names. Empty when it names something that cannot
 *  carry a constraint: a circle, a missing drawing, or an endpoint on an
 *  hline/vline — which have no endpoints, only the one coordinate they ARE. */
export function slotsForRef(ref: EntityRef, byId: Map<string, Drawing>): string[] {
  const d = byId.get(ref.id)
  if (!d || d.kind === 'circle') return []
  let owned: string[]
  if (d.kind === 'trend') {
    owned =
      ref.part === 'line'
        ? slotsOf(d) // the whole segment: both ends
        : [`${d.id}:${ref.part}:i`, `${d.id}:${ref.part}:p`]
  } else {
    if (ref.part !== 'line') return []
    owned = slotsOf(d)
  }
  if (!ref.axis) return owned
  const code = AXIS_CODE[ref.axis]
  return owned.filter((s) => s.endsWith(`:${code}`))
}

/** The equality an 'on' asserts.
 *
 *  The point shares the LINE'S OWN coordinate and keeps the other. An hline is
 *  a price, so a point on it shares a price and stays free in time — and that
 *  surviving freedom is the whole point: it is what lets the trend slide along
 *  while still touching. A vline is the transpose.
 *
 *  ONE equality, never two. Two would be point-to-point coincidence, which
 *  welds the endpoint to a spot and is a different (also useful) relation. */
export function onPair(c: Constraint, byId: Map<string, Drawing>): [string, string] | null {
  if (c.kind !== 'on' || !c.b) return null
  const pt = byId.get(c.a.id)
  const host = byId.get(c.b.id)
  if (!pt || !host || pt.kind !== 'trend') return null
  if (host.kind !== 'hline' && host.kind !== 'vline') return null
  if (c.a.part !== 'a' && c.a.part !== 'b') return null
  const code = host.kind === 'hline' ? 'p' : 'i'
  return [`${pt.id}:${c.a.part}:${code}`, `${host.id}:line:${code}`]
}

export interface SketchAnalysis {
  /** Every slot, mapped to the representative of its equality class. Two slots
   *  sharing a representative are ONE number. */
  rep: Map<string, string>
  /** Representatives of classes that cannot move. */
  pinned: Set<string>
  /** Distinct coordinates before equalities are applied. */
  total: number
  /** Distinct coordinates after: the real variable count. */
  classes: number
  /** What the badge shows. 0 = fully defined. */
  free: number
}

/** Union-find over coordinate slots — and it is EXACT, not an approximation.
 *
 *  Every constraint in this vocabulary is an equality between two coordinates
 *  ('on') or a pin on one ('lock'). For a system of equalities the rank is
 *  exactly (variables − connected components), which is precisely what
 *  union-find computes. So degrees of freedom come out right with no matrix, no
 *  rank tolerance, and no floating-point anywhere near a number shown to a user.
 *
 *  A QR factorisation only becomes necessary when DIMENSIONAL constraints
 *  arrive — a typed slope or gap is not an equality between two variables, it is
 *  a linear relation among several. That is the next stage, and it is the reason
 *  this one can ship without it. */
export function analyze(drawings: Drawing[], constraints: Constraint[]): SketchAnalysis {
  const byId = new Map(drawings.map((d) => [d.id, d]))
  const parent = new Map<string, string>()
  const add = (s: string): void => {
    if (!parent.has(s)) parent.set(s, s)
  }
  const find = (x: string): string => {
    let r = x
    while (parent.get(r) !== r) r = parent.get(r) as string
    let c = x
    while (parent.get(c) !== r) {
      const next = parent.get(c) as string
      parent.set(c, r)
      c = next
    }
    return r
  }
  for (const d of drawings) for (const s of slotsOf(d)) add(s)
  for (const c of constraints) {
    const pair = onPair(c, byId)
    if (!pair) continue
    // An 'on' naming a slot no drawing owns is dangling; ignore rather than
    // inventing the slot, or a deleted line would keep constraining a live one.
    if (!parent.has(pair[0]) || !parent.has(pair[1])) continue
    const ra = find(pair[0])
    const rb = find(pair[1])
    if (ra !== rb) parent.set(ra, rb)
  }
  const pinned = new Set<string>()
  for (const c of constraints) {
    if (c.kind !== 'lock') continue
    for (const s of slotsForRef(c.a, byId)) if (parent.has(s)) pinned.add(find(s))
  }
  const rep = new Map<string, string>()
  const reps = new Set<string>()
  for (const s of parent.keys()) {
    const r = find(s)
    rep.set(s, r)
    reps.add(r)
  }
  // A driving SLOPE is the first constraint here that is not an equality: it is
  // one linear relation over a trend's four coordinates, so it removes one more
  // degree of freedom. Independent unless every coordinate it touches is
  // already pinned, in which case it constrains nothing that could still move.
  // One per trend — a second is refused at admission, so this cannot double-count.
  const driven = new Set<string>()
  for (const c of constraints) {
    if (c.kind !== 'slope' || c.value === undefined || driven.has(c.a.id)) continue
    const d = byId.get(c.a.id)
    if (!d || d.kind !== 'trend') continue
    if (slotsOf(d).some((s) => parent.has(s) && !pinned.has(find(s)))) driven.add(c.a.id)
  }
  return {
    rep,
    pinned,
    total: parent.size,
    classes: reps.size,
    free: reps.size - pinned.size - driven.size,
  }
}

const slotOwner = (slot: string): string => slot.slice(0, slot.indexOf(':'))
const slotPoint = (d: Drawing, slot: string): Pt | undefined =>
  d.points[slot.split(':')[1] === 'b' ? 1 : 0]

export function readSlot(d: Drawing, slot: string): number | null {
  const pt = slotPoint(d, slot)
  if (!pt) return null
  return slot.endsWith(':p') ? pt.price : pt.time
}

export function writeSlot(d: Drawing, slot: string, v: number): void {
  const pt = slotPoint(d, slot)
  if (!pt) return
  if (slot.endsWith(':p')) pt.price = v
  else pt.time = v as UTCTimestamp
}

/** Make every equality true again after `movedIds` changed.
 *
 *  This is the entire solver for this vocabulary, and it is one pass: slots in
 *  a class are the same number, so a class containing something that moved
 *  simply takes that value everywhere. Drag an h-line and the trend endpoint
 *  held onto it comes along — "the trend line must stretch to always be in
 *  contact" — while the endpoint's TIME, which is in no class, does not move.
 *
 *  Directional on purpose: what the user grabbed is the truth, and everything
 *  attached follows. A class with two moved members takes the first, which is
 *  arbitrary but only reachable by dragging both ends of one equality at once. */
export function propagate(
  drawings: Drawing[],
  constraints: Constraint[],
  movedIds: Set<string>
): void {
  const a = analyze(drawings, constraints)
  const byId = new Map(drawings.map((d) => [d.id, d]))
  const truth = new Map<string, number>()
  for (const [slot, r] of a.rep) {
    if (!movedIds.has(slotOwner(slot)) || truth.has(r)) continue
    const d = byId.get(slotOwner(slot))
    const v = d ? readSlot(d, slot) : null
    if (v !== null) truth.set(r, v)
  }
  if (truth.size === 0) return
  // Writes to the moved slots too, deliberately. Skipping them looks like an
  // optimisation and is a bug: drag two drawings that share a class and the
  // second would keep its own value, leaving the equality false. Writing the
  // class value everywhere is a no-op for one mover and a repair for two.
  for (const [slot, r] of a.rep) {
    const v = truth.get(r)
    if (v === undefined) continue
    const d = byId.get(slotOwner(slot))
    if (d) writeSlot(d, slot, v)
  }
}

/** What the DOF badge reads. `locked` counts SLOTS held (so a pinned class of
 *  two reports two), `free` counts CLASSES still able to move. */
export function degreesOfFreedom(
  drawings: Drawing[],
  constraints: Constraint[]
): { total: number; locked: number; free: number } {
  const a = analyze(drawings, constraints)
  let locked = 0
  for (const r of a.rep.values()) if (a.pinned.has(r)) locked++
  return { total: a.total, locked, free: a.free }
}

/** What one chart's persisted objects look like on the wire. It is the bucket,
 *  nothing more — no selection, no tool, no hidden flag: those are how you are
 *  looking at the chart right now, not what you drew on it. */
export interface ChartDoc {
  version?: number
  drawings: Drawing[]
  measures: Measure[]
  pins: InspectPin[]
  constraints: Constraint[]
}

/** Persistence, INJECTED rather than imported.
 *
 *  The engine reaches nothing outside lightweight-charts and its own types,
 *  which is what lets the gate run its arithmetic under plain node with no
 *  bundler and no jsdom. An `import { api }` here would end that, so the pages
 *  hand in an adapter (renderer/src/chartStore.ts) and the engine keeps its
 *  one dependency direction.
 *
 *  `load` resolving null means "nothing saved" — the same as an empty doc.
 *  Errors belong to the adapter: it owns the UI that reports them. */
export interface ChartStore {
  load: (key: string) => Promise<ChartDoc | null>
  save: (key: string, doc: ChartDoc) => Promise<void>
}

/** What a pick can land on. Drawings carry the nearest-point/parameter that
 *  trim and measure-snap need; measures and pins are pick-or-not. `id` is on
 *  every arm so the selection can stay a flat string[] — mkId's single counter
 *  makes ids unique across all three collections, so no per-kind bookkeeping
 *  is needed anywhere downstream. */
export type Hit =
  | { kind: 'drawing'; id: string; drawing: Drawing; dist: number; nx: number; ny: number; u: number }
  | { kind: 'measure'; id: string; measure: Measure; dist: number }
  | { kind: 'pin'; id: string; pin: InspectPin; dist: number }

/** A chip's last drawn rectangle. Held as a {kind, id} REFERENCE rather than an
 *  object pointer: resolving through the bucket at read time means a stale rect
 *  left by a deleted measure picks nothing instead of resurrecting a ghost. */
interface HotZone {
  left: number
  top: number
  w: number
  h: number
  kind: 'measure' | 'pin'
  id: string
}

export interface DrawState {
  tool: DrawTool
  /** COUNT of drawings — feeds data-draw-count as-is. */
  drawings: number
  /** COUNT of measures + inspect pins — feeds data-measure-count as-is
   *  (pins are measurement annotations: clearmeasure removes both). */
  measures: number
  /** The selected DRAWING objects, in selection order — feeds DrawEditor's
   *  coordinate boxes, which are drawing vocabulary and stay typed to it. */
  selection: Drawing[]
  /** Selected measures / pins, as parallel channels. A measure has no points[]
   *  to edit, so folding these into `selection` would poison every existing
   *  consumer for no gain. */
  selectedMeasures: Measure[]
  selectedPins: InspectPin[]
  /** EVERY selected id, whatever kind. This — not selection.length — is the
   *  honest "is anything selected". */
  selected: string[]
  hidden: boolean
  /** Sketch status, CAD-style. `free` is what the badge shows: 0 means every
   *  coordinate is pinned and nothing can shift under you. */
  dof: { total: number; locked: number; free: number }
  /** Drawings carrying at least one LOCK — what the page paints a padlock on. */
  lockedIds: string[]
  /** Every pinned coordinate slot, `<id>:<part>:<p|i>`. The editor reads this
   *  to light the right per-field padlock, including coordinates held only
   *  THROUGH an equality — a price pinned by a lock on the line it rides is
   *  just as immovable, and showing it unlocked would be a lie. */
  lockedSlots: string[]
  /** The last refusal or caveat, in words. Null when the engine has nothing to
   *  say; cleared by the next successful action. */
  issue: ConstraintIssue | null
}

// ---------------------------------------------------------------------------
// Internals
// ---------------------------------------------------------------------------

interface Bucket {
  drawings: Drawing[]
  measures: Measure[]
  pins: InspectPin[]
  constraints: Constraint[]
}

const sessionStore = new Map<string, Bucket>()

function bucketFor(key: string): Bucket {
  let b = sessionStore.get(key)
  if (!b) {
    b = { drawings: [], measures: [], pins: [], constraints: [] }
    sessionStore.set(key, b)
  }
  return b
}

const isEmptyBucket = (b: Bucket): boolean =>
  b.drawings.length === 0 &&
  b.measures.length === 0 &&
  b.pins.length === 0 &&
  b.constraints.length === 0

let nextId = 1
const mkId = (prefix: string) => `${prefix}${nextId++}`

// ---- persistence ----------------------------------------------------------
// Module-level, not per-instance, because split view puts TWO engines on one
// key: they share the bucket already, so they must share the "have we loaded
// this?" and "what does the server hold?" answers too, or one instance's load
// clobbers the other's edits and both race to write.

/** Bumped in step with chartobjects.DOC_VERSION on the backend. */
export const CHART_DOC_VERSION = 1

/** Coalesces a burst of edits into one write. Every mutator emits, and a drag
 *  commit emits once, so this is about typing/rapid clicks rather than frames. */
const SAVE_DEBOUNCE_MS = 400

/** key -> the in-flight (or settled) first load for that key. */
const loadedKeys = new Map<string, Promise<void>>()
/** key -> the serialized doc last handed to the store, so an emit that changed
 *  only the SELECTION or the armed tool costs nothing. */
const savedDocs = new Map<string, string>()

const docOf = (b: Bucket): ChartDoc => ({
  version: CHART_DOC_VERSION,
  drawings: b.drawings,
  measures: b.measures,
  pins: b.pins,
  constraints: b.constraints,
})

/** Move the id counter past everything we just loaded.
 *
 *  Without this, a reload is a duplicate-id generator: `nextId` is a fresh
 *  module counter starting at 1, so the first new drawing on a restored chart
 *  is 'dw1' — the id the restored one already has. The selection is a flat
 *  string[] matched by id, so the two would select, drag and delete as one. */
function adoptIds(b: Bucket): void {
  let max = 0
  for (const o of [...b.drawings, ...b.measures, ...b.pins, ...b.constraints]) {
    const n = Number(/\d+$/.exec(o.id)?.[0] ?? '0')
    if (Number.isFinite(n) && n > max) max = n
  }
  if (max >= nextId) nextId = max + 1
}

/** Write one key's bucket if it differs from what the store last took. */
function saveNow(store: ChartStore, key: string): void {
  const b = sessionStore.get(key)
  if (!b) return
  const doc = docOf(b)
  const ser = JSON.stringify(doc)
  if (savedDocs.get(key) === ser) return
  // Recorded BEFORE the await so a burst does not queue N identical writes.
  // Dropped again on failure, so the next edit retries instead of believing a
  // write that never landed.
  savedDocs.set(key, ser)
  store.save(key, doc).catch(() => savedDocs.delete(key))
}

const SVG_NS = 'http://www.w3.org/2000/svg'
/** A press that travelled further than this before release is a pan. */
const CLICK_SLOP_PX = 4
/** Generous hit target for select/delete/trim and line snapping. */
const HIT_PX = 8
/** Horizontal distance to a bar center that snaps a measure anchor to it. */
const CANDLE_SNAP_PX = 6
/** Trim spans shorter than this are slivers, not drawings. */
const MIN_SPAN_PX = 12
const TRIM_EPS = 1e-6
const ELLIPSE_HIT_SAMPLES = 48

const STROKE = 'var(--accent)'
const STROKE_DANGER = 'var(--loss)'
const HALO = 'color-mix(in srgb, var(--accent) 25%, transparent)'
const HALO_DANGER = 'color-mix(in srgb, var(--loss) 30%, transparent)'
const MEASURE_STROKE = 'var(--text-dim)'

/** Nominal minutes of CHART time in one candle, per timeframe.
 *
 *  1Day is 390 — one regular US session — not 1440. Chart time is time that
 *  has candles in it, and a daily candle holds a session, not a calendar day.
 *  That is what lets the same line read the same slope on any timeframe:
 *  measure the same two moments on 5Min and on 1Day and the minutes agree.
 *
 *  Nominal, not a claim about any particular day: half-days are 210 minutes
 *  and an extended-hours feed prints more intraday candles than a regular
 *  session. chartMinutes() only ever uses it as a CEILING on a real gap, so
 *  an approximate constant cannot inflate a span — it can only stop one.
 *
 *  Deliberately NOT shared with backend/recorder.py's duration map, where
 *  1Day is 86400 because that answers a SCHEDULING question. Both numbers are
 *  right for their own question; merging them would break one of the two. */
export const TF_MINUTES: Record<string, number> = {
  '1Min': 1,
  '5Min': 5,
  '15Min': 15,
  '1Hour': 60,
  '1Day': 390,
}

/** Price change per hour of CHART time. Pure, and exported, so the gate can
 *  exercise the arithmetic directly instead of grepping for it. Null rather
 *  than Infinity on a zero span: two points in the same candle have no slope,
 *  and a chip reading "$Infinity/h" would be worse than an absent row. */
export function slopePerHour(dPrice: number, chartMins: number): number | null {
  if (!Number.isFinite(dPrice) || !Number.isFinite(chartMins)) return null
  if (chartMins === 0) return null
  return (dPrice / chartMins) * 60
}

/** Cumulative chart minutes: `out[k]` is the chart time from bar 0 to bar k.
 *
 *  The same arithmetic chartMinutes() summed step by step, hoisted into a prefix
 *  sum so a span is one subtraction instead of a walk — measureRows calls it per
 *  measure per render, and NOTES records 8435-bar charts.
 *
 *  It is also the coordinate in which a SLOPE is linear, which bar index is not
 *  and epoch seconds are not. On clean regular bars every step contributes one
 *  candle (`gap === per` fails `gap < per`), so this is exactly proportional to
 *  bar index; it bends only where a real gap is SHORTER than a candle — half
 *  sessions, extended-hours feeds, resampled series. Pure and exported so the
 *  gate can check it against the loop it replaced rather than trusting it. */
export function chartMinutePrefix(times: number[], per: number): number[] {
  const out = new Array<number>(times.length)
  out[0] = 0
  for (let i = 0; i + 1 < times.length; i++) {
    const gap = (times[i + 1] - times[i]) / 60
    out[i + 1] = out[i] + (gap > 0 && gap < per ? gap : per)
  }
  return out
}

/** Trading days from one calendar date to another — the arithmetic that maps
 *  an option EXPIRATION into the chart's bar-index space, where a leg object
 *  past the last candle has to live.
 *
 *  Weekends are skipped; market holidays are NOT modelled, so a holiday inside
 *  the span costs one bar of error. That is the honest trade: a real exchange
 *  calendar is a data dependency this file must not grow (it runs under plain
 *  node in the gate), and one bar at 30 DTE is smaller than the acceptance
 *  window it positions. Signed: a target before the base counts negative.
 *
 *  Dates are 'YYYY-MM-DD', compared in UTC — the same convention the OCC
 *  parser and the chain endpoint already use. A weekend target contributes no
 *  weekdays beyond the Friday before it, so a Saturday expiration (legacy
 *  contracts) lands on Friday's bar, which is where it trades. */
export function tradingDayOffset(fromDate: string, toDate: string): number | null {
  const a = Date.parse(fromDate + 'T00:00:00Z')
  const b = Date.parse(toDate + 'T00:00:00Z')
  if (!Number.isFinite(a) || !Number.isFinite(b)) return null
  const sign = b >= a ? 1 : -1
  const [lo, hi] = b >= a ? [a, b] : [b, a]
  let n = 0
  for (let t = lo + 86400_000; t <= hi; t += 86400_000) {
    const dow = new Date(t).getUTCDay()
    if (dow !== 0 && dow !== 6) n++
  }
  return sign * n
}

/** The calendar date N trading days after (or before, negative) a base date —
 *  the inverse of tradingDayOffset, used when a leg is DRAGGED along the time
 *  axis and its pixel position has to become an expiration again. */
export function dateAtTradingOffset(fromDate: string, offset: number): string | null {
  let t = Date.parse(fromDate + 'T00:00:00Z')
  if (!Number.isFinite(t) || !Number.isFinite(offset)) return null
  const step = offset >= 0 ? 86400_000 : -86400_000
  let left = Math.round(Math.abs(offset))
  while (left > 0) {
    t += step
    const dow = new Date(t).getUTCDay()
    if (dow !== 0 && dow !== 6) left--
  }
  return new Date(t).toISOString().slice(0, 10)
}

/** Where a FRACTIONAL bar index lands on the loaded lattice, or which side it
 *  fell off and by how much.
 *
 *  Never clamps silently, which is the whole reason it exists beside
 *  nearestBarTime: that one pins to ts[0]/ts[last] and never says it did. For
 *  the handle it places, clamping is fine. For anything a constraint has to
 *  SATISFY it is not — running out of loaded history is a validity failure the
 *  user has to be told about ("the end needs bar 128; the chart ends at 118"),
 *  not something to paper over with a wrong answer that looks right. */
export function barIndexOf(
  i: number,
  n: number
): { i: number } | { side: 'before' | 'after'; byBars: number } {
  if (n <= 0) return { side: 'before', byBars: 0 }
  const r = Math.round(i)
  if (r < 0) return { side: 'before', byBars: -r }
  if (r > n - 1) return { side: 'after', byBars: r - (n - 1) }
  return { i: r }
}

interface XY {
  x: number
  y: number
}
interface Seg {
  a: XY
  b: XY
}

function distToSeg(px: number, py: number, a: XY, b: XY): { dist: number; t: number; nx: number; ny: number } {
  const dx = b.x - a.x
  const dy = b.y - a.y
  const len2 = dx * dx + dy * dy
  const t = len2 === 0 ? 0 : Math.max(0, Math.min(1, ((px - a.x) * dx + (py - a.y) * dy) / len2))
  const nx = a.x + t * dx
  const ny = a.y + t * dy
  return { dist: Math.hypot(px - nx, py - ny), t, nx, ny }
}

/** Segment-segment intersection, standard param form; returns t along the
 *  TARGET or null. Parallel lines return null — no single crossing to cut at. */
function segSegIntersect(target: Seg, other: Seg): number | null {
  const rx = target.b.x - target.a.x
  const ry = target.b.y - target.a.y
  const sx = other.b.x - other.a.x
  const sy = other.b.y - other.a.y
  const denom = rx * sy - ry * sx
  if (Math.abs(denom) < 1e-9) return null
  const qx = other.a.x - target.a.x
  const qy = other.a.y - target.a.y
  const t = (qx * sy - qy * sx) / denom
  const u = (qx * ry - qy * rx) / denom
  if (t < -1e-9 || t > 1 + 1e-9 || u < -1e-9 || u > 1 + 1e-9) return null
  return t
}

const lerpSeg = (s: Seg, t: number): XY => ({
  x: s.a.x + (s.b.x - s.a.x) * t,
  y: s.a.y + (s.b.y - s.a.y) * t,
})

// ---- formatting -----------------------------------------------------------

function fmtNum(v: number): string {
  const a = Math.abs(v)
  return v.toFixed(a >= 1 || a === 0 ? 2 : 4)
}
function signOf(v: number): string {
  return v < 0 ? '-' : '+'
}
function fmtVol(v: number): string {
  if (v >= 1e9) return `${(v / 1e9).toFixed(1)}B`
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`
  if (v >= 1e3) return `${(v / 1e3).toFixed(1)}K`
  return String(Math.round(v))
}
/** UTC on purpose: bar timestamps are UTC and the axis shows UTC — a local-
 *  time label here would disagree with the scale under it. */
function fmtDate(t: number): string {
  const d = new Date(t * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  const base = `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())}`
  return d.getUTCHours() === 0 && d.getUTCMinutes() === 0 ? base : `${base} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`
}
function fmtSpan(sec: number): string {
  const s = Math.abs(sec)
  if (s < 60) return '0m'
  if (s < 86400) {
    const h = Math.floor(s / 3600)
    const m = Math.round((s % 3600) / 60)
    return h > 0 ? `${h}h ${m}m` : `${m}m`
  }
  return `${Math.round(s / 86400)}d`
}

interface ChipRow {
  text: string
  cls?: string
}

interface ResolvedAnchor {
  x: number
  y: number
  price: number
  time: UTCTimestamp
}

// ---------------------------------------------------------------------------
// The engine
// ---------------------------------------------------------------------------

export class ChartDraw {
  readonly chart: IChartApi
  private series: ISeriesApi<SeriesType>
  private key: string
  private tool: DrawTool = 'pointer'
  private barsOpt?: () => Bar[]
  private percentOpt?: () => boolean

  /** First click of a two-click placement (trend/circle). */
  private pendingPt: Pt | null = null
  /** The line the first trend click snapped onto, held until the second. */
  private pendingHost: string | null = null
  /** First click of a measure. */
  private pendingAnchor: MeasureAnchor | null = null
  private cursor: { x: number; y: number; time: UTCTimestamp | null } | null = null
  private selected: string[] = []
  /** Chip rectangles from the LAST COMPLETED render. Chips are HTML in a
   *  pointer-events:none layer, so remembering where they were drawn is the
   *  only way to click one. Published atomically at the END of render() from
   *  zoneDraft — picking against a half-built list would resolve to the wrong
   *  object, or to none. */
  private hotZones: HotZone[] = []
  private zoneDraft: HotZone[] = []
  /** What the cursor is over in pointer mode, or null. This exists to keep the
   *  default tool cheap: pointer now picks, so it has to follow the crosshair,
   *  but re-rendering on every move would put a full overlay rebuild in the
   *  app's resting mode. Rendering only when this ID *changes* means an idle
   *  cursor over empty chart costs one hit-test per move and no paint. */
  private hoverId: string | null = null
  private hidden = false
  private changeCb: ((s: DrawState) => void) | null = null

  private readonly host: HTMLElement
  private readonly svg: SVGSVGElement
  private readonly labels: HTMLDivElement
  private readonly ro: ResizeObserver
  private downAt: XY | null = null
  /** Was a modifier held on the mousedown that started this click? */
  private downAdditive = false
  /** A drag, PRIMED on mousedown over a drawing and only `live` once the
   *  pointer has travelled past CLICK_SLOP_PX. Priming without going live is
   *  what keeps a plain click a click: same gesture, and the distance decides.
   *  `orig` snapshots the points at grab time so every frame translates from
   *  the original rather than accumulating rounding. */
  private drag: {
    ids: string[]
    orig: Map<string, Pt[]>
    from: XY
    live: boolean
  } | null = null
  /** The library fires a click on the mouseup that ends a drag. The 4px guard
   *  in handleClick does not cover it (a 2px drag still moved something), so a
   *  real drag says so explicitly and the next click is swallowed. */
  private justDragged = false
  /** Dragging a DIMENSION (the measure), not the things it measures.
   *  `grab` is the cursor-minus-dimension offset captured at mousedown. It is
   *  not optional polish: the chip handle sits ~25-30px ABOVE the dimension
   *  line it belongs to, so driving `at` straight from the cursor would
   *  teleport the dimension up to meet the pointer on the first live frame —
   *  the same reason the drawing drag snapshots `orig` rather than tracking
   *  raw coordinates. */
  private dimDrag: {
    id: string
    from: XY
    grab: XY
    live: boolean
  } | null = null
  private teardown: (() => void)[] = []
  private store?: ChartStore
  private issue: ConstraintIssue | null = null
  private saveTimer: ReturnType<typeof setTimeout> | null = null
  /** Set by destroy(), read by the load continuation — a response can outlive
   *  the engine that asked for it. */
  private destroyed = false

  // bars index cache, keyed by array identity — pages hand the same array
  // until data actually changes, so this rebuilds only on real reloads.
  private barsCacheSrc: Bar[] | null = null
  private barsCacheTimes: number[] = []
  private barsCacheMap = new Map<number, number>()
  private barsCacheMins: number[] = []
  /** The candle length barsCacheMins was built with; null = not built. */
  private barsCachePer: number | null = null

  constructor(
    key: string,
    chart: IChartApi,
    series: ISeriesApi<SeriesType>,
    opts?: {
      bars?: Bar[] | (() => Bar[])
      percent?: () => boolean
      store?: ChartStore
    }
  ) {
    this.key = key
    this.chart = chart
    this.series = series
    this.setBarsSource(opts?.bars)
    this.percentOpt = opts?.percent
    this.store = opts?.store
    this.host = chart.chartElement()
    if (getComputedStyle(this.host).position === 'static') this.host.style.position = 'relative'

    this.svg = document.createElementNS(SVG_NS, 'svg')
    const st = this.svg.style
    st.position = 'absolute'
    st.left = '0'
    st.top = '0'
    st.overflow = 'hidden'
    st.pointerEvents = 'none' // clicks arrive via subscribeClick, never the DOM
    st.zIndex = '3' // above the library's canvases
    this.host.appendChild(this.svg)

    // HTML layer for measurement chips / readouts — text layout and theming
    // belong to CSS, not to hand-measured SVG rects. Class styles it
    // (charts.css); the load-bearing bits are inlined so a missing import
    // cannot smear block text across the chart.
    this.labels = document.createElement('div')
    this.labels.className = 'cd-labels'
    const ls = this.labels.style
    ls.position = 'absolute'
    ls.left = '0'
    ls.top = '0'
    ls.overflow = 'hidden'
    ls.pointerEvents = 'none'
    ls.zIndex = '4'
    this.host.appendChild(this.labels)

    const ts = chart.timeScale()
    const onRange = () => this.render()
    ts.subscribeVisibleLogicalRangeChange(onRange)
    this.teardown.push(() => ts.unsubscribeVisibleLogicalRangeChange(onRange))

    const onClick = (p: MouseEventParams) => this.handleClick(p)
    chart.subscribeClick(onClick)
    this.teardown.push(() => chart.unsubscribeClick(onClick))

    // Live previews: every armed tool tracks the crosshair (hline/vline
    // preview BEFORE the first click — a blind placement was the core of the
    // clunkiness complaint). Pointer mode is an immediate no-op, so there is
    // no per-move cost at rest.
    const onMove = (p: MouseEventParams) => {
      const ok = p.point !== undefined && (p.paneIndex ?? 0) === 0
      const next = ok
        ? { x: p.point!.x, y: p.point!.y, time: typeof p.time === 'number' ? p.time : null }
        : null
      if (this.tool === 'pointer') {
        // Pointer is the resting mode of the whole app, and it now picks, so
        // it has to know what is under the cursor. Paint ONLY when that answer
        // changes: an unconditional render here is a full overlay rebuild at
        // crosshair rate, which is the cost the no-idle-repaint rule exists to
        // avoid. Over empty chart the answer is null every time and nothing
        // repaints at all.
        this.cursor = next
        const id = next ? (this.hitAny(next.x, next.y)?.id ?? null) : null
        if (id === this.hoverId) return
        this.hoverId = id
        this.applyCursor() // pickable-vs-not has to reach the real cursor
        this.render()
        return
      }
      if (this.hoverId !== null) this.hoverId = null
      if (next === null && this.cursor === null) return
      this.cursor = next
      this.render()
    }
    chart.subscribeCrosshairMove(onMove)
    this.teardown.push(() => chart.unsubscribeCrosshairMove(onMove))

    // Click-vs-pan guard: the library's click callback also fires on the
    // mouseup that ends a drag-pan, which would plant an anchor at every
    // pan-end while a tool is armed.
    const onDown = (e: MouseEvent) => {
      const r = this.host.getBoundingClientRect()
      const at = { x: e.clientX - r.left, y: e.clientY - r.top }
      this.downAt = at
      // The library's click params carry no modifier keys, and this mousedown
      // is the only place a REAL MouseEvent reaches us before the click. Held
      // here so clickSelect can tell "add to the selection" from "replace it".
      this.downAdditive = e.shiftKey || e.ctrlKey || e.metaKey
      this.endDrag(false)
      // Only pointer grabs. Any armed tool is placing geometry, and a modifier
      // means "extend the selection", never "move it".
      if (e.button !== 0 || this.tool !== 'pointer' || this.downAdditive) return
      const hit = this.hitAny(at.x, at.y)
      // A pin's position IS its bar, so "dragging" one would mean re-pinning it
      // somewhere else — deliberately not a gesture.
      if (!hit || hit.kind === 'pin') return
      if (hit.kind === 'measure') {
        // Capture where the dimension line sits RIGHT NOW so the drag moves it
        // by a delta instead of snapping it under the cursor.
        const cur = this.dimAnchorPx(hit.measure)
        this.dimDrag = {
          id: hit.id,
          from: at,
          grab: cur ? { x: at.x - cur.x, y: at.y - cur.y } : { x: 0, y: 0 },
          live: false,
        }
        try {
          this.chart.applyOptions({ handleScroll: false, handleScale: false })
        } catch { /* see below */ }
        return
      }
      // Grabbing something already selected moves the WHOLE selection - the
      // same rule clickDelete uses, so "what will this act on" has one answer.
      const b = this.bucket()
      const wanted = this.selected.includes(hit.id)
        ? this.selected.filter((id) => b.drawings.some((d) => d.id === id))
        : [hit.id]
      // A locked drawing does not move, and it says so. Decided BEFORE the pan
      // is suspended and before any pixel is captured: a lock that let the drag
      // start and then snapped back would read as jitter, not as a constraint.
      const { ids, issue } = this.movableIds(wanted)
      if (issue) {
        this.issue = issue
        this.emit()
        return
      }
      const orig = new Map<string, Pt[]>()
      for (const id of ids) {
        const d = b.drawings.find((x) => x.id === id)
        if (d) orig.set(id, d.points.map((p) => ({ ...p })))
      }
      if (orig.size === 0) return
      // A successful grab clears a stale refusal; leaving it up would make the
      // notice describe an action two gestures ago.
      this.issue = null
      this.drag = { ids, orig, from: at, live: false }
      // Suspend pan/zoom NOW, not at the slop threshold: the chart pans on its
      // own mousemove, so waiting would let it slide a few pixels before we
      // decide this was a drag. Restored on mouseup whether or not it went live.
      try {
        this.chart.applyOptions({ handleScroll: false, handleScale: false })
      } catch { /* older builds: the drag still works, the chart just pans too */ }
    }
    this.host.addEventListener('mousedown', onDown)
    this.teardown.push(() => this.host.removeEventListener('mousedown', onDown))

    // move/up live on WINDOW: a drag that leaves the chart (or ends over the
    // price scale) must still track and still commit, or the drawing sticks to
    // the cursor after the button is already up.
    const onDragMove = (e: MouseEvent) => {
      const g = this.drag ?? this.dimDrag
      if (!g) return
      const r = this.host.getBoundingClientRect()
      const x = e.clientX - r.left
      const y = e.clientY - r.top
      const dx = x - g.from.x
      const dy = y - g.from.y
      if (!g.live) {
        if (Math.hypot(dx, dy) <= CLICK_SLOP_PX) return // still a click
        g.live = true
      }
      if (this.drag) this.moveDragged(dx, dy)
      else this.moveDimension(x, y)
    }
    const onDragUp = () => this.endDrag(true)
    window.addEventListener('mousemove', onDragMove)
    window.addEventListener('mouseup', onDragUp)
    this.teardown.push(() => window.removeEventListener('mousemove', onDragMove))
    this.teardown.push(() => window.removeEventListener('mouseup', onDragUp))

    // Escape cancels an in-progress placement, then clears selection.
    // Delete/Backspace removes the selection. Both ignore typing targets so
    // the DrawEditor's inputs keep their own Escape/Delete semantics.
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return
      if (e.key === 'Escape') {
        if (this.pendingPt !== null || this.pendingAnchor !== null) {
          this.pendingPt = null
          this.pendingHost = null
          this.pendingAnchor = null
          this.render()
        } else if (this.selected.length > 0) {
          this.selected = []
          this.render()
          this.emit()
        } else if (this.tool !== 'pointer') {
          // Last rung: nothing half-placed and nothing selected, so "cancel"
          // can only mean the TOOL itself — which is what Escape means in
          // every other drawing program. Without this the tool stayed armed
          // and the next click started another shape. setTool() already nulls
          // the pendings, resets the cursor, renders and emits, so the pages
          // learn about it through onChange.
          this.setTool('pointer')
        }
      } else if ((e.key === 'Delete' || e.key === 'Backspace') && this.selected.length > 0) {
        this.deleteSelected()
      } else if ((e.key === 'l' || e.key === 'L') && this.selected.length > 0) {
        // ONE key, both directions. Chart-scoped, like every other key here:
        // app-wide would mean routing through main's before-input-event (the
        // pattern main/tabs.ts uses for F12), a shell change and out of scope.
        this.toggleLockSelected()
      }
    }
    window.addEventListener('keydown', onKey)
    this.teardown.push(() => window.removeEventListener('keydown', onKey))

    // Fires only on actual size changes — event-driven, not a poll.
    this.ro = new ResizeObserver(() => this.render())
    this.ro.observe(this.host)
    this.teardown.push(() => this.ro.disconnect())

    this.applyCursor()
    this.render()
    // Last, and deliberately after the first render: hydrate() renders again
    // when the objects arrive, and a chart that paints empty and then fills is
    // honest about a store it has not heard from yet.
    this.hydrate()
  }

  // ---- public surface -----------------------------------------------------

  /** Chart.tsx rebuilds its series on every data change; re-point at the live one. */
  setSeries(series: ISeriesApi<SeriesType>): void {
    this.series = series
    this.render()
  }

  /** Refresh the bars source. Pages that pass a snapshot array at
   *  construction call this on the cheap re-anchor path so measure/inspect
   *  don't keep reading pre-refetch data. */
  setBars(bars: Bar[] | (() => Bar[])): void {
    this.setBarsSource(bars)
    this.render()
  }

  private setBarsSource(bars: Bar[] | (() => Bar[]) | undefined): void {
    if (bars === undefined) this.barsOpt = undefined
    else if (typeof bars === 'function') this.barsOpt = bars
    else this.barsOpt = () => bars
  }

  /** Switch store bucket (symbol/timeframe/scale change) — drops any
   *  half-placed object and the selection (ids belong to the old bucket). */
  setKey(key: string): void {
    if (key === this.key) return
    // Before the reassignment, while this.key still names the bucket those
    // pending edits belong to. A debounce timer cancelled by the switch and
    // never flushed is exactly how the last line drawn before a symbol change
    // disappears.
    this.flushSave()
    this.key = key
    this.pendingPt = null
    this.pendingHost = null
    this.pendingAnchor = null
    this.selected = []
    this.cursor = null
    this.render()
    this.emit()
    this.hydrate()
  }

  setTool(tool: DrawTool): void {
    if (tool === this.tool) return
    this.tool = tool
    this.pendingPt = null
    this.pendingHost = null
    this.pendingAnchor = null
    this.cursor = null
    this.applyCursor()
    this.render()
    this.emit()
  }

  /** DrawEditor commit: exact values in, times snapped to the nearest bar so
   *  the point stays projectable (timeToCoordinate only resolves times that
   *  exist on the scale — a typed 'Saturday' would silently hide the line).
   *  Without a bars getter the typed time is kept as-is. */
  updateDrawing(id: string, points: Pt[]): void {
    const d = this.bucket().drawings.find((x) => x.id === id)
    if (!d) return
    const want = d.kind === 'trend' || d.kind === 'circle' ? 2 : 1
    if (points.length !== want) return
    d.points = points.map((p) => ({
      time: (this.nearestBarTime(p.time) ?? p.time) as UTCTimestamp,
      price: p.price,
    }))
    propagate(this.bucket().drawings, this.bucket().constraints, new Set([id]))
    this.issue = this.restoreSlopes(new Set([id]))
    this.commit()
  }

  /** Removes EVERY selected object, whatever kind. One doomed set sweeps all
   *  three arrays because mkId's ids are globally unique — the selection needs
   *  no per-kind bookkeeping. Previously this filtered b.drawings alone, so a
   *  measure could never be deleted by the Del key or the Delete tool. */
  deleteSelected(): void {
    if (this.selected.length === 0) return
    const b = this.bucket()
    const doomed = new Set(this.selected)
    b.drawings = b.drawings.filter((d) => !doomed.has(d.id))
    b.measures = b.measures.filter((m) => !doomed.has(m.id))
    b.pins = b.pins.filter((p) => !doomed.has(p.id))
    this.selected = []
    this.commit()
  }

  /** Deselect everything without deleting (DrawEditor's × / page escape hatch). */
  clearSelection(): void {
    if (this.selected.length === 0) return
    this.selected = []
    this.render()
    this.emit()
  }

  /** Drawings only. Measures anchored to a deleted line degrade to their
   *  snap-moment free position rather than vanishing. */
  clearDrawings(): void {
    const b = this.bucket()
    b.drawings = []
    this.selected = []
    this.pendingPt = null
    this.commit()
  }

  /** Kept for pre-v2 callers. */
  clear(): void {
    this.clearDrawings()
  }

  /** Measures + inspect pins only. */
  clearMeasures(): void {
    const b = this.bucket()
    // Selected ids must go with the objects. Harmless while only drawings
    // could be selected; now that measures and pins can be, leaving their ids
    // behind would keep `selected` pointing at things that no longer exist and
    // the Delete key would act on nothing.
    const gone = new Set([...b.measures.map((m) => m.id), ...b.pins.map((p) => p.id)])
    b.measures = []
    b.pins = []
    this.selected = this.selected.filter((id) => !gone.has(id))
    this.pendingAnchor = null
    this.commit()
  }

  /** vis:draw — hide/show everything without deleting. Placement previews
   *  still render while hidden: a tool that draws an invisible line is a
   *  dead cursor, and pages un-hide on the next state change anyway. */
  setDrawingsHidden(hidden: boolean): void {
    if (hidden === this.hidden) return
    this.hidden = hidden
    this.render()
    this.emit()
  }

  getState(): DrawState {
    const b = this.bucket()
    const byId = new Map(b.drawings.map((d) => [d.id, d]))
    const mById = new Map(b.measures.map((m) => [m.id, m]))
    const pById = new Map(b.pins.map((p) => [p.id, p]))
    // Each kind is resolved into its own channel, in selection order. A
    // selected measure used to be dropped silently here (the filter kept only
    // Drawings), which is why clicking one could never light up the editor.
    return {
      tool: this.tool,
      drawings: b.drawings.length,
      measures: b.measures.length + b.pins.length,
      selection: this.selected
        .map((id) => byId.get(id))
        .filter((d): d is Drawing => d !== undefined),
      selectedMeasures: this.selected
        .map((id) => mById.get(id))
        .filter((m): m is Measure => m !== undefined),
      selectedPins: this.selected
        .map((id) => pById.get(id))
        .filter((p): p is InspectPin => p !== undefined),
      selected: [...this.selected],
      hidden: this.hidden,
      dof: degreesOfFreedom(b.drawings, b.constraints),
      lockedSlots: (() => {
        const a = analyze(b.drawings, b.constraints)
        return [...a.rep].filter(([, r]) => a.pinned.has(r)).map(([s]) => s)
      })(),
      // LOCKS only. An 'on' is not a lock, and painting a padlock on a line
      // that is merely held to something would say the opposite of the truth.
      lockedIds: [
        ...new Set(b.constraints.filter((c) => c.kind === 'lock').map((c) => c.a.id)),
      ],
      issue: this.issue,
    }
  }

  // ---- constraints --------------------------------------------------------

  /** ONE key, both directions. Kade: "I shouldn't press l and u I should toggle
   *  with one button." A separate unlock key also made the state invisible —
   *  you had to try the wrong one to find out which it was. */
  toggleLockSelected(): void {
    const b = this.bucket()
    const byId = new Map(b.drawings.map((d) => [d.id, d]))
    const targets = this.selected.filter((id) => byId.has(id))
    if (targets.length === 0) return
    const lockedNow = new Set(
      b.constraints.filter((c) => c.kind === 'lock').map((c) => c.a.id)
    )
    // All locked -> unlock. Otherwise lock whatever is not. Mixed selections
    // resolve toward locked, so the key always has a visible effect.
    if (targets.every((id) => lockedNow.has(id))) {
      b.constraints = b.constraints.filter(
        (c) => !(c.kind === 'lock' && targets.includes(c.a.id))
      )
      this.issue = null
      this.commit()
      return
    }
    let added = 0
    let issue: ConstraintIssue | null = null
    for (const id of targets) {
      if (lockedNow.has(id)) continue
      const d = byId.get(id)
      if (d && d.kind === 'circle') {
        issue = { code: 'unsupported', message: 'A circle cannot be locked.' }
        continue
      }
      const res = this.addConstraint({ kind: 'lock', a: { id, part: 'line' } })
      if (res.ok) added++
      else issue = res.issue
    }
    this.issue = issue
    if (added > 0) this.commit()
    else this.emit() // a refusal still has to reach the page
  }

  /** Hold a trend endpoint against an hline/vline.
   *
   *  NOT a lock, and the difference is the feature: the endpoint takes the
   *  line's price (or a vline's time) and KEEPS its other coordinate, so the
   *  line can still be dragged, the endpoint rides it, and the trend stretches
   *  to stay in contact. */
  holdOn(point: EntityRef, host: EntityRef): { ok: true; id: string } | { ok: false; issue: ConstraintIssue } {
    const res = this.addConstraint({ kind: 'on', a: point, b: host })
    this.issue = res.ok ? null : res.issue
    if (res.ok) this.commit()
    else this.emit()
    return res
  }

  /** Set one coordinate to an exact value AND hold it there.
   *
   *  Kade: "Setting any dimension of an item will lock that dimension until the
   *  user changes it; if that dimension can't change because of a locking
   *  constraint we must notify them." So this is one action, not two — typing a
   *  price both moves it and pins it — and it refuses when the coordinate is
   *  already held by SOMETHING ELSE, naming what. Re-typing a value you locked
   *  yourself just moves it, which is how a driving dimension behaves in CAD. */
  setSlotValue(ref: EntityRef, value: number): { ok: true } | { ok: false; issue: ConstraintIssue } {
    const b = this.bucket()
    const byId = new Map(b.drawings.map((d) => [d.id, d]))
    const slots = slotsForRef(ref, byId)
    if (slots.length !== 1) {
      return {
        ok: false,
        issue: { code: 'unknown', message: 'That is not a single coordinate to set.' },
      }
    }
    const slot = slots[0]
    const a = analyze(b.drawings, b.constraints)
    const r = a.rep.get(slot)
    const mine = b.constraints.find(
      (c) => c.kind === 'lock' && slotsForRef(c.a, byId).includes(slot)
    )
    if (r !== undefined && a.pinned.has(r) && !mine) {
      // Held through an equality by a lock on something else — the case that
      // has to be reported rather than silently ignored.
      const holder = b.constraints.find((c) => {
        if (c.kind !== 'lock') return false
        return slotsForRef(c.a, byId).some((s) => a.rep.get(s) === r)
      })
      const hd = holder ? byId.get(holder.a.id) : null
      return {
        ok: false,
        issue: {
          code: 'blocked',
          message: `That value is held by the lock on ${hd ? `the ${hd.kind}` : 'another object'}. Unlock it first.`,
        },
      }
    }
    // Write through the whole equality class: the point and the line it is held
    // against are ONE number, so setting either sets both.
    for (const [s, rr] of a.rep) {
      if (rr !== r) continue
      const d = byId.get(s.slice(0, s.indexOf(':')))
      if (d) writeSlot(d, s, value)
    }
    if (!mine) this.addConstraint({ kind: 'lock', a: ref })
    this.issue = this.restoreSlopes(new Set([ref.id]))
    this.commit()
    return { ok: true }
  }

  /** Remove the lock on one coordinate (the editor's per-field padlock, off). */
  clearSlotLock(ref: EntityRef): void {
    const b = this.bucket()
    const byId = new Map(b.drawings.map((d) => [d.id, d]))
    const want = slotsForRef(ref, byId).join()
    const before = b.constraints.length
    b.constraints = b.constraints.filter(
      (c) => !(c.kind === 'lock' && slotsForRef(c.a, byId).join() === want)
    )
    this.issue = null
    if (b.constraints.length !== before) this.commit()
    else this.emit()
  }

  /** Add one constraint, or say why not.
   *
   *  NO MATRIX. Every constraint here is an equality, so "does this add
   *  information?" is answered exactly by union-find: a lock adds nothing if its
   *  class is already pinned, and an 'on' adds nothing if the two slots are
   *  already the same number. Both are set lookups, not tolerances. */
  addConstraint(
    c: Omit<Constraint, 'id'>
  ): { ok: true; id: string } | { ok: false; issue: ConstraintIssue } {
    const b = this.bucket()
    const byId = new Map(b.drawings.map((d) => [d.id, d]))
    const probe: Constraint = { ...c, id: 'probe' }
    const a = analyze(b.drawings, b.constraints)

    if (c.kind === 'on') {
      const pair = onPair(probe, byId)
      if (!pair) {
        return {
          ok: false,
          issue: {
            code: 'unsupported',
            message: 'Hold a trend endpoint against a horizontal or vertical line.',
          },
        }
      }
      if (a.rep.get(pair[0]) === a.rep.get(pair[1])) {
        return {
          ok: false,
          issue: { code: 'duplicate', message: 'Those are already held together.' },
        }
      }
    } else {
      const want = slotsForRef(c.a, byId)
      if (want.length === 0) {
        return {
          ok: false,
          issue: {
            code: 'unknown',
            message: 'That constraint names something that cannot carry it.',
          },
        }
      }
      if (want.every((s) => a.pinned.has(a.rep.get(s) as string))) {
        const d = byId.get(c.a.id)
        return {
          ok: false,
          issue: {
            code: 'duplicate',
            message: `That ${d ? d.kind : 'object'} is already locked.`,
          },
        }
      }
    }
    const id = mkId('cn')
    b.constraints.push({ ...c, id })
    return { ok: true, id }
  }

  /** The live slope of a trend, in price per hour of chart time. */
  slopeOf(id: string): number | null {
    const d = this.bucket().drawings.find((x) => x.id === id)
    if (!d || d.kind !== 'trend') return null
    const mins = this.chartMinutes(d.points[0].time, d.points[1].time)
    return mins === null ? null : slopePerHour(d.points[1].price - d.points[0].price, mins)
  }

  /** The slope this trend is driven to hold, or null if it floats. */
  slopeLockOf(id: string): number | null {
    const c = this.bucket().constraints.find((x) => x.kind === 'slope' && x.a.id === id)
    return c?.value ?? null
  }

  /** Drive a trend's slope, or release it. This is the constraint Kade asked
   *  for: "lock the trend angle / price action per time so when changing a
   *  horizontal or vertical line's price the trend line stays the same but
   *  follows the anchor line without changing the slope." */
  setSlopeLock(id: string, value: number | null): { ok: true } | { ok: false; issue: ConstraintIssue } {
    const b = this.bucket()
    const d = b.drawings.find((x) => x.id === id)
    if (!d || d.kind !== 'trend') {
      return { ok: false, issue: { code: 'unsupported', message: 'Only a trend line has a slope.' } }
    }
    // Replace rather than stack: one driving slope per trend, so the DOF count
    // cannot double-count and there is never a second value to disagree with.
    b.constraints = b.constraints.filter((c) => !(c.kind === 'slope' && c.a.id === id))
    if (value !== null) {
      if (!Number.isFinite(value)) {
        return { ok: false, issue: { code: 'unsupported', message: 'A slope must be a number.' } }
      }
      b.constraints.push({ id: mkId('cn'), kind: 'slope', a: { id, part: 'line' }, value })
    }
    // Hold the trend itself: setting a slope must move the OTHER end, not
    // silently redefine the line you just dimensioned.
    this.issue = this.restoreSlopes(new Set())
    this.commit()
    return { ok: true }
  }

  /** Restore every driven slope after something moved.
   *
   *  ONE PASS, and closed-form, because the relation is affine:
   *      (p_b − p_a) = k · (i_b − i_a),   k = value · barMinutes / 60
   *  Solving it is picking WHICH coordinate absorbs the change, which is an
   *  ordering decision rather than a minimisation — a least-norm solve would
   *  move lines the user never touched, and by an amount that depends on how
   *  price and bars are weighted against each other.
   *
   *  The order is Kade's: the end drawn SECOND trails, the first is the anchor;
   *  and time gives way before price, so a dragged h-line makes the diagonal
   *  run out rather than dragging the other h-line with it. Anything pinned, and
   *  anything the user is currently holding, is skipped — restoring a constraint
   *  by undoing the drag that triggered it is not a fix. */
  private restoreSlopes(held: Set<string>): ConstraintIssue | null {
    const b = this.bucket()
    const idx = this.barsIdx()
    const per = this.barMinutes()
    if (!idx || per === null) return null
    const byId = new Map(b.drawings.map((d) => [d.id, d]))
    let issue: ConstraintIssue | null = null

    for (const c of b.constraints) {
      if (c.kind !== 'slope' || c.value === undefined) continue
      const d = byId.get(c.a.id)
      if (!d || d.kind !== 'trend') continue
      const ia = idx.map.get(d.points[0].time)
      const ib = idx.map.get(d.points[1].time)
      if (ia === undefined || ib === undefined) continue
      const k = (c.value * per) / 60 // price per BAR — chart time is linear in index
      const pa = d.points[0].price
      const pb = d.points[1].price
      if (Math.abs(pb - pa - k * (ib - ia)) < 1e-9) continue // already true

      // Recomputed per constraint: an earlier restore may have moved something.
      const a = analyze(b.drawings, b.constraints)
      const usable = (slot: string): boolean => {
        const r = a.rep.get(slot)
        if (r === undefined || a.pinned.has(r)) return false
        // Nothing in the class may belong to what the user is holding.
        for (const [s, rr] of a.rep) if (rr === r && held.has(slotOwner(s))) return false
        return true
      }
      const write = (slot: string, v: number): void => {
        const r = a.rep.get(slot)
        for (const [s, rr] of a.rep) {
          if (rr !== r) continue
          const od = byId.get(slotOwner(s))
          if (od) writeSlot(od, s, v)
        }
      }

      // k === 0 is a flat line: the index terms vanish, so only a price can
      // satisfy it. No division by zero exists anywhere in this block.
      const byIndex = k !== 0
      const cands: { slot: string; value: number; index: boolean }[] = []
      if (byIndex) {
        cands.push({ slot: `${d.id}:b:i`, value: ia + (pb - pa) / k, index: true })
        cands.push({ slot: `${d.id}:a:i`, value: ib - (pb - pa) / k, index: true })
      }
      cands.push({ slot: `${d.id}:b:p`, value: pa + k * (ib - ia), index: false })
      cands.push({ slot: `${d.id}:a:p`, value: pb - k * (ib - ia), index: false })

      const pick = cands.find((x) => usable(x.slot))
      if (!pick) {
        issue = {
          code: 'blocked',
          message: `The slope is locked at ${c.value.toFixed(4)} and nothing is free to absorb the change. Unlock something to move this.`,
        }
        continue
      }
      if (!pick.index) {
        write(pick.slot, pick.value)
        continue
      }
      // An index has to land on a real bar. barIndexOf REPORTS running off the
      // loaded history rather than clamping to the last bar and pretending.
      const landed = barIndexOf(pick.value, idx.times.length)
      if (!('i' in landed)) {
        issue = {
          code: 'blocked',
          message: `Holding that slope needs a bar ${landed.byBars} past the ${landed.side === 'after' ? 'end' : 'start'} of the loaded history.`,
        }
        continue
      }
      write(pick.slot, idx.times[landed.i])
      // Bars are a lattice, so the slope can only be hit exactly when the
      // solution happens to be a whole bar. Say so rather than quietly
      // realising a different number than the one on screen.
      const got = this.slopeOf(d.id)
      if (got !== null && Math.abs(got - c.value) > Math.abs(c.value) * 1e-6 + 1e-9) {
        issue = {
          code: 'quantized',
          message: `Slope ${got.toFixed(4)} — ${c.value.toFixed(4)} falls between bars.`,
        }
      }
    }
    return issue
  }

  /** Which of the wanted drawings a drag may actually move, and why not if not.
   *
   *  A named method rather than four lines inside the mousedown closure, because
   *  the rule is the feature: it is the difference between "locked" meaning
   *  something and a drag that visibly starts and then snaps back. Nothing here
   *  touches the DOM, so the gate can exercise it directly. */
  private movableIds(wanted: string[]): { ids: string[]; issue: ConstraintIssue | null } {
    const b = this.bucket()
    const a = analyze(b.drawings, b.constraints)
    // PINNED, not merely constrained. An 'on' does not block anything — it is
    // the relation that makes the h-line draggable WITH the trend attached, so
    // treating any constraint as a brake would forbid exactly the motion the
    // feature exists to allow. A translation moves every coordinate a drawing
    // owns, so one pinned slot is enough to stop it.
    const frozen = (id: string): boolean => {
      const d = b.drawings.find((x) => x.id === id)
      if (!d) return false
      return slotsOf(d).some((s) => a.pinned.has(a.rep.get(s) as string))
    }
    const ids = wanted.filter((id) => !frozen(id))
    if (ids.length > 0) return { ids, issue: null }
    const d = b.drawings.find((x) => x.id === wanted[0])
    return {
      ids,
      issue: {
        code: 'blocked',
        message: `That ${d ? d.kind : 'drawing'} is locked. Unlock it to move it.`,
      },
    }
  }

  /** Drop constraints whose target no longer exists.
   *
   *  Called from commit(), so EVERY path that removes a drawing is covered —
   *  delete, clear, and trim, which replaces a drawing with new ones and would
   *  otherwise leave a lock pointing at an id that is gone. A dangling
   *  constraint is invisible, still counts against DOF, and the backend
   *  validator has no way to know the id is dead. */
  private pruneConstraints(b: Bucket): boolean {
    if (b.constraints.length === 0) return false
    const live = new Set(b.drawings.map((d) => d.id))
    // BOTH references. An 'on' names two drawings, so deleting the host leaves
    // it dangling just as surely as deleting the point does — and a relation
    // pointing at a dead id is invisible, unremovable, and still merges
    // coordinate classes that no longer have any business being merged.
    const kept = b.constraints.filter(
      (c) => live.has(c.a.id) && (c.b === undefined || live.has(c.b.id))
    )
    if (kept.length === b.constraints.length) return false
    b.constraints = kept
    return true
  }

  /** Single subscriber (the owning page). Fires immediately so the page's
   *  data-draw-* attrs and DrawEditor start synced, then on every change. */
  onChange(cb: (s: DrawState) => void): void {
    this.changeCb = cb
    cb(this.getState())
  }

  destroy(): void {
    // Write before tearing down. An unmount is the most common way a drawing
    // reaches the end of its life within the debounce window — draw, switch
    // tab, and the timer dies with the engine unless it is flushed here.
    this.flushSave()
    this.destroyed = true
    // React unmounts children before parents, so the chart is often already
    // disposed when the owning page's cleanup runs — every unhook tolerates it.
    for (const fn of this.teardown) {
      try {
        fn()
      } catch {
        /* chart already removed */
      }
    }
    this.teardown = []
    this.changeCb = null
    this.host.style.cursor = ''
    this.svg.remove()
    this.labels.remove()
  }

  // ---- store / projection helpers ----------------------------------------

  private bucket(): Bucket {
    return bucketFor(this.key)
  }

  // ---- persistence --------------------------------------------------------

  /** Pull this key's saved objects in — once per key per session, however many
   *  engines ask. A revisit inside the session reads the live bucket instead,
   *  which is both faster and correct: the bucket is newer than the server
   *  copy whenever a save is still debouncing. */
  private hydrate(): void {
    const store = this.store
    if (!store) return
    const key = this.key
    let first = loadedKeys.get(key)
    if (!first) {
      first = store
        .load(key)
        .then((doc) => {
          const b = bucketFor(key)
          // The user can draw in the milliseconds a load is in flight. Their
          // work is newer than the server's, so it wins and the pending save
          // carries it — seeding here would delete a line they just drew.
          if (!isEmptyBucket(b)) return
          b.drawings = doc?.drawings ?? []
          b.measures = doc?.measures ?? []
          b.pins = doc?.pins ?? []
          b.constraints = doc?.constraints ?? []
          adoptIds(b)
          // Now in sync with the store, so the emit below is not a write.
          savedDocs.set(key, JSON.stringify(docOf(b)))
        })
        .catch(() => {
          /* The adapter owns reporting. A chart whose objects failed to load
             still draws — it just starts blank, and the first edit tries to
             save again. */
        })
      loadedKeys.set(key, first)
    }
    void first.then(() => {
      // A tab switch or an unmount can beat the response back. Painting then
      // would draw the previous symbol's lines onto this one, or touch nodes
      // that destroy() already removed.
      if (this.destroyed || this.key !== key) return
      // Through commit(): restored objects ARE a change to what is drawn, and
      // the solver will need to run once here to colour them.
      this.commit()
    })
  }

  private scheduleSave(): void {
    if (!this.store) return
    if (this.saveTimer !== null) clearTimeout(this.saveTimer)
    this.saveTimer = setTimeout(() => {
      this.saveTimer = null
      this.flushSave()
    }, SAVE_DEBOUNCE_MS)
  }

  /** Write the pending edit NOW. Called before the key changes and on destroy:
   *  both cancel the debounce timer, and a cancelled timer with no flush is
   *  exactly how the last drawing before a tab switch goes missing. */
  private flushSave(): void {
    if (this.saveTimer !== null) {
      clearTimeout(this.saveTimer)
      this.saveTimer = null
    }
    // this.key, not a captured one: setKey() flushes BEFORE it reassigns, so
    // in both call sites this is still the key the pending edits belong to.
    if (this.store) saveNow(this.store, this.key)
  }

  /** Mutate the bucket, then paint and persist ONCE.
   *
   *  Every method that CHANGES WHAT IS DRAWN ends here instead of calling
   *  render() and emit() itself. Selection, tool and visibility changes
   *  deliberately do NOT: they still render+emit, because they change how you
   *  are looking at the chart rather than what is on it.
   *
   *  The distinction is worth the seam now because the constraint solver lands
   *  here. A solve that moves twelve drawings must fire one render and one save,
   *  not twelve of each — and it must not run at all when you merely click
   *  something. */
  private commit(): void {
    this.pruneConstraints(this.bucket())
    this.render()
    this.emit()
  }

  private emit(): void {
    // Every mutator routes through here, so this is the one place persistence
    // has to hook. Selection and tool changes emit too; saveNow's comparison
    // makes those free rather than a write per click.
    this.scheduleSave()
    this.changeCb?.(this.getState())
  }

  private percentMode(): boolean {
    if (this.percentOpt) return this.percentOpt()
    return this.key.endsWith('|%') // charts-page bucket convention
  }

  private applyCursor(): void {
    // Crosshair while any tool is armed: the cursor itself says "the chart
    // is a placement surface right now", pointer mode hands it back.
    if (this.tool !== 'pointer') {
      this.host.style.cursor = 'crosshair'
      return
    }
    // Pointer picks, so the cursor must say when something is pickable. This
    // has to live on the HOST: both overlay layers are pointer-events:none
    // (clicks arrive through subscribeClick, not the DOM), so a chip is never
    // the pointer target and a `cursor` rule on it is never consulted.
    this.host.style.cursor = this.hoverId !== null ? 'pointer' : ''
  }

  private xForTime(t: UTCTimestamp): number | null {
    try {
      return this.chart.timeScale().timeToCoordinate(t)
    } catch {
      return null
    }
  }

  private timeAtX(x: number): UTCTimestamp | null {
    try {
      const t = this.chart.timeScale().coordinateToTime(x)
      return typeof t === 'number' ? t : null
    } catch {
      return null
    }
  }

  private yForPrice(p: number): number | null {
    try {
      return this.series.priceToCoordinate(p)
    } catch {
      return null
    }
  }

  private priceAtY(y: number): number | null {
    try {
      return this.series.coordinateToPrice(y)
    } catch {
      return null
    }
  }

  private project(pt: Pt): XY | null {
    const x = this.xForTime(pt.time)
    const y = this.yForPrice(pt.price)
    return x === null || y === null ? null : { x, y }
  }

  private barsIdx(): {
    bars: Bar[]
    times: number[]
    map: Map<number, number>
    /** Cumulative chart minutes, or [] when the timeframe is unknown. */
    mins: number[]
  } | null {
    const arr = this.barsOpt?.()
    if (!arr || arr.length === 0) return null
    if (arr !== this.barsCacheSrc) {
      this.barsCacheSrc = arr
      this.barsCacheTimes = arr.map((b) => Math.floor(new Date(b.ts).getTime() / 1000))
      this.barsCacheMap = new Map(this.barsCacheTimes.map((t, i) => [t, i]))
      this.barsCachePer = null // the prefix sum belongs to these times
    }
    // Keyed on the CANDLE LENGTH as well as array identity: the prefix sum is a
    // function of both, and setKey can change the timeframe while a page hands
    // back the same array. Rebuilding on identity alone would leave 1Day
    // minutes describing a 5Min chart.
    const per = this.barMinutes()
    if (per !== null && this.barsCachePer !== per) {
      this.barsCacheMins = chartMinutePrefix(this.barsCacheTimes, per)
      this.barsCachePer = per
    }
    return {
      bars: arr,
      times: this.barsCacheTimes,
      map: this.barsCacheMap,
      mins: this.barsCachePer === null ? [] : this.barsCacheMins,
    }
  }

  private barAt(time: number): Bar | null {
    const idx = this.barsIdx()
    if (!idx) return null
    const i = idx.map.get(time)
    return i === undefined ? null : idx.bars[i]
  }

  private barCountBetween(a: number, b: number): number | null {
    const idx = this.barsIdx()
    if (!idx) return null
    const ia = idx.map.get(a)
    const ib = idx.map.get(b)
    if (ia === undefined || ib === undefined) return null
    return Math.abs(ib - ia)
  }

  /** Nominal minutes per candle for THIS bucket, read off the key's timeframe.
   *  The header documents that the key must carry it, and percentMode() already
   *  parses the key this way — no new plumbing, and no page has to pass it in.
   *  Null (unknown timeframe) degrades honestly: no slope row, same habit as
   *  bars-absent degrading the bar count. */
  private barMinutes(): number | null {
    for (const part of this.key.split('|')) {
      const m = TF_MINUTES[part]
      if (m !== undefined) return m
    }
    return null
  }

  /** Chart time between two bar times, in minutes, SIGNED.
   *
   *  Signed because a slope has a direction: barCountBetween is Math.abs, so
   *  building a slope on it would report the opposite $/h for the same line
   *  depending on which end was clicked first.
   *
   *  DERIVED FROM THE CANDLES, NOT THE CALENDAR. Each step between consecutive
   *  candles contributes min(its real gap, one candle). So an overnight or
   *  weekend break costs ONE candle rather than seventeen hours — time with no
   *  candles in it does not exist — and a missing candle costs one step rather
   *  than the hole it left. That is what makes the number portable: measure
   *  09:30→11:30 on 5Min (24 steps × 5) or on 1Hour (2 steps × 60) and both
   *  give 120 minutes. */
  private chartMinutes(a: number, b: number): number | null {
    const idx = this.barsIdx()
    const per = this.barMinutes()
    if (!idx || per === null) return null
    const ia = idx.map.get(a)
    const ib = idx.map.get(b)
    if (ia === undefined || ib === undefined) return null
    // One subtraction, not a walk. The difference is signed for free, which is
    // what the old `ib >= ia ? mins : -mins` was doing by hand.
    const c = idx.mins
    if (c.length !== idx.times.length) return null
    return c[ib] - c[ia]
  }

  private nearestBarTime(t: number): number | null {
    const idx = this.barsIdx()
    if (!idx) return null
    const ts = idx.times
    if (t <= ts[0]) return ts[0]
    if (t >= ts[ts.length - 1]) return ts[ts.length - 1]
    let lo = 0
    let hi = ts.length - 1
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1
      if (ts[mid] <= t) lo = mid
      else hi = mid
    }
    return t - ts[lo] <= ts[hi] - t ? ts[lo] : ts[hi]
  }

  // ---- geometry -----------------------------------------------------------

  private paneSizeSafe(): { width: number; height: number } | null {
    try {
      return this.chart.paneSize(0)
    } catch {
      return null
    }
  }

  /** The pixel segment used for HIT-TESTING (and as a cut donor): hlines and
   *  vlines act as pane-spanning (visually infinite) lines. */
  private hitSegPx(d: Drawing, pane: { width: number; height: number }): Seg | null {
    if (d.kind === 'trend') {
      const a = this.project(d.points[0])
      const b = this.project(d.points[1])
      return a && b ? { a, b } : null
    }
    if (d.kind === 'hline') {
      const y = this.yForPrice(d.points[0].price)
      return y === null ? null : { a: { x: 0, y }, b: { x: pane.width, y } }
    }
    if (d.kind === 'vline') {
      const x = this.xForTime(d.points[0].time)
      return x === null ? null : { a: { x, y: 0 }, b: { x, y: pane.height } }
    }
    return null // circle: sampled separately
  }

  /** The pixel segment a TRIM parameterizes: hlines bound at the data extent
   *  (first/last bar), vlines at the visible price extent, trends at their
   *  endpoints. See the header block for why. */
  private trimSegPx(d: Drawing, pane: { width: number; height: number }): Seg | null {
    if (d.kind === 'trend') return this.hitSegPx(d, pane)
    if (d.kind === 'hline') {
      const y = this.yForPrice(d.points[0].price)
      if (y === null) return null
      const idx = this.barsIdx()
      let x0: number | null = null
      let x1: number | null = null
      if (idx) {
        x0 = this.xForTime(idx.times[0] as UTCTimestamp)
        x1 = this.xForTime(idx.times[idx.times.length - 1] as UTCTimestamp)
      }
      // No bars getter (or extremes unprojectable): fall back to the visible
      // edge bars — narrower than the data, but never wrong on screen.
      if (x0 === null) {
        const t = this.timeAtX(0)
        x0 = t === null ? null : this.xForTime(t)
      }
      if (x1 === null) {
        const t = this.timeAtX(pane.width - 1)
        x1 = t === null ? null : this.xForTime(t)
      }
      if (x0 === null || x1 === null || x0 === x1) return null
      return { a: { x: x0, y }, b: { x: x1, y } }
    }
    if (d.kind === 'vline') {
      const x = this.xForTime(d.points[0].time)
      return x === null ? null : { a: { x, y: 0 }, b: { x, y: pane.height } }
    }
    return null
  }

  private ellipsePx(d: Drawing): { cx: number; cy: number; rx: number; ry: number } | null {
    const c = this.project(d.points[0])
    const e = this.project(d.points[1])
    if (!c || !e) return null
    return { cx: c.x, cy: c.y, rx: Math.max(Math.abs(e.x - c.x), 1), ry: Math.max(Math.abs(e.y - c.y), 1) }
  }

  /** Nearest object within HIT_PX of (x, y). linesOnly skips circles (trim
   *  and line-snap vocabulary). Returns the nearest point + param for snaps. */
  private hitTest(
    x: number,
    y: number,
    linesOnly = false
  ): { drawing: Drawing; dist: number; nx: number; ny: number; u: number } | null {
    // Same rule as hitAny: an invisible drawing is not a target. This also
    // stops trim cutting, and measure-snap snapping to, geometry the user
    // cannot see.
    if (this.hidden) return null
    const pane = this.paneSizeSafe()
    if (!pane) return null
    let best: { drawing: Drawing; dist: number; nx: number; ny: number; u: number } | null = null
    for (const d of this.bucket().drawings) {
      if (d.kind === 'circle') {
        if (linesOnly) continue
        const g = this.ellipsePx(d)
        if (!g) continue
        // Ellipse boundary as a sampled polyline: exact enough at 48 samples,
        // and reuses the one distance primitive instead of ellipse calculus.
        let prev: XY | null = null
        for (let i = 0; i <= ELLIPSE_HIT_SAMPLES; i += 1) {
          const ang = (i / ELLIPSE_HIT_SAMPLES) * Math.PI * 2
          const pt = { x: g.cx + g.rx * Math.cos(ang), y: g.cy + g.ry * Math.sin(ang) }
          if (prev) {
            const h = distToSeg(x, y, prev, pt)
            if (!best || h.dist < best.dist) best = { drawing: d, dist: h.dist, nx: h.nx, ny: h.ny, u: 0 }
          }
          prev = pt
        }
      } else {
        const s = this.hitSegPx(d, pane)
        if (!s) continue
        const h = distToSeg(x, y, s.a, s.b)
        if (!best || h.dist < best.dist) best = { drawing: d, dist: h.dist, nx: h.nx, ny: h.ny, u: h.t }
      }
    }
    return best !== null && best.dist <= HIT_PX ? best : null
  }

  /**
   * The picking entry point for select and delete.
   *
   * hitTest() above is left byte-identical and still walks drawings only —
   * trim and measure-snap call it directly and their vocabulary IS lines, so
   * widening it would let "trim" resolve to a measurement. This wrapper is
   * what the user-facing "click a thing" verbs go through instead.
   *
   * Chips are tried first: a chip is an opaque box drawn over everything, and
   * a label the eye reads as the object IS the object, so a click inside one
   * can only have meant that chip. Connectors then compete with drawings on
   * plain pixel distance, so the closer thing under the cursor wins rather
   * than whichever array happened to be scanned first.
   */
  private hitAny(x: number, y: number): Hit | null {
    // Hidden means hidden. render() skips every object when this.hidden, so
    // picking must too, or a plain left-click in the resting tool selects
    // something invisible: the editor opens for an object that is not on
    // screen, and Delete then removes it with nothing to see. Harmless while
    // selecting needed an armed tool; reachable from any click now.
    if (this.hidden) return null
    const b = this.bucket()
    // Nothing to hit: bail before paneSizeSafe(), the anchor projections and
    // the ellipse sampler. Pointer is the resting tool and calls this on every
    // crosshair move, so the empty chart - the state the app spends most of
    // its life in - must cost three length checks and no allocation.
    if (b.drawings.length === 0 && b.measures.length === 0 && b.pins.length === 0) return null
    for (const z of this.hotZones) {
      if (x < z.left || x > z.left + z.w || y < z.top || y > z.top + z.h) continue
      if (z.kind === 'measure') {
        const m = b.measures.find((v) => v.id === z.id)
        if (m) return { kind: 'measure', id: m.id, measure: m, dist: 0 }
      } else {
        const p = b.pins.find((v) => v.id === z.id)
        if (p) return { kind: 'pin', id: p.id, pin: p, dist: 0 }
      }
    }
    let best: Hit | null = null
    for (const m of b.measures) {
      const A = this.resolveAnchor(m.a)
      const B = this.resolveAnchor(m.b)
      if (!A || !B) continue
      const h = distToSeg(x, y, A, B)
      if (h.dist <= HIT_PX && (best === null || h.dist < best.dist)) {
        best = { kind: 'measure', id: m.id, measure: m, dist: h.dist }
      }
    }
    const d = this.hitTest(x, y)
    if (d && (best === null || d.dist < best.dist)) {
      return {
        kind: 'drawing', id: d.drawing.id, drawing: d.drawing,
        dist: d.dist, nx: d.nx, ny: d.ny, u: d.u,
      }
    }
    return best
  }

  // ---- trim ---------------------------------------------------------------

  private computeTrim(
    d: Drawing,
    x: number,
    y: number
  ):
    | { whole: true }
    | {
        whole: false
        removed: Seg
        spans: Drawing[]
        boundaries: { donor: Drawing; at: XY }[]
      }
    | null {
    if (d.kind === 'circle') return null
    const pane = this.paneSizeSafe()
    if (!pane) return null
    const seg = this.trimSegPx(d, pane)
    if (!seg) return null

    const cuts: { t: number; donor: Drawing }[] = []
    for (const o of this.bucket().drawings) {
      if (o.id === d.id || o.kind === 'circle') continue
      const os = this.hitSegPx(o, pane)
      if (!os) continue
      const t = segSegIntersect(seg, os)
      if (t !== null && t > TRIM_EPS && t < 1 - TRIM_EPS) cuts.push({ t, donor: o })
    }
    if (cuts.length === 0) return { whole: true }

    const tc = distToSeg(x, y, seg.a, seg.b).t
    let lo = 0
    let hi = 1
    let loDonor: Drawing | null = null
    let hiDonor: Drawing | null = null
    for (const c of cuts) {
      if (c.t <= tc) {
        if (c.t > lo) {
          lo = c.t
          loDonor = c.donor
        }
      } else if (c.t < hi) {
        hi = c.t
        hiDonor = c.donor
      }
    }

    const segLen = Math.hypot(seg.b.x - seg.a.x, seg.b.y - seg.a.y)
    const spans: Drawing[] = []
    const mk = (t0: number, t1: number) => {
      if ((t1 - t0) * segLen < MIN_SPAN_PX) return
      const s = this.spanToDrawing(d, seg, t0, t1)
      if (s) spans.push(s)
    }
    mk(0, lo)
    mk(hi, 1)

    const boundaries: { donor: Drawing; at: XY }[] = []
    if (loDonor) boundaries.push({ donor: loDonor, at: lerpSeg(seg, lo) })
    if (hiDonor) boundaries.push({ donor: hiDonor, at: lerpSeg(seg, hi) })
    return { whole: false, removed: { a: lerpSeg(seg, lo), b: lerpSeg(seg, hi) }, spans, boundaries }
  }

  /** Convert a [t0, t1] sub-span of a trimmed line back to a data-space
   *  drawing. Trend endpoint times quantize to bars with the price recomputed
   *  on the original pixel line — spans stay collinear with what was drawn. */
  private spanToDrawing(d: Drawing, seg: Seg, t0: number, t1: number): Drawing | null {
    const p0 = lerpSeg(seg, t0)
    const p1 = lerpSeg(seg, t1)
    if (d.kind === 'vline') {
      const time = d.points[0].time
      const pr0 = this.priceAtY(p0.y)
      const pr1 = this.priceAtY(p1.y)
      if (pr0 === null || pr1 === null || pr0 === pr1) return null
      return { id: mkId('dw'), kind: 'trend', points: [{ time, price: pr0 }, { time, price: pr1 }] }
    }
    if (d.kind === 'hline') {
      const price = d.points[0].price
      const ta = this.timeAtX(p0.x)
      const tb = this.timeAtX(p1.x)
      if (ta === null || tb === null || ta === tb) return null
      return { id: mkId('dw'), kind: 'trend', points: [{ time: ta, price }, { time: tb, price }] }
    }
    // trend
    const mkPt = (p: XY): Pt | null => {
      if (Math.abs(seg.b.x - seg.a.x) < 1) {
        // Vertical trend (both anchors on one bar): time is fixed, split by price.
        const price = this.priceAtY(p.y)
        return price === null ? null : { time: d.points[0].time, price }
      }
      const tQ = this.timeAtX(p.x)
      if (tQ === null) return null
      const xQ = this.xForTime(tQ)
      if (xQ === null) return null
      const yQ = seg.a.y + ((xQ - seg.a.x) * (seg.b.y - seg.a.y)) / (seg.b.x - seg.a.x)
      const price = this.priceAtY(yQ)
      return price === null ? null : { time: tQ, price }
    }
    const q0 = mkPt(p0)
    const q1 = mkPt(p1)
    if (!q0 || !q1) return null
    if (q0.time === q1.time && q0.price === q1.price) return null // quantized away
    return { id: mkId('dw'), kind: 'trend', points: [q0, q1] }
  }

  /** Split a cut-boundary drawing at the intersection point. Returns the
   *  replacement pieces, or null to keep the donor whole (intersection at an
   *  endpoint, unprojectable, or both halves would vanish). */
  private splitDonor(o: Drawing, at: XY): Drawing[] | null {
    const pane = this.paneSizeSafe()
    if (!pane) return null
    const seg = this.trimSegPx(o, pane)
    if (!seg) return null
    const u = distToSeg(at.x, at.y, seg.a, seg.b).t
    if (u < 0.001 || u > 0.999) return null
    const s1 = this.spanToDrawing(o, seg, 0, u)
    const s2 = this.spanToDrawing(o, seg, u, 1)
    const pieces = [s1, s2].filter((s): s is Drawing => s !== null)
    return pieces.length === 0 ? null : pieces
  }

  // ---- measure snapping ---------------------------------------------------

  /** Resolve where a click/hover lands: nearest line within HIT_PX, a candle
   *  when within CANDLE_SNAP_PX of its center x (and vertically near its
   *  high-low band), else a free point. Line and candle compete by pixel
   *  distance so the closer magnet wins. Circles are not snap targets (v1). */
  private snapAnchor(
    x: number,
    y: number,
    time: UTCTimestamp | null
  ): { anchor: MeasureAnchor; x: number; y: number; snapped: 'line' | 'candle' | null; drawing?: Drawing } | null {
    // line candidate
    const lineHit = this.hitTest(x, y, true)
    // candle candidate
    let candle: { time: UTCTimestamp; cx: number; cy: number; dist: number } | null = null
    const tNear = time ?? this.timeAtX(x)
    if (tNear !== null) {
      const bar = this.barAt(tNear)
      const cx = this.xForTime(tNear)
      if (bar && cx !== null && Math.abs(cx - x) <= CANDLE_SNAP_PX) {
        const yHigh = this.yForPrice(bar.high)
        const yLow = this.yForPrice(bar.low)
        const yClose = this.yForPrice(bar.close)
        if (yHigh !== null && yLow !== null && yClose !== null) {
          const top = Math.min(yHigh, yLow) - 8
          const bot = Math.max(yHigh, yLow) + 8
          if (y >= top && y <= bot) candle = { time: tNear, cx, cy: yClose, dist: Math.abs(cx - x) }
        }
      }
    }

    if (lineHit && (!candle || lineHit.dist <= candle.dist)) {
      const d = lineHit.drawing
      const t = this.timeAtX(lineHit.nx) ?? tNear
      const p = this.priceAtY(lineHit.ny)
      if (t !== null && p !== null) {
        return {
          anchor: { kind: 'line', drawingId: d.id, u: lineHit.u, time: t, price: p },
          x: lineHit.nx,
          y: lineHit.ny,
          snapped: 'line',
          drawing: d,
        }
      }
    }
    if (candle) {
      return {
        anchor: { kind: 'candle', time: candle.time },
        x: candle.cx,
        y: candle.cy,
        snapped: 'candle',
      }
    }
    const t = tNear
    const p = this.priceAtY(y)
    if (t === null || p === null) return null
    return { anchor: { kind: 'free', time: t, price: p }, x, y, snapped: null }
  }

  /** Where an anchor sits NOW: candles ride their bar, line anchors ride the
   *  drawing (and degrade to their snap-moment position if it was deleted),
   *  free anchors project directly. Null = unprojectable, measure hides. */
  private resolveAnchor(a: MeasureAnchor): ResolvedAnchor | null {
    if (a.kind === 'candle') {
      const bar = this.barAt(a.time)
      if (!bar) return null
      const x = this.xForTime(a.time)
      const y = this.yForPrice(bar.close)
      return x === null || y === null ? null : { x, y, price: bar.close, time: a.time }
    }
    if (a.kind === 'free') {
      const x = this.xForTime(a.time)
      const y = this.yForPrice(a.price)
      return x === null || y === null ? null : { x, y, price: a.price, time: a.time }
    }
    const d = this.bucket().drawings.find((x) => x.id === a.drawingId)
    if (!d || d.kind === 'circle') {
      // Drawing deleted/trimmed away: the snapshot keeps the measure honest.
      const x = this.xForTime(a.time)
      const y = this.yForPrice(a.price)
      return x === null || y === null ? null : { x, y, price: a.price, time: a.time }
    }
    if (d.kind === 'hline') {
      const x = this.xForTime(a.time)
      const y = this.yForPrice(d.points[0].price)
      return x === null || y === null ? null : { x, y, price: d.points[0].price, time: a.time }
    }
    if (d.kind === 'vline') {
      const x = this.xForTime(d.points[0].time)
      const y = this.yForPrice(a.price)
      return x === null || y === null ? null : { x, y, price: a.price, time: d.points[0].time }
    }
    // trend: u is stable in data space, so the anchor stays put through
    // pan/zoom and follows the line through DrawEditor edits.
    const pa = this.project(d.points[0])
    const pb = this.project(d.points[1])
    if (!pa || !pb) return null
    const x = pa.x + (pb.x - pa.x) * a.u
    const y = pa.y + (pb.y - pa.y) * a.u
    const price = this.priceAtY(y)
    const time = this.timeAtX(x) ?? a.time
    return price === null ? null : { x, y, price, time }
  }

  // ---- click handling -----------------------------------------------------

  private handleClick(p: MouseEventParams): void {
    // A drag just ended: the library fires a click on that same mouseup, and
    // acting on it would re-select (or worse, place) at the drop point.
    if (this.justDragged) {
      this.justDragged = false
      return
    }
    // 'pointer' no longer returns here: it IS the select tool now, so it needs
    // the pan guard and pane check below just as much as a placement does — a
    // drag-pan that ends over a drawing must not select it.
    // No point / another pane (RSI) = not a placement surface.
    if (p.point === undefined || (p.paneIndex ?? 0) !== 0) return
    if (
      this.downAt !== null &&
      Math.hypot(p.point.x - this.downAt.x, p.point.y - this.downAt.y) > CLICK_SLOP_PX
    ) {
      return // travelled: that was a pan, not a placement
    }
    const x = p.point.x
    const y = p.point.y
    const time = typeof p.time === 'number' ? p.time : null

    switch (this.tool) {
      case 'trend':
      case 'circle':
        this.clickTwoPoint(x, y, time)
        break
      case 'hline':
        this.clickHline(x, y, time)
        break
      case 'vline':
        this.clickVline(y, time)
        break
      case 'pointer':
        this.clickSelect(x, y)
        break
      case 'delete':
        this.clickDelete(x, y)
        break
      case 'trim':
        this.clickTrim(x, y)
        break
      case 'measure':
        this.clickMeasure(x, y, time)
        break
      case 'inspect':
        this.clickInspect(time)
        break
    }
  }

  /** An hline/vline under the cursor — what a trend endpoint snaps onto. */
  private lineUnder(x: number, y: number): Drawing | null {
    const d = this.hitTest(x, y)?.drawing
    return d && (d.kind === 'hline' || d.kind === 'vline') ? d : null
  }

  /** Where a trend endpoint would ACTUALLY land, in pixels, given the line
   *  under the cursor. The preview draws to this rather than to the raw cursor,
   *  so the rubber band visibly jumps onto the line before you commit — the
   *  same "magnet is visible before the click" the measure tool already has. */
  private snapToLine(x: number, y: number): { x: number; y: number; host: Drawing } | null {
    const host = this.lineUnder(x, y)
    if (!host) return null
    if (host.kind === 'hline') {
      const hy = this.yForPrice(host.points[0].price)
      return hy === null ? null : { x, y: hy, host }
    }
    const hx = this.xForTime(host.points[0].time)
    return hx === null ? null : { x: hx, y, host }
  }

  private clickTwoPoint(x: number, y: number, time: UTCTimestamp | null): void {
    // Whitespace right of the last bar has no time — nothing to anchor to.
    // The preview dims out there so the dead zone is visible before the click.
    if (time === null) return
    const price = this.priceAtY(y)
    if (price === null) return
    // SNAP AT PLACEMENT. Kade: "lines snap to each other". Dropping an endpoint
    // on an h/v line takes that line's coordinate EXACTLY — not within a few
    // pixels — and records that it is held there, so the two move together from
    // then on. Only trends: a circle held to a line has no meaning here, since
    // circles carry no constrainable coordinates at all.
    const host = this.tool === 'trend' ? this.lineUnder(x, y) : null
    const pt: Pt = { time, price }
    if (host) {
      if (host.kind === 'hline') pt.price = host.points[0].price
      else pt.time = host.points[0].time
    }
    if (this.pendingPt === null) {
      this.pendingPt = pt
      this.pendingHost = host?.id ?? null
      this.render()
      return
    }
    // Tool stays armed so several can go down without a toolbar round-trip.
    const id = mkId('dw')
    this.bucket().drawings.push({
      id,
      kind: this.tool === 'circle' ? 'circle' : 'trend',
      points: [this.pendingPt, pt],
    })
    if (this.tool === 'trend') {
      // A refused inference is dropped in silence: the user asked for a line,
      // not for a relation, so a modal about one they never requested would be
      // noise. An explicit hold-on says why.
      if (this.pendingHost) {
        this.addConstraint({
          kind: 'on',
          a: { id, part: 'a' },
          b: { id: this.pendingHost, part: 'line' },
        })
      }
      if (host) {
        this.addConstraint({ kind: 'on', a: { id, part: 'b' }, b: { id: host.id, part: 'line' } })
      }
    }
    this.pendingPt = null
    this.pendingHost = null
    this.commit()
  }

  private clickHline(x: number, y: number, time: UTCTimestamp | null): void {
    const price = this.priceAtY(y)
    if (price === null) return
    // The price IS the line; the time only places the handle, so clicking the
    // whitespace right of the data still works — handle falls to the last bar.
    const idx = this.barsIdx()
    const t =
      time ?? this.timeAtX(x) ?? (idx ? (idx.times[idx.times.length - 1] as UTCTimestamp) : null)
    if (t === null) return
    this.bucket().drawings.push({ id: mkId('dw'), kind: 'hline', points: [{ time: t, price }] })
    this.commit()
  }

  private clickVline(y: number, time: UTCTimestamp | null): void {
    if (time === null) return
    // Price is irrelevant to the line; it remembers where the handle sits.
    const price = this.priceAtY(y) ?? 0
    this.bucket().drawings.push({ id: mkId('dw'), kind: 'vline', points: [{ time, price }] })
    this.commit()
  }

  /** Add/remove one id. Shared so selection behaves identically however the
   *  pick arrived. */
  private toggleSelect(id: string): void {
    const at = this.selected.indexOf(id)
    if (at >= 0) this.selected.splice(at, 1)
    else this.selected.push(id)
    this.render()
    this.emit()
  }

  /** Translate every dragged drawing by a PIXEL delta, from its grab-time
   *  snapshot. Pixels, not data: the x axis is affine in bar INDEX, so a
   *  constant Δtime is not a constant Δx across a weekend. Projecting each
   *  original point, shifting it on screen and converting back is the only
   *  translation that follows the cursor everywhere on the axis. */
  private moveDragged(dx: number, dy: number): void {
    if (!this.drag) return
    const b = this.bucket()
    let moved = false
    for (const id of this.drag.ids) {
      const d = b.drawings.find((x) => x.id === id)
      const orig = this.drag.orig.get(id)
      if (!d || !orig) continue
      const next: Pt[] = []
      for (const p of orig) {
        const px = this.project(p)
        if (!px) break // unprojectable (scrolled off): leave this one alone
        const t = this.timeAtX(px.x + dx)
        const price = this.priceAtY(px.y + dy)
        if (t === null || price === null) break
        next.push({ time: (this.nearestBarTime(t) ?? t) as UTCTimestamp, price })
      }
      if (next.length !== orig.length) continue // partial move would deform it
      d.points = next
      moved = true
    }
    if (moved) {
      // Equalities re-satisfied every frame, so the attached trend stretches
      // WHILE you drag rather than snapping into place on mouseup.
      const grabbed = new Set(this.drag.ids)
      propagate(b.drawings, b.constraints, grabbed)
      // Slopes AFTER equalities: an endpoint riding an h-line has to have taken
      // its new price before the slope can work out where the far end goes.
      this.issue = this.restoreSlopes(grabbed)
      this.render()
    }
  }

  /** Where the dimension line currently sits in pixels — its midpoint for a
   *  locked one, the anchor midpoint for a free one. The grab reference. */
  private dimAnchorPx(m: Measure): XY | null {
    const A = this.resolveAnchor(m.a)
    const B = this.resolveAnchor(m.b)
    if (!A || !B) return null
    const seg = this.dimSeg(m, A, B)
    if (!seg) return null
    return { x: (seg.a.x + seg.b.x) / 2, y: (seg.a.y + seg.b.y) / 2 }
  }

  /** Move the DIMENSION. Never its anchors: the measured things stay exactly
   *  where they are, which is the whole promise of a CAD dimension.
   *
   *  Only the shared coordinate changes — the time of a vertical dimension, the
   *  price of a horizontal one — because that coordinate IS the position.
   *  A free (unlocked) measure has no shared coordinate to move, so dragging it
   *  LOCKS it: pull sideways and it becomes a vertical price dimension, pull up
   *  or down and it becomes a horizontal span. That is the "snap when near
   *  vertical" gesture, and judging it in pixels is legitimate here because it
   *  is a live gesture, not a stored quantity — what gets stored is a time or a
   *  price, either way. */
  private moveDimension(mx: number, my: number): void {
    if (!this.dimDrag) return
    const m = this.bucket().measures.find((v) => v.id === this.dimDrag!.id)
    if (!m) return
    // Cursor minus the grab offset = where the dimension should sit now.
    const x = mx - this.dimDrag.grab.x
    const y = my - this.dimDrag.grab.y
    // DECIDE the axis here, ASSIGN it only once the coordinate for it is known.
    // The old code wrote `{axis, at: 0}` as a placeholder and then returned
    // early whenever timeAtX came back null — which it does over the right-hand
    // whitespace, where there is no bar under the cursor. That left the measure
    // locked to epoch 0: unprojectable, invisible, and, since persistence
    // landed, SAVED that way.
    let axis: 'time' | 'price'
    if (!m.place) {
      const A = this.resolveAnchor(m.a)
      const B = this.resolveAnchor(m.b)
      if (!A || !B) return
      const ox = x - (A.x + B.x) / 2
      const oy = y - (A.y + B.y) / 2
      // Near the middle it stays free, so a nudge does not silently commit to
      // an axis. Beyond that, the DOMINANT direction wins: 2:1 rather than
      // bare comparison, so a diagonal drag does not flicker between the two.
      if (Math.hypot(ox, oy) < CLICK_SLOP_PX * 2) return
      if (Math.abs(ox) >= Math.abs(oy) * 2) axis = 'time'
      else if (Math.abs(oy) >= Math.abs(ox) * 2) axis = 'price'
      else return
    } else {
      axis = m.place.axis
    }
    if (axis === 'time') {
      const t = this.timeAtX(x)
      const snapped = t === null ? null : this.nearestBarTime(t)
      if (snapped === null) return
      m.place = { axis: 'time', at: snapped as UTCTimestamp }
    } else {
      const p = this.priceAtY(y)
      if (p === null) return
      m.place = { axis: 'price', at: p }
    }
    this.render()
  }

  /** End a drag. `commit` distinguishes mouseup from a fresh mousedown that
   *  supersedes an abandoned one. Pan/zoom is always restored - leaving the
   *  chart unpannable because a drag ended oddly would be far worse than a
   *  drawing landing a pixel out. */
  private endDrag(commit: boolean): void {
    const wasLive = this.drag?.live === true || this.dimDrag?.live === true
    if (this.drag || this.dimDrag) {
      try {
        this.chart.applyOptions({ handleScroll: true, handleScale: true })
      } catch { /* see onDown */ }
    }
    this.drag = null
    this.dimDrag = null
    if (commit && wasLive) {
      this.justDragged = true // swallow the click the library fires on mouseup
      // commit(), not a bare emit(): this is where the solver's quantization
      // pass will land, and it has to be able to repaint what it moved.
      this.commit()
    }
  }

  private clickSelect(x: number, y: number): void {
    const hit = this.hitAny(x, y)
    if (!hit) {
      if (this.selected.length > 0) {
        this.selected = []
        this.render()
        this.emit()
      }
      return
    }
    // Plain click REPLACES, modifier-click adds. Toggling on every plain click
    // was survivable while selecting needed an armed tool, but as the resting
    // gesture it silently accumulates: click a measure, then a line, and the
    // editor shows one object over a two-object selection whose Delete button
    // takes both. Replace is also what every drawing program does.
    if (this.downAdditive) {
      this.toggleSelect(hit.id)
      return
    }
    if (this.selected.length === 1 && this.selected[0] === hit.id) return
    this.selected = [hit.id]
    this.render()
    this.emit()
  }

  private clickDelete(x: number, y: number): void {
    const hit = this.hitAny(x, y)
    if (!hit) return // empty click = deliberate no-op, never "delete something"
    const b = this.bucket()
    // Clicking any SELECTED object deletes the whole selection; clicking an
    // unselected one deletes just it (selection survives).
    const doomed = this.selected.includes(hit.id) ? new Set(this.selected) : new Set([hit.id])
    b.drawings = b.drawings.filter((d) => !doomed.has(d.id))
    b.measures = b.measures.filter((m) => !doomed.has(m.id))
    b.pins = b.pins.filter((p) => !doomed.has(p.id))
    this.selected = this.selected.filter((id) => !doomed.has(id))
    this.commit()
  }

  private clickTrim(x: number, y: number): void {
    const hit = this.hitTest(x, y, true)
    if (!hit) return
    const res = this.computeTrim(hit.drawing, x, y)
    if (!res) return
    const b = this.bucket()
    const dead = new Set([hit.drawing.id])
    const added: Drawing[] = []
    if (!res.whole) {
      added.push(...res.spans)
      for (const bd of res.boundaries) {
        const pieces = this.splitDonor(bd.donor, bd.at)
        if (pieces) {
          dead.add(bd.donor.id)
          added.push(...pieces)
        }
      }
    }
    b.drawings = b.drawings.filter((d) => !dead.has(d.id))
    b.drawings.push(...added)
    this.selected = this.selected.filter((id) => !dead.has(id))
    this.commit()
  }

  private clickMeasure(x: number, y: number, time: UTCTimestamp | null): void {
    const snap = this.snapAnchor(x, y, time)
    if (!snap) return
    if (this.pendingAnchor === null) {
      this.pendingAnchor = snap.anchor
      this.render()
      return
    }
    const a = this.pendingAnchor
    this.bucket().measures.push({
      id: mkId('ms'), a, b: snap.anchor, place: this.defaultPlace(a, snap.anchor),
    })
    this.pendingAnchor = null
    this.commit()
  }

  /** The axis a new measure is born locked to, or undefined for a free one.
   *
   *  Decided in DATA space, never in pixels. A pixel threshold looks tempting
   *  and is wrong here: the default chart depth is "all", which puts ~2,700
   *  bars in ~900px, so ten pixels spans about thirty bars and nearly every
   *  measure would auto-lock at birth and lose its bar count.
   *
   *  Two h-lines are the case Kade described: "how far apart are these?" is a
   *  price question, so the dimension stands vertically between them. Until
   *  now that pair drew a DIAGONAL between wherever the two clicks landed, and
   *  printed a time row describing only the clicks — both artefacts of the
   *  click positions rather than facts about the lines. */
  private defaultPlace(a: MeasureAnchor, b: MeasureAnchor): MeasurePlace | undefined {
    const dA = a.kind === 'line' ? this.bucket().drawings.find((d) => d.id === a.drawingId) : null
    const dB = b.kind === 'line' ? this.bucket().drawings.find((d) => d.id === b.drawingId) : null
    // Through resolveAnchor rather than the anchor fields: an anchor's own
    // time/price is documented as the snap-moment SNAPSHOT kept as a fallback
    // for a deleted drawing, and only 'line'/'free' carry a price at all.
    const A = this.resolveAnchor(a)
    const B = this.resolveAnchor(b)
    if (!A || !B) return undefined
    if (dA?.kind === 'hline' && dB?.kind === 'hline') {
      // Midway between the two clicks: the user's own indication of where they
      // were looking, and draggable from there.
      const t = this.nearestBarTime(((A.time as number) + (B.time as number)) / 2)
      if (t !== null) return { axis: 'time', at: t as UTCTimestamp }
    }
    if (dA?.kind === 'vline' && dB?.kind === 'vline') {
      // The mirror: two v-lines are a "how long between these?" question, so
      // the dimension lies horizontally across them.
      return { axis: 'price', at: (A.price + B.price) / 2 }
    }
    return undefined
  }

  private clickInspect(time: UTCTimestamp | null): void {
    if (time === null) return
    if (!this.barAt(time)) return
    const b = this.bucket()
    // Click a pinned bar again to unpin — the pin is a toggle, not a stack.
    const at = b.pins.findIndex((p) => p.time === time)
    if (at >= 0) b.pins.splice(at, 1)
    else b.pins.push({ id: mkId('pin'), time })
    this.commit()
  }

  // ---- SVG / label primitives --------------------------------------------

  private el<K extends keyof SVGElementTagNameMap>(
    name: K,
    attrs: Record<string, string | number>
  ): SVGElementTagNameMap[K] {
    const e = document.createElementNS(SVG_NS, name)
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, String(v))
    this.svg.appendChild(e)
    return e
  }

  private line(a: XY, b: XY, stroke: string, width: number, dashed = false, opacity = 1): void {
    this.el('line', {
      x1: a.x, y1: a.y, x2: b.x, y2: b.y,
      stroke, 'stroke-width': width,
      ...(dashed ? { 'stroke-dasharray': '4 3' } : {}),
      ...(opacity !== 1 ? { 'stroke-opacity': opacity } : {}),
    })
  }

  private handleDot(x: number, y: number): void {
    this.el('circle', {
      cx: x, cy: y, r: 4,
      fill: STROKE, stroke: 'var(--surface)', 'stroke-width': 1.5,
    })
  }

  private ring(x: number, y: number, r = 5, stroke = STROKE): void {
    this.el('circle', { cx: x, cy: y, r, fill: 'none', stroke, 'stroke-width': 1.5 })
  }

  /** A positioned text chip in the HTML layer. ax/ay anchor the box on the
   *  point (0 = left/top edge at it, 1 = right/bottom). Always clamped into
   *  the pane so a label near an edge stays readable. */
  private chip(
    x: number,
    y: number,
    rows: ChipRow[],
    cls: string,
    ax: number,
    ay: number,
    pane: { width: number; height: number }
  ): { left: number; top: number; w: number; h: number } {
    const el = document.createElement('div')
    el.className = `cd-chip${cls ? ` ${cls}` : ''}`
    for (const r of rows) {
      const row = document.createElement('div')
      if (r.cls) row.className = r.cls
      row.textContent = r.text
      el.appendChild(row)
    }
    this.labels.appendChild(el)
    const w = el.offsetWidth
    const h = el.offsetHeight
    const left = Math.max(4, Math.min(x - w * ax, pane.width - w - 4))
    const top = Math.max(4, Math.min(y - h * ay, pane.height - h - 4))
    el.style.left = `${left}px`
    el.style.top = `${top}px`
    return { left, top, w, h }
  }

  // ---- rendering ----------------------------------------------------------

  private render(): void {
    const pane = this.paneSizeSafe()
    if (!pane) return
    // Sizing to pane 0 clips everything off the price scale and any lower
    // pane (RSI) without needing a clipPath.
    this.svg.setAttribute('width', String(pane.width))
    this.svg.setAttribute('height', String(pane.height))
    this.labels.style.width = `${pane.width}px`
    this.labels.style.height = `${pane.height}px`
    while (this.svg.firstChild) this.svg.removeChild(this.svg.firstChild)
    while (this.labels.firstChild) this.labels.removeChild(this.labels.firstChild)
    // Chip rectangles are collected into a draft and published at the very end
    // of this method: hitAny() may be called at any time and must always see a
    // COMPLETE frame (the last one), never this one half-built.
    this.zoneDraft = []

    const b = this.bucket()
    const cur = this.cursor

    // Hover resolution feeds hover states INTO the object render below —
    // every interacting tool telegraphs its target before the click.
    // hitTest is drawings-only and its callers (delete/trim) speak in lines.
    // Pointer's hover is the wider one and rides on this.hoverId instead, so
    // it can highlight a measurement or a pin too.
    const hover =
      cur && (this.tool === 'delete' || this.tool === 'trim')
        ? this.hitTest(cur.x, cur.y, this.tool === 'trim')
        : null
    const trimPrev =
      this.tool === 'trim' && hover ? this.computeTrim(hover.drawing, cur!.x, cur!.y) : null
    const dangerIds = new Set<string>()
    if (this.tool === 'delete' && hover) {
      if (this.selected.includes(hover.drawing.id)) for (const id of this.selected) dangerIds.add(id)
      else dangerIds.add(hover.drawing.id)
    }
    if (trimPrev?.whole) dangerIds.add(hover!.drawing.id) // no intersections: trim deletes whole

    if (!this.hidden) {
      const locked = new Set(
        b.constraints.filter((c) => c.kind === 'lock').map((c) => c.a.id)
      )
      for (const d of b.drawings) {
        this.renderDrawing(pane, d, {
          selected: this.selected.includes(d.id),
          hover: this.hoverId === d.id,
          danger: dangerIds.has(d.id),
        })
        if (locked.has(d.id)) this.renderLock(d)
      }
      // After every drawing, so a joint is never buried under the lines that
      // meet at it — which is exactly where it is least visible and most needed.
      this.renderJoints(b)
      for (const m of b.measures) this.renderMeasure(pane, m)
      for (const pin of b.pins) this.renderPin(pane, pin)
    }

    // Trim preview: the exact span the click would remove, plus rings at the
    // cut points — SolidWorks shows you the doomed span, so do we.
    if (trimPrev && !trimPrev.whole) {
      this.line(trimPrev.removed.a, trimPrev.removed.b, STROKE_DANGER, 2.5, true)
      this.ring(trimPrev.removed.a.x, trimPrev.removed.a.y, 4, STROKE_DANGER)
      this.ring(trimPrev.removed.b.x, trimPrev.removed.b.y, 4, STROKE_DANGER)
    }

    this.renderPreview(pane)
    // Frame complete — swap the chip hit-zones in atomically.
    this.hotZones = this.zoneDraft
  }

  /** The "held here" marker.
   *
   *  ONE glyph for both the live snap preview and the placed relation, and that
   *  is the whole point: what you saw hovering is what stays, so a connected
   *  endpoint is recognisable at a glance — and, just as importantly, so is one
   *  that only LOOKS close. Dashed while it is still a proposal, solid once it
   *  is real, which is the same preview idiom the trim and placement previews
   *  already use. */
  private renderJoint(x: number, y: number, live: boolean): void {
    // Classed so a test can tell a live proposal from a committed joint. A
    // count alone could not: the whole claim is that the two look the same and
    // mean different things.
    this.el('circle', {
      class: live ? 'cd-joint cd-joint-live' : 'cd-joint',
      cx: x,
      cy: y,
      r: 5.5,
      fill: 'var(--surface)',
      stroke: STROKE,
      'stroke-width': 1.75,
      ...(live ? { 'stroke-dasharray': '3 2' } : {}),
    })
    this.el('circle', { cx: x, cy: y, r: 1.8, fill: STROKE })
  }

  /** Every endpoint currently held onto a line. Drawn after the geometry so a
   *  joint is never hidden under the lines that meet at it. */
  private renderJoints(b: Bucket): void {
    const byId = new Map(b.drawings.map((d) => [d.id, d]))
    for (const c of b.constraints) {
      if (c.kind !== 'on') continue
      const d = byId.get(c.a.id)
      if (!d) continue
      const pt = d.points[c.a.part === 'b' ? 1 : 0]
      const at = pt ? this.project(pt) : null
      if (at) this.renderJoint(at.x, at.y, false)
    }
  }

  /** A padlock at the drawing's handle. CAD shows constrained geometry in a
   *  different colour; a glyph is the honest equivalent here, because this
   *  chart's stroke colour is already carrying selection, hover and danger, and
   *  a fourth meaning on the same channel would be unreadable. */
  private renderLock(d: Drawing): void {
    const at = this.project(d.points[0])
    if (!at) return
    const x = Math.round(at.x) + 8
    const y = Math.round(at.y) - 10
    this.el('rect', {
      x: x - 4, y: y, width: 8, height: 6, rx: 1,
      fill: STROKE, 'fill-opacity': 0.9,
    })
    // The shackle: an arc over the body, so it reads as closed.
    this.el('path', {
      d: `M ${x - 2.5} ${y} v -2 a 2.5 2.5 0 0 1 5 0 v 2`,
      fill: 'none', stroke: STROKE, 'stroke-width': 1.2,
    })
  }

  private renderDrawing(
    pane: { width: number; height: number },
    d: Drawing,
    st: { selected: boolean; hover: boolean; danger: boolean }
  ): void {
    const stroke = st.danger ? STROKE_DANGER : STROKE
    const width = st.selected || st.hover || st.danger ? 2.5 : 1.5
    const halo = st.danger ? HALO_DANGER : HALO
    const wantHalo = st.selected || st.hover || st.danger

    if (d.kind === 'circle') {
      const g = this.ellipsePx(d)
      if (!g) return
      if (wantHalo)
        this.el('ellipse', {
          cx: g.cx, cy: g.cy, rx: g.rx, ry: g.ry,
          fill: 'none', stroke: halo, 'stroke-width': 8,
        })
      this.el('ellipse', {
        cx: g.cx, cy: g.cy, rx: g.rx, ry: g.ry,
        fill: 'none', stroke, 'stroke-width': width,
      })
      if (st.selected) {
        const c = this.project(d.points[0])
        const e = this.project(d.points[1])
        if (c) this.handleDot(c.x, c.y)
        if (e) this.handleDot(e.x, e.y)
      }
      return
    }

    const seg = this.hitSegPx(d, pane)
    if (!seg) return // unprojectable now; back when the range returns
    if (wantHalo) this.line(seg.a, seg.b, halo, 8)
    this.line(seg.a, seg.b, stroke, width)
    if (st.selected) {
      // Handles sit on the DATA anchors (an hline handle can scroll away
      // with its bar; the line itself stays full-width).
      for (const p of d.points) {
        const at = this.project(p)
        if (at) this.handleDot(at.x, at.y)
      }
    }
  }

  /** The dimension's drawn segment, plus the witness lines that tie it back to
   *  what it measures. Free (no place) = the anchors themselves, i.e. exactly
   *  today's diagonal, with no witnesses.
   *
   *  Locked to 'time': both ends share x = xForTime(at) and keep their own
   *  prices — two prices at one time. Locked to 'price': both ends share
   *  y = yForPrice(at) and keep their own times. That IS the whole geometry;
   *  there is no angle in it. */
  private dimSeg(
    m: Measure,
    A: ResolvedAnchor,
    B: ResolvedAnchor
  ): { a: XY; b: XY; wit: [XY, XY][] } | null {
    if (!m.place) return { a: { x: A.x, y: A.y }, b: { x: B.x, y: B.y }, wit: [] }
    if (m.place.axis === 'time') {
      const x = this.xForTime(m.place.at)
      // Unprojectable (scrolled off, or the anchoring symbol was hidden on the
      // multi-chart page): HIDE it, the way resolveAnchor hides an unresolvable
      // measure. Falling back to the anchor-to-anchor diagonal would redraw the
      // exact defect this exists to fix, while the chip kept printing the
      // locked, price-only reading — geometry saying one thing and the label
      // another is worse than a measure that waits for the range to come back.
      if (x === null) return null
      return {
        a: { x, y: A.y },
        b: { x, y: B.y },
        wit: [
          [{ x: A.x, y: A.y }, { x, y: A.y }],
          [{ x: B.x, y: B.y }, { x, y: B.y }],
        ],
      }
    }
    const y = this.yForPrice(m.place.at)
    if (y === null) return null
    return {
      a: { x: A.x, y },
      b: { x: B.x, y },
      wit: [
        [{ x: A.x, y: A.y }, { x: A.x, y }],
        [{ x: B.x, y: B.y }, { x: B.x, y }],
      ],
    }
  }

  /** Overlay-only emphasis for a snapped line during measure hover. */
  private highlightDrawing(pane: { width: number; height: number }, d: Drawing): void {
    const seg = this.hitSegPx(d, pane)
    if (seg) this.line(seg.a, seg.b, HALO, 8)
  }

  private measureRows(
    aKind: string,
    bKind: string,
    A: ResolvedAnchor,
    B: ResolvedAnchor,
    place?: MeasurePlace
  ): ChipRow[] {
    const dp = B.price - A.price
    let priceTxt: string
    if (this.percentMode()) {
      // The axis is % change already — a $ delta here would be a lie.
      priceTxt = `Δ ${signOf(dp)}${Math.abs(dp).toFixed(2)}%`
    } else {
      const pct = A.price !== 0 ? (dp / Math.abs(A.price)) * 100 : null
      priceTxt = `Δ ${signOf(dp)}$${fmtNum(Math.abs(dp))}${
        pct === null ? '' : ` (${signOf(pct)}${Math.abs(pct).toFixed(2)}%)`
      }`
    }
    const dt = (B.time as number) - (A.time as number)
    const nBars = this.barCountBetween(A.time, B.time)
    const timeTxt = `${fmtSpan(dt)}${nBars === null ? '' : ` · ${nBars} bars`}`
    const priceRow: ChipRow = { text: priceTxt }
    const timeRow: ChipRow = { text: timeTxt }
    // The slope, in the unit the axis is already speaking. Per HOUR OF CHART
    // TIME rather than per candle: a candle is a different amount of time on
    // every timeframe, so "$/candle" would rename itself when you switched,
    // which is the whole thing chartMinutes exists to avoid.
    const cm = this.chartMinutes(A.time as number, B.time as number)
    const slope = cm === null ? null : slopePerHour(dp, cm)
    const slopeRow: ChipRow | null =
      slope === null
        ? null
        : {
            text: this.percentMode()
              ? `${signOf(slope)}${Math.abs(slope).toFixed(3)}%/h`
              : `${signOf(slope)}$${fmtNum(Math.abs(slope))}/h`,
          }
    const withSlope = (rows: ChipRow[]): ChipRow[] =>
      slopeRow === null ? rows : [...rows, slopeRow]
    // An axis-locked dimension answers ONE question, and printing the other
    // number would be printing a property of the dimension's own placement
    // rather than of anything measured. A vertical dimension's two ends share
    // a time, so "0 bars" says nothing; a horizontal one's ends share a price,
    // so the price delta is always zero. Drop the dead row, and drop the slope
    // with it — a locked dimension has no slope, by construction.
    if (place?.axis === 'time') {
      priceRow.cls = 'em'
      return [priceRow]
    }
    if (place?.axis === 'price') {
      timeRow.cls = 'em'
      return [timeRow]
    }
    if (aKind === 'candle' && bKind === 'candle') {
      timeRow.cls = 'em' // candle↔candle is a how-long question
      return withSlope([timeRow, priceRow])
    }
    if (aKind === 'line' && bKind === 'line') {
      priceRow.cls = 'em' // line↔line (two hlines) is a how-far question
      return withSlope([priceRow, timeRow])
    }
    return withSlope([priceRow, timeRow]) // mixed: both, no thumb on the scale
  }

  /** liveB carries the in-progress second point during placement. */
  private renderMeasure(
    pane: { width: number; height: number },
    m: Measure,
    liveB?: { anchor: MeasureAnchor; x: number; y: number }
  ): void {
    const A = this.resolveAnchor(m.a)
    const B = liveB
      ? (() => {
          const r = this.resolveAnchor(liveB.anchor)
          return r ?? null
        })()
      : this.resolveAnchor(m.b)
    if (!A || !B) return
    // A measurement is pickable now, so it has to look pickable and look
    // picked. Without this a click that landed was indistinguishable from one
    // that missed, which is most of why "they are not clickable" persisted.
    const picked = !liveB && this.selected.includes(m.id)
    const hot = !liveB && !picked && this.hoverId === m.id
    const ink = picked || hot ? STROKE : MEASURE_STROKE
    const wide = picked ? 2 : 1.25
    // Where the dimension LINE goes, which is not necessarily between the
    // anchors: an axis-locked one stands off at its own time or price, joined
    // back to what it measures by witness lines. The in-progress preview is
    // always the free diagonal — you cannot stand off from a point you have
    // not placed yet.
    const seg = liveB ? { a: A as XY, b: B as XY, wit: [] as [XY, XY][] } : this.dimSeg(m, A, B)
    if (!seg) return // locked somewhere unprojectable — wait for the range
    for (const [w0, w1] of seg.wit) this.line(w0, w1, MEASURE_STROKE, 0.75, true)
    this.line(seg.a, seg.b, ink, wide, true)
    // Dimension end ticks, perpendicular to the dimension line itself.
    const len = Math.hypot(seg.b.x - seg.a.x, seg.b.y - seg.a.y)
    if (len > 1) {
      const px = -(seg.b.y - seg.a.y) / len
      const py = (seg.b.x - seg.a.x) / len
      for (const e of [seg.a, seg.b]) {
        this.line(
          { x: e.x - px * 5, y: e.y - py * 5 },
          { x: e.x + px * 5, y: e.y + py * 5 },
          ink,
          wide
        )
      }
    }
    // Dots stay on the ANCHORS — they mark what is being measured, which is
    // the thing a witness line exists to keep visible once the dimension has
    // been dragged away from it.
    this.el('circle', { cx: A.x, cy: A.y, r: 2, fill: ink })
    this.el('circle', { cx: B.x, cy: B.y, r: 2, fill: ink })
    const rows = this.measureRows(
      m.a.kind, liveB ? liveB.anchor.kind : m.b.kind, A, B, liveB ? undefined : m.place
    )
    // On the DIMENSION line's midpoint, not the anchors': the chip is the grab
    // handle, so it has to travel with the thing being grabbed. For a free
    // measure the two are the same point.
    // The axis rides on the chip as a class so it is observable from the DOM.
    // Without it a test can only read a selection COUNT, which cannot tell a
    // locked dimension from a free one — and "is it actually locked" is the
    // whole claim being made here.
    const axisCls = m.place ? ` cd-ax-${m.place.axis}` : ''
    const box = this.chip(
      (seg.a.x + seg.b.x) / 2, (seg.a.y + seg.b.y) / 2 - 10, rows,
      `${picked ? 'cd-sel' : hot ? 'cd-hot' : ''}${liveB ? '' : axisCls}`.trim(), 0.5, 1, pane
    )
    // The chip IS the measurement's handle — it is what the eye reads as the
    // object, so it is what a click must resolve to. Only for a REAL stored
    // measure: liveB means this is the in-progress preview, which is not in
    // b.measures and must never become clickable.
    if (!liveB) this.zoneDraft.push({ ...box, kind: 'measure', id: m.id })
  }

  private inspectRows(bar: Bar, time: number): ChipRow[] {
    const range = bar.high - bar.low
    const body = Math.abs(bar.close - bar.open)
    const rangePct = bar.low !== 0 ? ` (${((range / bar.low) * 100).toFixed(2)}%)` : ''
    const bodyPct = bar.open !== 0 ? ` (${((body / bar.open) * 100).toFixed(2)}%)` : ''
    return [
      { text: fmtDate(time), cls: 'dim' },
      {
        text: `O ${fmtNum(bar.open)}  H ${fmtNum(bar.high)}  L ${fmtNum(bar.low)}  C ${fmtNum(bar.close)}`,
        cls: 'em',
      },
      { text: `Range $${fmtNum(range)}${rangePct} · Body $${fmtNum(body)}${bodyPct}` },
      { text: `Vol ${fmtVol(bar.volume)}` },
    ]
  }

  private renderPin(pane: { width: number; height: number }, pin: InspectPin): void {
    const bar = this.barAt(pin.time)
    if (!bar) return
    const x = this.xForTime(pin.time)
    const yHigh = this.yForPrice(bar.high)
    if (x === null || yHigh === null) return
    // Pins are pickable exactly like measures — and their chip is the largest
    // one drawn, so it absorbs clicks over a big rectangle. It has to show the
    // same two states, or a click that landed on a pin looks like a click that
    // hit nothing while quietly joining it to the selection.
    const picked = this.selected.includes(pin.id)
    const hot = !picked && this.hoverId === pin.id
    const cls = `cd-inspect cd-pin${picked ? ' cd-sel' : hot ? ' cd-hot' : ''}`
    const box = this.chip(x, yHigh - 12, this.inspectRows(bar, pin.time), cls, 0.5, 1, pane)
    this.zoneDraft.push({ ...box, kind: 'pin', id: pin.id })
    // Stem from the chip down to the bar it belongs to — a floating box with
    // no stem stops meaning anything the moment two pins share a screen.
    this.line({ x, y: box.top + box.h }, { x, y: yHigh - 2 },
              picked || hot ? STROKE : MEASURE_STROKE, picked ? 1.75 : 1)
  }

  private renderPreview(pane: { width: number; height: number }): void {
    const cur = this.cursor
    switch (this.tool) {
      case 'hline': {
        // The line follows the crosshair BEFORE the click — placement is
        // never blind (the core clunkiness fix).
        if (!cur) return
        this.line({ x: 0, y: cur.y }, { x: pane.width, y: cur.y }, STROKE, 1.5, true)
        const price = this.priceAtY(cur.y)
        if (price !== null)
          this.chip(pane.width - 6, cur.y - 6, [{ text: fmtNum(price), cls: 'em' }], '', 1, 1, pane)
        return
      }
      case 'vline': {
        if (!cur) return
        const dead = cur.time === null // whitespace right of the data: unplaceable
        this.line({ x: cur.x, y: 0 }, { x: cur.x, y: pane.height }, STROKE, 1.5, true, dead ? 0.35 : 1)
        if (!dead)
          this.chip(cur.x + 6, pane.height - 6, [{ text: fmtDate(cur.time!), cls: 'em' }], '', 0, 1, pane)
        return
      }
      case 'trend':
      case 'circle': {
        // THE MAGNET, VISIBLE BEFORE THE CLICK — and before the FIRST click
        // too, which is why this sits above the pendingPt guard. Without it the
        // snap only became apparent after both ends were down, so there was no
        // way to tell a connected endpoint from one that merely looked close.
        const snap = this.tool === 'trend' && cur ? this.snapToLine(cur.x, cur.y) : null
        if (snap) {
          this.highlightDrawing(pane, snap.host)
          this.renderJoint(snap.x, snap.y, true)
        }
        if (this.pendingPt === null) return
        const a = this.project(this.pendingPt)
        if (!a) return
        this.handleDot(a.x, a.y)
        if (!cur) return
        const dead = cur.time === null
        const op = dead ? 0.35 : 1
        if (this.tool === 'trend') {
          // Drawn to the SNAPPED point, so the band jumps onto the line under
          // the cursor instead of trailing a few pixels off it.
          this.line(a, snap ?? cur, STROKE, 1.5, true, op)
        } else {
          this.el('ellipse', {
            cx: a.x, cy: a.y,
            rx: Math.max(Math.abs(cur.x - a.x), 1), ry: Math.max(Math.abs(cur.y - a.y), 1),
            fill: 'none', stroke: STROKE, 'stroke-width': 1.5,
            'stroke-dasharray': '4 3', 'stroke-opacity': op,
          })
        }
        // Live deltas beside the cursor: what the line will span if committed.
        if (!dead) {
          const price = this.priceAtY(cur.y)
          if (price !== null) {
            const A: ResolvedAnchor = { x: a.x, y: a.y, price: this.pendingPt.price, time: this.pendingPt.time }
            const B: ResolvedAnchor = { x: cur.x, y: cur.y, price, time: cur.time! }
            this.chip(cur.x + 14, cur.y + 14, this.measureRows('free', 'free', A, B), '', 0, 0, pane)
          }
        }
        return
      }
      case 'measure': {
        if (!cur) return
        const snap = this.snapAnchor(cur.x, cur.y, cur.time)
        if (snap) {
          // The magnet is visible before the click: ring on the snap point,
          // halo on a snapped line.
          if (snap.snapped === 'line' && snap.drawing) this.highlightDrawing(pane, snap.drawing)
          if (snap.snapped !== null) this.ring(snap.x, snap.y, 5)
          if (this.pendingAnchor !== null) {
            this.renderMeasure(pane, { id: 'live', a: this.pendingAnchor, b: snap.anchor }, snap)
          }
        } else if (this.pendingAnchor !== null) {
          const A = this.resolveAnchor(this.pendingAnchor)
          if (A) this.line(A, cur, MEASURE_STROKE, 1.25, true, 0.35)
        }
        return
      }
      case 'inspect': {
        if (!cur || cur.time === null) return
        const bar = this.barAt(cur.time)
        if (!bar) return
        this.chip(cur.x + 16, cur.y + 16, this.inspectRows(bar, cur.time), 'cd-inspect', 0, 0, pane)
        return
      }
      default:
        return
    }
  }
}
