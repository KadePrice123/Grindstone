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
  type SeriesType,
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

/** Editable periods for the parametrized indicators. The KEYS are fixed
 *  slots — 'sma20' means "the first SMA", not a promise of 20 — so the
 *  wheel/backend vocabulary (ind:sma20) survives a period edit unchanged. */
export interface IndicatorParams {
  sma20?: { period: number }
  sma50?: { period: number }
  ema20?: { period: number }
  rsi14?: { period: number }
}

export const DEFAULT_INDICATOR_PERIODS = { sma20: 20, sma50: 50, ema20: 20, rsi14: 14 } as const

/** Bad input never reaches the math: integer, 1..400, else the slot default. */
function periodOf(p: { period: number } | undefined, fallback: number): number {
  const v = Math.round(p?.period ?? fallback)
  return Number.isFinite(v) ? Math.min(400, Math.max(1, v)) : fallback
}

/** Handed to onReady after every series (re)build so a drawing layer
 *  (ChartDraw) can anchor projections to the live chart + series. */
export interface ChartReadyApi {
  chart: IChartApi
  mainSeries: ISeriesApi<SeriesType>
}

/** Comparison mode (charts.gs): one line per symbol instead of candles. */
export interface CompareLine {
  symbol: string
  bars: Bar[]
  color: string
  hidden?: boolean
}

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

// REVIEW 2026-08-02: destructuring defaults (`bars = []`) mint a NEW array
// identity per render, and both sit in the rebuild effect's deps — on the
// multi-chart page (which passes neither prop) every keystroke in the add
// box rebuilt every series and fitContent() threw away the user's zoom.
// Module constants keep the identity stable.
const NO_BARS: Bar[] = []
const NO_INDICATORS: IndicatorKey[] = []
const NO_PARAMS: IndicatorParams = {} // in the rebuild deps — same identity rule
const NO_FLAGS: string[] = []

export function Chart({
  bars = NO_BARS,
  indicators = NO_INDICATORS,
  indicatorParams = NO_PARAMS,
  height = 420,
  compact = false,
  onClick,
  lines,
  normalize = false,
  symbols,
  hiddenSymbols,
  onReady,
  timeframe,
  flags = NO_FLAGS,
  isolated,
  drawTool = 'pointer',
  drawCount = 0,
  measureCount = 0,
  selectedCount = 0,
  dofFree = null,
}: {
  bars?: Bar[]
  indicators?: IndicatorKey[]
  /** Periods for the parametrized indicators. In the rebuild-effect deps, so
   *  hosts must pass a stable object (state or module const), never a fresh
   *  inline literal per render. */
  indicatorParams?: IndicatorParams
  height?: number
  compact?: boolean
  onClick?: () => void
  /** Comparison mode: when set, bars is ignored and each entry draws a line. */
  lines?: CompareLine[]
  /** Lines mode only: rebase every line to % change from its first close. */
  normalize?: boolean
  /** Wheel-context truth for data-chart-symbols; defaults to lines' symbols. */
  symbols?: string[]
  hiddenSymbols?: string[]
  /** Fires after every series (re)build — series handles go stale each build,
   *  so a drawing layer must re-anchor here, not once. */
  onReady?: (api: ChartReadyApi) => void
  // ---- testability / wheel-context pass-throughs ------------------------
  // The PAGE owns all of this state (tool, counts come from the drawing
  // engine's onChange); this component only stamps it onto the container so
  // the wheel (data-chart-*) and the CDP e2e (data-draw-*) read live truth.
  timeframe?: string
  /** Active view flags, e.g. ['drawhidden','indhidden']. */
  flags?: string[]
  isolated?: string | null
  drawTool?: string
  drawCount?: number
  measureCount?: number
  selectedCount?: number
  /** Free coordinates left in the sketch; null when nothing is constrained. */
  dofFree?: number | null
}) {
  const box = useRef<HTMLDivElement>(null)
  const chart = useRef<IChartApi | null>(null)
  const price = useRef<ISeriesApi<'Candlestick'> | ISeriesApi<'Area'> | null>(null)
  const extras = useRef<ISeriesApi<'Line'>[]>([])
  const compare = useRef<ISeriesApi<'Line'>[]>([])
  const volume = useRef<ISeriesApi<'Histogram'> | null>(null)
  const rsiSeries = useRef<ISeriesApi<'Line'> | null>(null)
  // Ref, not effect dep: a host re-creating its callback must not force a
  // full series rebuild.
  const onReadyRef = useRef(onReady)
  onReadyRef.current = onReady

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
      compare.current = []
      volume.current = null
      rsiSeries.current = null
    }
  }, [compact])

  useEffect(() => {
    const c = chart.current
    if (!c) return

    // Rebuild series on every data/indicator change — simple and correct;
    // these datasets are hundreds of points, not millions.
    if (price.current) {
      c.removeSeries(price.current as ISeriesApi<'Candlestick'>)
      price.current = null
    }
    for (const s of extras.current) c.removeSeries(s)
    extras.current = []
    for (const s of compare.current) c.removeSeries(s)
    compare.current = []
    if (volume.current) {
      c.removeSeries(volume.current)
      volume.current = null
    }
    if (rsiSeries.current) {
      c.removeSeries(rsiSeries.current)
      rsiSeries.current = null
    }

    // Comparison mode: a line per visible symbol. Normalize rebases each to
    // % change from its FIRST LOADED close — the only way an $80 ETF and a
    // $500 one share an axis honestly — and the axis labels must say so
    // (custom % formatter) or the numbers read as prices.
    if (lines) {
      let main: ISeriesApi<'Line'> | null = null
      for (const l of lines) {
        if (l.hidden || l.bars.length === 0) continue
        const base = l.bars[0].close
        const s = c.addSeries(LineSeries, {
          color: l.color,
          lineWidth: 2,
          priceLineVisible: false,
          lastValueVisible: false,
          ...(normalize
            ? {
                priceFormat: {
                  type: 'custom' as const,
                  formatter: (p: number) => `${p >= 0 ? '+' : ''}${p.toFixed(2)}%`,
                  minMove: 0.01,
                },
              }
            : {}),
        })
        s.setData(
          l.bars.map((b) => ({
            time: toTime(b.ts),
            value: normalize && base !== 0 ? (b.close / base - 1) * 100 : b.close,
          }))
        )
        compare.current.push(s)
        if (!main) main = s // drawings anchor to the first visible symbol's scale
      }
      c.timeScale().fitContent()
      if (main) onReadyRef.current?.({ chart: c, mainSeries: main })
      return
    }

    if (bars.length === 0) return

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
      // Titles carry the LIVE period so an edited SMA never masquerades as 20.
      const pSma20 = periodOf(indicatorParams.sma20, DEFAULT_INDICATOR_PERIODS.sma20)
      const pSma50 = periodOf(indicatorParams.sma50, DEFAULT_INDICATOR_PERIODS.sma50)
      const pEma20 = periodOf(indicatorParams.ema20, DEFAULT_INDICATOR_PERIODS.ema20)
      if (indicators.includes('sma20')) addLine(sma(closes, pSma20), COLORS.sma20, `SMA ${pSma20}`)
      if (indicators.includes('sma50')) addLine(sma(closes, pSma50), COLORS.sma50, `SMA ${pSma50}`)
      if (indicators.includes('ema20')) addLine(ema(closes, pEma20), COLORS.ema20, `EMA ${pEma20}`)

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
        const pRsi = periodOf(indicatorParams.rsi14, DEFAULT_INDICATOR_PERIODS.rsi14)
        // v5 multi-pane: paneIndex puts RSI in its own pane below price.
        const r = c.addSeries(
          LineSeries,
          { color: COLORS.rsi, lineWidth: 1, priceLineVisible: false, title: `RSI ${pRsi}` },
          1
        )
        r.setData(
          rsi(closes, pRsi)
            .map((v, i) => (v == null ? null : { time: times[i], value: v }))
            .filter((d): d is { time: UTCTimestamp; value: number } => d !== null)
        )
        rsiSeries.current = r
      }
    }

    c.timeScale().fitContent()
    if (price.current) onReadyRef.current?.({ chart: c, mainSeries: price.current })
  }, [bars, indicators, indicatorParams, compact, lines, normalize])

  // Working charts declare themselves to the gesture wheel here — these
  // attrs are what wheelEvents.ts reads on right-click, so a chart on any
  // page (symbol, charts.gs) spawns the chart wheel with live state.
  //
  // COMPACT charts are previews, not workspaces (REVIEW 2026-08-02): the
  // search featured card has no drawing layer, no indicators and no
  // onChartAction listener, so a chart wheel spawned over it offered eight
  // tools that all silently did nothing. A preview right-click gets the
  // default wheel instead — worse-looking on paper, honest in the hand.
  const ctxSymbols = symbols ?? (lines ? lines.map((l) => l.symbol) : [])
  const ctxHidden =
    hiddenSymbols ?? (lines ? lines.filter((l) => l.hidden).map((l) => l.symbol) : [])
  return (
    <div
      ref={box}
      className={compact ? 'chart-box compact' : 'chart-box'}
      style={{ height }}
      onClick={onClick}
      data-wheel-context={compact ? undefined : 'chart'}
      data-chart-symbols={ctxSymbols.join(',')}
      data-chart-indicators={indicators.join(',')}
      data-chart-hidden={ctxHidden.join(',')}
      data-chart-timeframe={timeframe ?? ''}
      data-chart-flags={flags.join(',')}
      data-chart-isolated={isolated ?? ''}
      data-draw-tool={drawTool}
      data-draw-count={drawCount}
      data-measure-count={measureCount}
      data-draw-selected={selectedCount}
      data-draw-dof={dofFree ?? ''}
    />
  )
}
