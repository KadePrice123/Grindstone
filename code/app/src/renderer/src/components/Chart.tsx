/**
 * Price chart on TradingView Lightweight Charts v5 (Apache-2.0).
 *
 * v5 API note: series are created with addSeries(SeriesDefinition, options) —
 * v4's addCandlestickSeries() is gone, and most tutorials online are stale.
 * The TradingView attribution required by the license stays on via the
 * built-in attributionLogo option.
 *
 * Indicators are computed here from the same bars the chart draws, so what
 * you see and what the numbers say can never disagree.
 */
import {
  AreaSeries,
  CandlestickSeries,
  ColorType,
  HistogramSeries,
  LineSeries,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from 'lightweight-charts'
import { useEffect, useRef } from 'react'

export interface Bar {
  ts: string
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export type IndicatorKey = 'sma20' | 'sma50' | 'ema20' | 'vol' | 'rsi14'

const COLORS = {
  up: '#2EBD85',
  down: '#E5484D',
  grid: '#22262A',
  text: '#9AA0A6',
  bg: '#101214',
  accent: '#D98324',
  sma20: '#D98324',
  sma50: '#6BA4E8',
  ema20: '#C77DD6',
  rsi: '#D98324',
}

function toTime(ts: string): UTCTimestamp {
  return Math.floor(new Date(ts).getTime() / 1000) as UTCTimestamp
}

function sma(values: number[], period: number): (number | null)[] {
  const out: (number | null)[] = []
  let sum = 0
  for (let i = 0; i < values.length; i += 1) {
    sum += values[i]
    if (i >= period) sum -= values[i - period]
    out.push(i >= period - 1 ? sum / period : null)
  }
  return out
}

function ema(values: number[], period: number): (number | null)[] {
  const k = 2 / (period + 1)
  const out: (number | null)[] = []
  let prev: number | null = null
  for (let i = 0; i < values.length; i += 1) {
    if (i < period - 1) {
      out.push(null)
      continue
    }
    if (prev === null) {
      let s = 0
      for (let j = i - period + 1; j <= i; j += 1) s += values[j]
      prev = s / period
    } else {
      prev = values[i] * k + prev * (1 - k)
    }
    out.push(prev)
  }
  return out
}

/** Wilder's RSI — the standard smoothing, not a plain moving average. */
function rsi(values: number[], period = 14): (number | null)[] {
  const out: (number | null)[] = new Array(values.length).fill(null)
  if (values.length <= period) return out
  let gain = 0
  let loss = 0
  for (let i = 1; i <= period; i += 1) {
    const d = values[i] - values[i - 1]
    if (d >= 0) gain += d
    else loss -= d
  }
  gain /= period
  loss /= period
  out[period] = loss === 0 ? 100 : 100 - 100 / (1 + gain / loss)
  for (let i = period + 1; i < values.length; i += 1) {
    const d = values[i] - values[i - 1]
    gain = (gain * (period - 1) + Math.max(d, 0)) / period
    loss = (loss * (period - 1) + Math.max(-d, 0)) / period
    out[i] = loss === 0 ? 100 : 100 - 100 / (1 + gain / loss)
  }
  return out
}

export function Chart({
  bars,
  indicators = [],
  height = 420,
  compact = false,
  onClick,
}: {
  bars: Bar[]
  indicators?: IndicatorKey[]
  height?: number
  compact?: boolean
  onClick?: () => void
}) {
  const box = useRef<HTMLDivElement>(null)
  const chart = useRef<IChartApi | null>(null)
  const price = useRef<ISeriesApi<'Candlestick'> | ISeriesApi<'Area'> | null>(null)
  const extras = useRef<ISeriesApi<'Line'>[]>([])
  const volume = useRef<ISeriesApi<'Histogram'> | null>(null)
  const rsiSeries = useRef<ISeriesApi<'Line'> | null>(null)

  useEffect(() => {
    if (!box.current) return
    const c = createChart(box.current, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: COLORS.bg },
        textColor: COLORS.text,
        attributionLogo: true, // Apache-2.0 NOTICE requirement — keep it on
        fontSize: 11,
      },
      grid: {
        vertLines: { color: COLORS.grid, visible: !compact },
        horzLines: { color: COLORS.grid, visible: !compact },
      },
      rightPriceScale: { borderColor: COLORS.grid, visible: !compact },
      timeScale: { borderColor: COLORS.grid, visible: !compact, timeVisible: true },
      crosshair: { mode: compact ? 0 : 1 },
      handleScroll: !compact,
      handleScale: !compact,
    })
    chart.current = c
    return () => {
      c.remove()
      chart.current = null
      price.current = null
      extras.current = []
      volume.current = null
      rsiSeries.current = null
    }
  }, [compact])

  useEffect(() => {
    const c = chart.current
    if (!c || bars.length === 0) return

    // Rebuild series on every data/indicator change — simple and correct;
    // these datasets are hundreds of points, not millions.
    if (price.current) c.removeSeries(price.current as ISeriesApi<'Candlestick'>)
    for (const s of extras.current) c.removeSeries(s)
    extras.current = []
    if (volume.current) {
      c.removeSeries(volume.current)
      volume.current = null
    }
    if (rsiSeries.current) {
      c.removeSeries(rsiSeries.current)
      rsiSeries.current = null
    }

    const closes = bars.map((b) => b.close)
    const times = bars.map((b) => toTime(b.ts))

    if (compact) {
      const s = c.addSeries(AreaSeries, {
        lineColor: COLORS.accent,
        topColor: 'rgba(217,131,36,0.28)',
        bottomColor: 'rgba(217,131,36,0.02)',
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: false,
      })
      s.setData(bars.map((b, i) => ({ time: times[i], value: b.close })))
      price.current = s
    } else {
      const s = c.addSeries(CandlestickSeries, {
        upColor: COLORS.up,
        downColor: COLORS.down,
        borderUpColor: COLORS.up,
        borderDownColor: COLORS.down,
        wickUpColor: COLORS.up,
        wickDownColor: COLORS.down,
      })
      s.setData(
        bars.map((b, i) => ({
          time: times[i],
          open: b.open,
          high: b.high,
          low: b.low,
          close: b.close,
        }))
      )
      price.current = s

      const addLine = (values: (number | null)[], color: string, title: string) => {
        const line = c.addSeries(LineSeries, {
          color,
          lineWidth: 1,
          priceLineVisible: false,
          lastValueVisible: false,
          title,
        })
        line.setData(
          values
            .map((v, i) => (v == null ? null : { time: times[i], value: v }))
            .filter((d): d is { time: UTCTimestamp; value: number } => d !== null)
        )
        extras.current.push(line)
      }
      if (indicators.includes('sma20')) addLine(sma(closes, 20), COLORS.sma20, 'SMA 20')
      if (indicators.includes('sma50')) addLine(sma(closes, 50), COLORS.sma50, 'SMA 50')
      if (indicators.includes('ema20')) addLine(ema(closes, 20), COLORS.ema20, 'EMA 20')

      if (indicators.includes('vol')) {
        const v = c.addSeries(HistogramSeries, {
          priceFormat: { type: 'volume' },
          priceScaleId: 'vol',
        })
        v.setData(
          bars.map((b, i) => ({
            time: times[i],
            value: b.volume,
            color: b.close >= b.open ? 'rgba(46,189,133,0.45)' : 'rgba(229,72,77,0.45)',
          }))
        )
        c.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } })
        volume.current = v
      }

      if (indicators.includes('rsi14')) {
        // v5 multi-pane: paneIndex puts RSI in its own pane below price.
        const r = c.addSeries(
          LineSeries,
          { color: COLORS.rsi, lineWidth: 1, priceLineVisible: false, title: 'RSI 14' },
          1
        )
        r.setData(
          rsi(closes)
            .map((v, i) => (v == null ? null : { time: times[i], value: v }))
            .filter((d): d is { time: UTCTimestamp; value: number } => d !== null)
        )
        rsiSeries.current = r
      }
    }

    c.timeScale().fitContent()
  }, [bars, indicators, compact])

  return (
    <div
      ref={box}
      className={compact ? 'chart-box compact' : 'chart-box'}
      style={{ height }}
      onClick={onClick}
    />
  )
}
