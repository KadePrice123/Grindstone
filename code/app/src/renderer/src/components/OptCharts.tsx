/** The Opt page's three charts, on lightweight-charts — the SAME engine the
 *  candle chart runs on, so crosshair, pan, zoom, axis labels and the whole
 *  feel match the rest of the app instead of approximating it.
 *
 *  A hand-rolled SVG renderer sat here first and taught the lesson: it looked
 *  wrong next to the main chart, its crosshair ran backwards on the reversed
 *  axis, and detail was unreadable. The library's crosshair cannot run
 *  backwards, because it tracks the pointer natively.
 *
 *  DTE AXES. Two of these charts run on days-to-expiry, not calendar time,
 *  and the library only plots time. So DTE is mapped onto a synthetic UTC day
 *  lattice (one day per DTE step) and the tick formatter prints the DTE the
 *  slot stands for. The fan chart maps dte -> (cap - dte) so expiry sits at
 *  the RIGHT edge and the x axis counts down toward 0d, the way an option's
 *  life actually runs.
 *
 *  TWO PANELS, not three: the bid-ask spread chart was removed after review.
 *  It answered a different question in different units beside a premium chart,
 *  and the two were read as one — the numbers "did not match" because they
 *  were never the same quantity.
 */
import { useEffect, useRef } from 'react'
import {
  ColorType,
  LineSeries,
  LineStyle,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts'
import type { ExitPoint } from '../optgrid'

// The candle chart's own palette (Chart.tsx) — one canvas, one theme.
const C = {
  bg: '#101214',
  grid: '#22262A',
  text: '#9AA0A6',
  accent: '#D98324',
  up: '#2EBD85',
  down: '#E5484D',
  dim: '#9AA0A6',
  under: '#6BA4E8',
  band: 'rgba(232, 234, 237, 0.10)',
  bandInner: 'rgba(232, 234, 237, 0.16)',
}

const DAY = 86400
// Any fixed epoch works for a synthetic axis; days are only slot spacing.
const BASE = Date.UTC(2020, 0, 6) / 1000 // a Monday

function baseOptions(height: number) {
  return {
    height,
    autoSize: false,
    layout: {
      background: { type: ColorType.Solid, color: C.bg },
      textColor: C.text,
      attributionLogo: true, // Apache-2.0 NOTICE requirement — keep it on
      fontSize: 11,
    },
    grid: {
      vertLines: { color: C.grid },
      horzLines: { color: C.grid },
    },
    rightPriceScale: { borderColor: C.grid },
    timeScale: { borderColor: C.grid },
  } as const
}

/** Create-on-mount / rebuild-on-data chart shell shared by the three panels.
 *  Datasets here are dozens to hundreds of points; a full rebuild per change
 *  is the candle chart's own precedent and is imperceptible at this size. */
function usePanel(
  build: (chart: IChartApi) => void,
  deps: unknown[],
  height: number,
  // v5 types tickMarkFormatter only at CREATION (TimeChartOptions), not on
  // timeScale().applyOptions — so DTE panels hand their formatter in here.
  extra?: { tickMarkFormatter?: (time: number) => string }
) {
  const box = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    if (!box.current) return
    const base = baseOptions(height)
    const chart = createChart(box.current, {
      ...base,
      autoSize: true,
      timeScale: { ...base.timeScale, ...(extra ?? {}) },
    })
    build(chart)
    chart.timeScale().fitContent()
    return () => chart.remove()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  return box
}

// ---------------------------------------------------------------------------

export interface HistPoint {
  date: string
  /** ALREADY IN THE DISPLAY UNIT — dollars, or percent of strike. The panel
   *  never divides anything: only the page knows the strike each point was
   *  actually matched at, and that is the only correct denominator for it. */
  value: number | null
}

/** The delta-matched history: what this trade's SHAPE has cost, day by day,
 *  against the underlying — and where today sits in that record.
 *
 *  THE UNDERLYING IS THE BASE TIMELINE. It trades every session; the archive
 *  has gaps (a day nobody quoted, a day the loader skipped). Setting the
 *  option series first made ITS dates the axis, so the underlying could only
 *  draw where an option happened to exist — and with a sparse series it
 *  effectively did not draw at all. Now the underlying defines the span and
 *  the option nodes overlay onto it, which is also the honest reading: the
 *  market ran continuously whether or not this shape was quoted.
 *
 *  MID ONLY, and no last-value chip on it. Bid and ask sat within pennies and
 *  read as one thick line; a third chip stacked under `today` and `normal`
 *  just competed with the two references that matter.
 *
 *  A NODE PER POINT, because every point is a DIFFERENT CONTRACT — the same
 *  delta and DTE at a different strike as the market moved. A continuous line
 *  alone implies one instrument being re-quoted, which this series is not.
 */
export function HistoryPanel({
  rows, under, underLabel, avg, avgLabel, refPrice = null, pct = false, height = 380,
  picked = null, onPick,
}: {
  rows: HistPoint[]
  under: { date: string; close: number }[]
  underLabel: string
  /** Rolling average of the option mid — "normal" as a line you can follow,
   *  over a window the user chooses. Empty draws nothing. */
  avg?: { date: string; value: number | null }[]
  avgLabel?: string
  /** Today's LIVE quote, in the SAME unit as `rows` — the level every
   *  historical point is read against. */
  refPrice?: number | null
  /** Percent rather than dollars — of strike, or annualised; the page has
   *  already done that division, and both format identically here. Drives the
   *  axis, the average and the today line from one place, so the scale can
   *  never disagree with the numbers printed on it. */
  pct?: boolean
  height?: number
  /** The node whose exit view is open, marked ON the line so the sub-chart
   *  below is visibly anchored to the point it explains. */
  picked?: string | null
  /** A NODE was clicked — every node is one archived contract, and clicking
   *  it asks "and what happened to that trade next". Fires with the node's
   *  date; the page owns what that means. */
  onPick?: (date: string) => void
}) {
  // The callback lives in a ref so a fresh closure per render does not sit in
  // the deps array tearing the chart down on every parent render — and the
  // MARKER is applied through refs for the same reason, one level worse: with
  // `picked` in the deps, the click that picks a node rebuilt the chart, and
  // the rebuild's fitContent() threw away the user's pan and zoom at the
  // exact moment they were looking at a point they had navigated to.
  const onPickRef = useRef(onPick)
  onPickRef.current = onPick
  const pickedRef = useRef(picked)
  pickedRef.current = picked
  const markersRef = useRef<{ setMarkers: (m: SeriesMarker<Time>[]) => void } | null>(null)
  const nodesRef = useRef<Set<string>>(new Set())
  const box = usePanel((chart) => {
    const t = (d: string) => (Date.parse(d + 'T00:00:00Z') / 1000) as UTCTimestamp

    // ---- the base timeline, and the scale it owns -------------------------
    if (under.length > 0) {
      const u = chart.addSeries(LineSeries, {
        color: C.under,
        lineWidth: 2,
        priceScaleId: 'left',
        title: underLabel,
        priceLineVisible: false,
        priceFormat: { type: 'price', precision: 0, minMove: 1 },
      })
      u.setData(under.map((b) => ({ time: t(b.date), value: b.close })))
      chart.priceScale('left').applyOptions({
        visible: true,
        borderColor: C.grid,
        scaleMargins: { top: 0.08, bottom: 0.08 },
      })
    }

    // The option, projected ONTO that timeline: every underlying day gets a
    // point, and days the archive never priced are whitespace — so the line
    // breaks over a gap instead of bridging one that was never observed.
    // ONE format for the option scale, its average and its today line.
    // Precision follows the data: a 328-DTE put costs ~5% of strike, a 5-DTE
    // wing ~0.04%, and a fixed 2dp would round the second to "0.04%" — three
    // distinguishable prices collapsing onto one label.
    const peak = Math.max(
      0,
      ...rows.map((r) => (typeof r.value === 'number' ? Math.abs(r.value) : 0)),
      refPrice !== null && Number.isFinite(refPrice) ? Math.abs(refPrice) : 0
    )
    const dp = !pct ? 2 : peak >= 10 ? 2 : peak >= 1 ? 3 : 4
    const optFmt = pct
      ? {
          type: 'custom' as const,
          formatter: (v: number) => `${v.toFixed(dp)}%`,
          minMove: Math.pow(10, -dp),
        }
      : { type: 'price' as const, precision: 2, minMove: 0.01 }
    const byDate = new Map(rows.map((r) => [r.date, r.value]))
    const spine = under.length > 0
      ? under.map((b) => b.date)
      : rows.map((r) => r.date)
    const mid = chart.addSeries(LineSeries, {
      color: C.accent,
      lineWidth: 2,
      // No title and no last-value chip: the orange line is the subject of the
      // whole panel, and its badge only crowded `today` and the average.
      priceLineVisible: false,
      lastValueVisible: false,
      pointMarkersVisible: true,
      pointMarkersRadius: 2,
      priceFormat: optFmt,
    })
    mid.setData(spine.map((d) => {
      const v = byDate.get(d)
      return v === undefined || v === null ? { time: t(d) } : { time: t(d), value: v }
    }))

    // ---- picking a node: click near it, get its exit view -------------------
    // The click lands on whatever SPINE date the crosshair snapped to, which
    // on a sparse series is almost never a node — so the handler finds the
    // nearest NODE by pixel distance instead, within a tolerance wide enough
    // to be clickable and narrow enough not to teleport across the chart.
    // (14px ≈ the draw engine's own 8px hit tolerance plus the node radius.)
    const nodeDates = rows.filter((r) => r.value !== null).map((r) => r.date)
    nodesRef.current = new Set(nodeDates)
    if (onPickRef.current !== undefined && nodeDates.length > 0) {
      chart.subscribeClick((param) => {
        if (!param.point) return
        let bestDate: string | null = null
        let bestDx = 14
        for (const d of nodeDates) {
          const x = chart.timeScale().timeToCoordinate(t(d))
          if (x === null) continue
          const dx = Math.abs(x - param.point.x)
          if (dx < bestDx) { bestDx = dx; bestDate = d }
        }
        if (bestDate !== null) onPickRef.current?.(bestDate)
      })
    }
    // The markers PRIMITIVE is created with the chart; which marker it shows
    // is applied outside the build (see the effect below), so picking a node
    // never rebuilds the chart under the click.
    try {
      markersRef.current = createSeriesMarkers(mid, markerFor(pickedRef.current, nodesRef.current))
    } catch { /* markers are decoration; the chart stands without them */ }

    if (avg && avg.length > 0) {
      const a = chart.addSeries(LineSeries, {
        color: C.text,
        lineWidth: 1,
        lineStyle: LineStyle.Dotted,
        title: avgLabel ?? 'normal',
        priceLineVisible: false,
        priceFormat: optFmt,
      })
      const avgBy = new Map(avg.map((r) => [r.date, r.value]))
      a.setData(spine.map((d) => {
        const v = avgBy.get(d)
        return v === undefined || v === null ? { time: t(d) } : { time: t(d), value: v }
      }))
    }

    // TODAY, as the one horizontal reference: the live mid you are being
    // offered right now, against everything the shape has ever cost.
    if (refPrice !== null && Number.isFinite(refPrice)) {
      mid.createPriceLine({
        price: refPrice,
        color: C.accent,
        lineWidth: 2,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: 'today',
      })
    }
  }, [rows, under, underLabel, avg, avgLabel, refPrice, pct, height], height)
  // The picked marker, applied WITHOUT a rebuild: setMarkers on the live
  // primitive. Rebuilds re-seed it from pickedRef inside build, so the two
  // paths cannot disagree about which node is marked.
  useEffect(() => {
    try {
      markersRef.current?.setMarkers(markerFor(picked, nodesRef.current))
    } catch { /* chart torn down mid-update; the rebuild will re-seed */ }
  }, [picked])
  return <div ref={box} style={{ height }} />
}

/** The entry marker for a picked node, or none — only a date that IS a node
 *  gets marked, so a stale pick cannot pin an arrow to empty whitespace. */
function markerFor(picked: string | null | undefined, nodes: Set<string>): SeriesMarker<Time>[] {
  if (!picked || !nodes.has(picked)) return []
  return [{
    time: (Date.parse(picked + 'T00:00:00Z') / 1000) as UTCTimestamp,
    position: 'belowBar',
    shape: 'arrowUp',
    color: C.up,
    text: 'entry',
  }]
}

// ---------------------------------------------------------------------------

/** The exit view of one picked node: what closing that trade has cost, day by
 *  day since entry, against the premium it opened at.
 *
 *  TWO AXES, TWO QUESTIONS, one timeline. The right axis is dollars: the mark
 *  (cost to close at mid) against a dashed line at the entry premium — cross
 *  below the dash and a short is winning. The left axis is P&L as % of the
 *  strike capital (capitalFor: the cash-secured buying-power effect, the
 *  heatmap's own denominator), with a dotted zero line — the same story
 *  normalised, so two picks at different strikes compare honestly.
 *
 *  Nodes on both lines because each day is one archived quote; gaps are
 *  one-sided days, drawn as gaps for the same reason every other chart here
 *  draws them that way. */
export function ExitPanel({
  points, premium, side, height = 240,
}: {
  points: ExitPoint[]
  /** Entry premium per share — credit received (short) or debit paid (long). */
  premium: number
  side: 'short' | 'long'
  height?: number
}) {
  const box = usePanel((chart) => {
    const t = (d: string) => (Date.parse(d + 'T00:00:00Z') / 1000) as UTCTimestamp

    // P&L IN DOLLARS PER CONTRACT, the left axis — the money, not a quote.
    // It was a percentage, and a percentage on a left axis beside dollars on
    // a right axis is precisely how a reader ends up quoting the green line
    // against the orange scale: 13.25% "read" as ~55 because that is what the
    // right-hand axis says at that height. The two axes now carry the SAME
    // unit ($), so mis-reading one against the other costs nothing. The
    // percentage still exists — in the caption, next to the capital it
    // divides by, where it cannot be confused for a price.
    const pnl = chart.addSeries(LineSeries, {
      color: C.up,
      lineWidth: 2,
      priceScaleId: 'left',
      title: 'P&L $',
      priceLineVisible: false,
      lastValueVisible: true,
      pointMarkersVisible: true,
      pointMarkersRadius: 2,
      priceFormat: {
        type: 'custom',
        formatter: (v: number) =>
          `${v >= 0 ? '+' : '-'}$${Math.abs(v).toLocaleString('en-US', {
            minimumFractionDigits: 0, maximumFractionDigits: 0 })}`,
        minMove: 1,
      },
    })
    pnl.setData(points.map((p) =>
      p.pnl === null ? { time: t(p.date) } : { time: t(p.date), value: p.pnl }))
    pnl.createPriceLine({
      price: 0,
      color: C.text,
      lineWidth: 1,
      lineStyle: LineStyle.Dotted,
      axisLabelVisible: false,
      title: '',
    })
    chart.priceScale('left').applyOptions({
      visible: true,
      borderColor: C.grid,
      scaleMargins: { top: 0.12, bottom: 0.12 },
    })

    // The mark, the right axis: the QUOTE, per share, the way a chain prints
    // it — deliberately not multiplied, because this line is what you would
    // pay to close and quotes are per-share everywhere else in the app. The
    // axis title says so, since the other axis is now dollars too.
    const mark = chart.addSeries(LineSeries, {
      color: C.accent,
      lineWidth: 2,
      title: side === 'short' ? 'buy-back (quote)' : 'sell (quote)',
      priceLineVisible: false,
      lastValueVisible: true,
      pointMarkersVisible: true,
      pointMarkersRadius: 2,
      priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
    })
    mark.setData(points.map((p) =>
      p.mark === null ? { time: t(p.date) } : { time: t(p.date), value: p.mark }))
    // The entry premium as the standing reference: the level the whole panel
    // is read against, labelled by which direction the money went.
    mark.createPriceLine({
      price: premium,
      color: C.accent,
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      axisLabelVisible: true,
      title: side === 'short' ? 'credit' : 'cost',
    })
  }, [points, premium, side, height], height)
  return <div ref={box} style={{ height }} />
}

// ---------------------------------------------------------------------------

export interface TermPoint {
  dte: number
  mid: number | null
  bid: number | null
  ask: number | null
  /** Annualised rate at this expiration — premium/strike scaled by sessions,
   *  the SAME arithmetic as the heatmap cells, so the two surfaces agree. */
  rate: number | null
}

/** The term structure, made comparable: premium on the right axis, the
 *  ANNUALISED rate on the left — the line that actually answers "is the far
 *  month better", instead of the obvious "longer costs more". */
export function TermPanel({
  points, markDte, height = 260,
}: {
  points: TermPoint[]
  markDte: number | null
  height?: number
}) {
  const box = usePanel((chart) => {
    const t = (dte: number) => ((BASE + dte * DAY)) as UTCTimestamp
    chart.applyOptions({
      localization: {
        timeFormatter: (time: number) =>
          `${Math.round((time - BASE) / DAY)} days to expiry`,
      },
    })
    const pts = [...points].sort((a, b) => a.dte - b.dte)

    const rate = chart.addSeries(LineSeries, {
      color: C.up,
      lineWidth: 2,
      priceScaleId: 'left',
      title: 'rate/yr',
      priceLineVisible: false,
      pointMarkersVisible: true,
      priceFormat: {
        type: 'custom',
        formatter: (v: number) => `${v.toFixed(0)}%`,
        minMove: 0.1,
      },
    })
    rate.setData(pts.map((p) =>
      p.rate === null ? { time: t(p.dte) } : { time: t(p.dte), value: p.rate * 100 }))
    chart.priceScale('left').applyOptions({ visible: true, borderColor: C.grid })

    const mk = (
      color: string, width: 1 | 2, dashed: boolean, title: string,
      val: (p: TermPoint) => number | null, markers: boolean
    ) => {
      const s = chart.addSeries(LineSeries, {
        color,
        lineWidth: width,
        lineStyle: dashed ? LineStyle.Dashed : LineStyle.Solid,
        title,
        priceLineVisible: false,
        pointMarkersVisible: markers,
      })
      s.setData(pts.map((p) => {
        const v = val(p)
        return v === null ? { time: t(p.dte) } : { time: t(p.dte), value: v }
      }))
      return s
    }
    mk(C.dim, 1, true, 'bid', (p) => (p.bid && p.bid > 0 ? p.bid : null), false)
    mk(C.dim, 1, true, 'ask', (p) => p.ask ?? null, false)
    const mid = mk(C.accent, 2, false, 'mid', (p) => p.mid, true)

    // The expiration you picked, marked ON the mid line — an arrow beats a
    // separate rule because it survives pan and zoom with its point.
    if (markDte !== null && pts.some((p) => p.dte === markDte && p.mid !== null)) {
      import('lightweight-charts').then(({ createSeriesMarkers }) => {
        try {
          createSeriesMarkers(mid, [{
            time: t(markDte),
            position: 'aboveBar',
            shape: 'arrowDown',
            color: C.accent,
            text: 'picked',
          }])
        } catch { /* markers are decoration; the chart stands without them */ }
      })
    }
  }, [points, markDte, height], height, {
    tickMarkFormatter: (time: number) => `${Math.round((time - BASE) / DAY)}d`,
  })
  return <div ref={box} style={{ height }} />
}
