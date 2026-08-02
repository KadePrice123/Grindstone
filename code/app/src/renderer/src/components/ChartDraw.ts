/**
 * Drawing layer for lightweight-charts: an SVG overlay sized to pane 0, with
 * drawings stored in DATA space {time, price} and projected to pixels only
 * when something actually changes. Re-render triggers are all event-driven —
 * visible-range changes, container resizes (ResizeObserver), crosshair moves
 * while a trend line is half-placed, and drawing edits. NO rAF loop, NO
 * setInterval: anything that repaints at rest holds the GPU forever (the
 * spinning-logo lesson, measured at ~10% of a core).
 *
 * Drawings are keyed per chart (e.g. "SPY|1Day") in a module-level Map so a
 * tab revisit within the session keeps them. The key must include the
 * timeframe: anchors are bar timestamps and timeToCoordinate() only resolves
 * times that exist on the current scale, so a 1Min anchor is unprojectable
 * on a 1Day axis. Separate buckets keep every stored drawing renderable in
 * the bucket it was drawn in.
 */
import type {
  IChartApi,
  ISeriesApi,
  MouseEventParams,
  SeriesType,
  UTCTimestamp,
} from 'lightweight-charts'

export type DrawTool = 'pointer' | 'trend' | 'hline'

interface Anchor {
  time: UTCTimestamp
  price: number
}

type Drawing =
  | { kind: 'trend'; a: Anchor; b: Anchor }
  | { kind: 'hline'; at: Anchor } // price is the line; time only places the handle

const sessionStore = new Map<string, Drawing[]>()

const SVG_NS = 'http://www.w3.org/2000/svg'
/** A press that travelled further than this before release is a pan. */
const CLICK_SLOP_PX = 4

export class ChartDraw {
  readonly chart: IChartApi
  private series: ISeriesApi<SeriesType>
  private key: string
  private tool: DrawTool = 'pointer'
  private pending: Anchor | null = null
  private cursor: { x: number; y: number } | null = null
  private readonly host: HTMLElement
  private readonly svg: SVGSVGElement
  private readonly ro: ResizeObserver
  private downAt: { x: number; y: number } | null = null
  private teardown: (() => void)[] = []

  constructor(key: string, chart: IChartApi, series: ISeriesApi<SeriesType>) {
    this.key = key
    this.chart = chart
    this.series = series
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

    const ts = chart.timeScale()
    const onRange = () => this.render()
    ts.subscribeVisibleLogicalRangeChange(onRange)
    this.teardown.push(() => ts.unsubscribeVisibleLogicalRangeChange(onRange))

    const onClick = (p: MouseEventParams) => this.handleClick(p)
    chart.subscribeClick(onClick)
    this.teardown.push(() => chart.unsubscribeClick(onClick))

    // Live preview while a trend line has one anchor down. Subscribed always
    // but an immediate no-op unless placing — no per-move cost at rest.
    const onMove = (p: MouseEventParams) => {
      if (this.pending === null) return
      this.cursor =
        p.point !== undefined && (p.paneIndex ?? 0) === 0
          ? { x: p.point.x, y: p.point.y }
          : null
      this.render()
    }
    chart.subscribeCrosshairMove(onMove)
    this.teardown.push(() => chart.unsubscribeCrosshairMove(onMove))

    // Click-vs-pan guard: the library's click callback also fires on the
    // mouseup that ends a drag-pan, which would plant an anchor at every
    // pan-end while a tool is armed.
    const onDown = (e: MouseEvent) => {
      const r = this.host.getBoundingClientRect()
      this.downAt = { x: e.clientX - r.left, y: e.clientY - r.top }
    }
    this.host.addEventListener('mousedown', onDown)
    this.teardown.push(() => this.host.removeEventListener('mousedown', onDown))

    // Fires only on actual size changes — event-driven, not a poll.
    this.ro = new ResizeObserver(() => this.render())
    this.ro.observe(this.host)
    this.teardown.push(() => this.ro.disconnect())

    this.render()
  }

  /** Chart.tsx rebuilds its series on every data change; re-point at the live one. */
  setSeries(series: ISeriesApi<SeriesType>): void {
    this.series = series
    this.render()
  }

  /** Switch store bucket (symbol/timeframe change) — drops any half-placed line. */
  setKey(key: string): void {
    if (key === this.key) return
    this.key = key
    this.pending = null
    this.cursor = null
    this.render()
  }

  setTool(tool: DrawTool): void {
    if (tool === this.tool) return
    this.tool = tool
    this.pending = null
    this.cursor = null
    this.render()
  }

  clear(): void {
    sessionStore.set(this.key, [])
    this.pending = null
    this.cursor = null
    this.render()
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
    this.svg.remove()
  }

  private drawings(): Drawing[] {
    let d = sessionStore.get(this.key)
    if (!d) {
      d = []
      sessionStore.set(this.key, d)
    }
    return d
  }

  private handleClick(p: MouseEventParams): void {
    if (this.tool === 'pointer') return
    // No time = the whitespace right of the last bar (or off the data); no
    // point / another pane (RSI) = not a placement surface.
    if (p.point === undefined || typeof p.time !== 'number') return
    if ((p.paneIndex ?? 0) !== 0) return
    if (
      this.downAt !== null &&
      Math.hypot(p.point.x - this.downAt.x, p.point.y - this.downAt.y) > CLICK_SLOP_PX
    ) {
      return // travelled: that was a pan, not a placement
    }
    let price: number | null = null
    try {
      price = this.series.coordinateToPrice(p.point.y)
    } catch {
      return // series disposed mid-rebuild
    }
    if (price === null) return
    const anchor: Anchor = { time: p.time, price }
    if (this.tool === 'hline') {
      this.drawings().push({ kind: 'hline', at: anchor })
      this.render()
      return
    }
    // trend: first click anchors, second completes. The tool stays armed so
    // several lines can go down without a toolbar round-trip.
    if (this.pending === null) {
      this.pending = anchor
    } else {
      this.drawings().push({ kind: 'trend', a: this.pending, b: anchor })
      this.pending = null
      this.cursor = null
    }
    this.render()
  }

  private project(a: Anchor): { x: number; y: number } | null {
    try {
      const x = this.chart.timeScale().timeToCoordinate(a.time)
      const y = this.series.priceToCoordinate(a.price)
      return x === null || y === null ? null : { x, y }
    } catch {
      return null // chart or series disposed between an event and this render
    }
  }

  private render(): void {
    let pane: { width: number; height: number }
    try {
      pane = this.chart.paneSize(0)
    } catch {
      return
    }
    // Sizing the svg to pane 0 clips drawings off the price scale and any
    // lower pane (RSI) without needing a clipPath.
    this.svg.setAttribute('width', String(pane.width))
    this.svg.setAttribute('height', String(pane.height))
    while (this.svg.firstChild) this.svg.removeChild(this.svg.firstChild)

    const line = (x1: number, y1: number, x2: number, y2: number, dashed = false) => {
      const el = document.createElementNS(SVG_NS, 'line')
      el.setAttribute('x1', String(x1))
      el.setAttribute('y1', String(y1))
      el.setAttribute('x2', String(x2))
      el.setAttribute('y2', String(y2))
      el.setAttribute('stroke', 'var(--accent)')
      el.setAttribute('stroke-width', '1.5')
      if (dashed) el.setAttribute('stroke-dasharray', '4 3')
      this.svg.appendChild(el)
    }
    const handle = (x: number, y: number) => {
      const el = document.createElementNS(SVG_NS, 'circle')
      el.setAttribute('cx', String(x))
      el.setAttribute('cy', String(y))
      el.setAttribute('r', '3.5')
      el.setAttribute('fill', 'var(--accent)')
      this.svg.appendChild(el)
    }

    for (const d of this.drawings()) {
      if (d.kind === 'hline') {
        let y: number | null = null
        try {
          y = this.series.priceToCoordinate(d.at.price)
        } catch {
          y = null
        }
        if (y === null) continue
        line(0, y, pane.width, y)
        const at = this.project(d.at)
        if (at) handle(at.x, at.y) // handle hides when its bar scrolls away
      } else {
        const a = this.project(d.a)
        const b = this.project(d.b)
        if (!a || !b) continue // unprojectable now; back when the range returns
        line(a.x, a.y, b.x, b.y)
        handle(a.x, a.y)
        handle(b.x, b.y)
      }
    }

    if (this.pending !== null) {
      const a = this.project(this.pending)
      if (a) {
        handle(a.x, a.y)
        if (this.cursor) line(a.x, a.y, this.cursor.x, this.cursor.y, true)
      }
    }
  }
}
