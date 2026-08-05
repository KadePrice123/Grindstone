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

export interface Measure {
  id: string
  a: MeasureAnchor
  b: MeasureAnchor
}

/** A pinned inspect readout: the bar keeps its identity, OHLC resolves live. */
export interface InspectPin {
  id: string
  time: UTCTimestamp
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
}

// ---------------------------------------------------------------------------
// Internals
// ---------------------------------------------------------------------------

interface Bucket {
  drawings: Drawing[]
  measures: Measure[]
  pins: InspectPin[]
}

const sessionStore = new Map<string, Bucket>()

let nextId = 1
const mkId = (prefix: string) => `${prefix}${nextId++}`

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
  private teardown: (() => void)[] = []

  // bars index cache, keyed by array identity — pages hand the same array
  // until data actually changes, so this rebuilds only on real reloads.
  private barsCacheSrc: Bar[] | null = null
  private barsCacheTimes: number[] = []
  private barsCacheMap = new Map<number, number>()

  constructor(
    key: string,
    chart: IChartApi,
    series: ISeriesApi<SeriesType>,
    opts?: { bars?: Bar[] | (() => Bar[]); percent?: () => boolean }
  ) {
    this.key = key
    this.chart = chart
    this.series = series
    this.setBarsSource(opts?.bars)
    this.percentOpt = opts?.percent
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
      if (!hit || hit.kind !== 'drawing') return // measures/pins move in a later stage
      // Grabbing something already selected moves the WHOLE selection - the
      // same rule clickDelete uses, so "what will this act on" has one answer.
      const b = this.bucket()
      const ids = this.selected.includes(hit.id)
        ? this.selected.filter((id) => b.drawings.some((d) => d.id === id))
        : [hit.id]
      const orig = new Map<string, Pt[]>()
      for (const id of ids) {
        const d = b.drawings.find((x) => x.id === id)
        if (d) orig.set(id, d.points.map((p) => ({ ...p })))
      }
      if (orig.size === 0) return
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
      if (!this.drag) return
      const r = this.host.getBoundingClientRect()
      const x = e.clientX - r.left
      const y = e.clientY - r.top
      const dx = x - this.drag.from.x
      const dy = y - this.drag.from.y
      if (!this.drag.live) {
        if (Math.hypot(dx, dy) <= CLICK_SLOP_PX) return // still a click
        this.drag.live = true
      }
      this.moveDragged(dx, dy)
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
    this.key = key
    this.pendingPt = null
    this.pendingAnchor = null
    this.selected = []
    this.cursor = null
    this.render()
    this.emit()
  }

  setTool(tool: DrawTool): void {
    if (tool === this.tool) return
    this.tool = tool
    this.pendingPt = null
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
    this.render()
    this.emit()
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
    this.render()
    this.emit()
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
    this.render()
    this.emit()
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
    this.render()
    this.emit()
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
    }
  }

  /** Single subscriber (the owning page). Fires immediately so the page's
   *  data-draw-* attrs and DrawEditor start synced, then on every change. */
  onChange(cb: (s: DrawState) => void): void {
    this.changeCb = cb
    cb(this.getState())
  }

  destroy(): void {
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
    let b = sessionStore.get(this.key)
    if (!b) {
      b = { drawings: [], measures: [], pins: [] }
      sessionStore.set(this.key, b)
    }
    return b
  }

  private emit(): void {
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

  private barsIdx(): { bars: Bar[]; times: number[]; map: Map<number, number> } | null {
    const arr = this.barsOpt?.()
    if (!arr || arr.length === 0) return null
    if (arr !== this.barsCacheSrc) {
      this.barsCacheSrc = arr
      this.barsCacheTimes = arr.map((b) => Math.floor(new Date(b.ts).getTime() / 1000))
      this.barsCacheMap = new Map(this.barsCacheTimes.map((t, i) => [t, i]))
    }
    return { bars: arr, times: this.barsCacheTimes, map: this.barsCacheMap }
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
    const lo = Math.min(ia, ib)
    const hi = Math.max(ia, ib)
    let mins = 0
    for (let i = lo; i < hi; i++) {
      const gap = (idx.times[i + 1] - idx.times[i]) / 60
      mins += gap > 0 && gap < per ? gap : per
    }
    return ib >= ia ? mins : -mins
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

  private clickTwoPoint(_x: number, y: number, time: UTCTimestamp | null): void {
    // Whitespace right of the last bar has no time — nothing to anchor to.
    // The preview dims out there so the dead zone is visible before the click.
    if (time === null) return
    const price = this.priceAtY(y)
    if (price === null) return
    const pt: Pt = { time, price }
    if (this.pendingPt === null) {
      this.pendingPt = pt
      this.render()
      return
    }
    // Tool stays armed so several can go down without a toolbar round-trip.
    this.bucket().drawings.push({
      id: mkId('dw'),
      kind: this.tool === 'circle' ? 'circle' : 'trend',
      points: [this.pendingPt, pt],
    })
    this.pendingPt = null
    this.render()
    this.emit()
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
    this.render()
    this.emit()
  }

  private clickVline(y: number, time: UTCTimestamp | null): void {
    if (time === null) return
    // Price is irrelevant to the line; it remembers where the handle sits.
    const price = this.priceAtY(y) ?? 0
    this.bucket().drawings.push({ id: mkId('dw'), kind: 'vline', points: [{ time, price }] })
    this.render()
    this.emit()
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
    if (moved) this.render()
  }

  /** End a drag. `commit` distinguishes mouseup from a fresh mousedown that
   *  supersedes an abandoned one. Pan/zoom is always restored - leaving the
   *  chart unpannable because a drag ended oddly would be far worse than a
   *  drawing landing a pixel out. */
  private endDrag(commit: boolean): void {
    const wasLive = this.drag?.live === true
    if (this.drag) {
      try {
        this.chart.applyOptions({ handleScroll: true, handleScale: true })
      } catch { /* see onDown */ }
    }
    this.drag = null
    if (commit && wasLive) {
      this.justDragged = true // swallow the click the library fires on mouseup
      this.emit()
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
    this.render()
    this.emit()
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
    this.render()
    this.emit()
  }

  private clickMeasure(x: number, y: number, time: UTCTimestamp | null): void {
    const snap = this.snapAnchor(x, y, time)
    if (!snap) return
    if (this.pendingAnchor === null) {
      this.pendingAnchor = snap.anchor
      this.render()
      return
    }
    this.bucket().measures.push({ id: mkId('ms'), a: this.pendingAnchor, b: snap.anchor })
    this.pendingAnchor = null
    this.render()
    this.emit()
  }

  private clickInspect(time: UTCTimestamp | null): void {
    if (time === null) return
    if (!this.barAt(time)) return
    const b = this.bucket()
    // Click a pinned bar again to unpin — the pin is a toggle, not a stack.
    const at = b.pins.findIndex((p) => p.time === time)
    if (at >= 0) b.pins.splice(at, 1)
    else b.pins.push({ id: mkId('pin'), time })
    this.render()
    this.emit()
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
      for (const d of b.drawings) {
        this.renderDrawing(pane, d, {
          selected: this.selected.includes(d.id),
          hover: this.hoverId === d.id,
          danger: dangerIds.has(d.id),
        })
      }
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

  /** Overlay-only emphasis for a snapped line during measure hover. */
  private highlightDrawing(pane: { width: number; height: number }, d: Drawing): void {
    const seg = this.hitSegPx(d, pane)
    if (seg) this.line(seg.a, seg.b, HALO, 8)
  }

  private measureRows(aKind: string, bKind: string, A: ResolvedAnchor, B: ResolvedAnchor): ChipRow[] {
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
    this.line(A, B, ink, wide, true)
    // Dimension end ticks, perpendicular to the connector.
    const len = Math.hypot(B.x - A.x, B.y - A.y)
    if (len > 1) {
      const px = -(B.y - A.y) / len
      const py = (B.x - A.x) / len
      for (const e of [A, B]) {
        this.line(
          { x: e.x - px * 5, y: e.y - py * 5 },
          { x: e.x + px * 5, y: e.y + py * 5 },
          ink,
          wide
        )
      }
    }
    this.el('circle', { cx: A.x, cy: A.y, r: 2, fill: ink })
    this.el('circle', { cx: B.x, cy: B.y, r: 2, fill: ink })
    const rows = this.measureRows(m.a.kind, liveB ? liveB.anchor.kind : m.b.kind, A, B)
    const box = this.chip(
      (A.x + B.x) / 2, (A.y + B.y) / 2 - 10, rows,
      picked ? 'cd-sel' : hot ? 'cd-hot' : '', 0.5, 1, pane
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
        if (this.pendingPt === null) return
        const a = this.project(this.pendingPt)
        if (!a) return
        this.handleDot(a.x, a.y)
        if (!cur) return
        const dead = cur.time === null
        const op = dead ? 0.35 : 1
        if (this.tool === 'trend') {
          this.line(a, cur, STROKE, 1.5, true, op)
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
