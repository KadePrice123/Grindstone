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
  type IChartApi,
  type UTCTimestamp,
} from 'lightweight-charts'

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
  /** Percent-of-strike rather than dollars. Formats the axis, the average and
   *  the today line from one place, so the scale can never disagree with the
   *  numbers printed on it. */
  pct?: boolean
  height?: number
}) {
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
