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
 *  BANDS. The library has no band series; the fill between two percentiles is
 *  painted with the AREA-MASK trick — an area down from the upper envelope,
 *  then an area in the BACKGROUND colour down from the lower envelope to mask
 *  everything beneath it. Two passes give the 10-90 and 25-75 fills. The cost
 *  is that grid lines vanish inside a band, which is visually fine: the band
 *  IS the reference there.
 */
import { useEffect, useRef } from 'react'
import {
  AreaSeries,
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
  mid: number | null
  bid: number | null
  ask: number | null
}

/** The contract's archived daily prices; the underlying rides the LEFT scale
 *  so both series keep honest units on one calendar. */
export function HistoryPanel({
  rows, under, underLabel, refPrice = null, height = 240,
}: {
  rows: HistPoint[]
  under: { date: string; close: number }[]
  underLabel: string
  /** Today's LIVE mid for the selected contract — the horizontal reference
   *  every historical point is read against. Null (no live chain) draws
   *  nothing rather than a line at a made-up level. */
  refPrice?: number | null
  height?: number
}) {
  const box = usePanel((chart) => {
    const t = (d: string) => (Date.parse(d + 'T00:00:00Z') / 1000) as UTCTimestamp
    if (under.length > 0) {
      const s = chart.addSeries(LineSeries, {
        color: C.dim,
        lineWidth: 1,
        priceScaleId: 'left',
        title: underLabel,
        priceLineVisible: false,
        crosshairMarkerVisible: true,
      })
      s.setData(under.map((b) => ({ time: t(b.date), value: b.close })))
      chart.priceScale('left').applyOptions({ visible: true, borderColor: C.grid })
    }
    const mk = (
      color: string, width: 1 | 2, dashed: boolean, title: string,
      val: (r: HistPoint) => number | null
    ) => {
      const s = chart.addSeries(LineSeries, {
        color,
        lineWidth: width,
        lineStyle: dashed ? LineStyle.Dashed : LineStyle.Solid,
        title,
        priceLineVisible: false,
      })
      // A day with no two-sided market is a WHITESPACE point — the line
      // breaks there instead of inventing a straight bridge over a gap.
      s.setData(rows.map((r) => {
        const v = val(r)
        return v === null ? { time: t(r.date) } : { time: t(r.date), value: v }
      }))
      return s
    }
    mk(C.up, 1, true, 'bid', (r) => (r.bid && r.bid > 0 ? r.bid : null))
    mk(C.down, 1, true, 'ask', (r) => r.ask ?? null)
    const mid = mk(C.accent, 2, false, 'mid', (r) => r.mid)
    // WHERE TODAY SITS. A dashed rule at the live mid, so every historical
    // point is read as above-or-below what the same shape costs right now.
    if (refPrice !== null && Number.isFinite(refPrice)) {
      mid.createPriceLine({
        price: refPrice,
        color: C.accent,
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: 'today',
      })
    }
  }, [rows, under, underLabel, refPrice, height], height)
  return <div ref={box} style={{ height }} />
}

// ---------------------------------------------------------------------------

export interface FanData {
  band: { dte: number; p10: number; p25: number; p50: number; p75: number; p90: number }[]
  path: { dte: number; spread: number | null }[]
  cap: number
}

/** The fan chart: spread percentile bands of similar contracts, this
 *  contract's own life on top, x counting DOWN to expiry at the right. */
export function FanPanel({ data, height = 240 }: { data: FanData; height?: number }) {
  const box = usePanel((chart) => {
    const { band, path, cap } = data
    // dte -> slot: cap-dte, so dte=cap is the left edge and 0d the right.
    const t = (dte: number) => ((BASE + (cap - dte) * DAY)) as UTCTimestamp
    chart.applyOptions({
      localization: {
        timeFormatter: (time: number) =>
          `${Math.round(cap - (time - BASE) / DAY)} days to expiry`,
      },
    })

    const rows = band.filter((b) => b.dte <= cap).sort((a, b) => b.dte - a.dte)
    const area = (fill: string, val: (b: FanData['band'][number]) => number) => {
      const s = chart.addSeries(AreaSeries, {
        topColor: fill,
        bottomColor: fill,
        lineColor: 'rgba(0,0,0,0)',
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      })
      s.setData(rows.map((b) => ({ time: t(b.dte), value: val(b) })))
    }
    if (rows.length > 0) {
      // Paint order IS the stacking: wide fill, mask, inner fill, mask.
      area(C.band, (b) => b.p90)
      area(C.bg, (b) => b.p10)
      area(C.bandInner, (b) => b.p75)
      area(C.bg, (b) => b.p25)
      const med = chart.addSeries(LineSeries, {
        color: C.dim,
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        title: 'median',
        priceLineVisible: false,
        lastValueVisible: false,
      })
      med.setData(rows.map((b) => ({ time: t(b.dte), value: b.p50 })))
    }
    if (path.length > 0) {
      const line = chart.addSeries(LineSeries, {
        color: C.accent,
        lineWidth: 2,
        title: 'this contract',
        priceLineVisible: false,
        pointMarkersVisible: path.length <= 120,
      })
      line.setData(
        [...path]
          .sort((a, b) => b.dte - a.dte)
          .map((p) =>
            p.spread === null
              ? { time: t(p.dte) }
              : { time: t(p.dte), value: p.spread })
      )
    }
  }, [data, height], height, {
    tickMarkFormatter: (time: number) =>
      `${Math.round(data.cap - (time - BASE) / DAY)}d`,
  })
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
