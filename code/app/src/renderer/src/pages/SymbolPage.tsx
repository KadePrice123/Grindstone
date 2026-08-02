import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, SymbolSummary } from '../api'
import { Bar, Chart, ChartReadyApi, IndicatorKey } from '../components/Chart'
import { ChartDraw, DrawTool } from '../components/ChartDraw'
import { Metrics, barRange } from '../components/Metrics'
import { ChartMiniIcon } from '../components/icons'
import '../charts.css'

const TIMEFRAMES: { key: string; label: string }[] = [
  { key: '1Min', label: '1m' },
  { key: '5Min', label: '5m' },
  { key: '15Min', label: '15m' },
  { key: '1Hour', label: '1H' },
  { key: '1Day', label: '1D' },
]

const INDICATORS: { key: IndicatorKey; label: string }[] = [
  { key: 'vol', label: 'Volume' },
  { key: 'sma20', label: 'SMA 20' },
  { key: 'sma50', label: 'SMA 50' },
  { key: 'ema20', label: 'EMA 20' },
  { key: 'rsi14', label: 'RSI 14' },
]

const DRAW_TOOLS: { key: DrawTool; label: string }[] = [
  { key: 'pointer', label: 'Pointer' },
  { key: 'trend', label: 'Trend' },
  { key: 'hline', label: 'H-line' },
]

function age(iso: string): string {
  const m = Math.floor((Date.now() - new Date(iso).getTime()) / 60000)
  if (m < 60) return `${Math.max(m, 0)}m ago`
  const h = Math.floor(m / 60)
  return h < 48 ? `${h}h ago` : `${Math.floor(h / 24)}d ago`
}

export function SymbolPage({
  symbol,
  onNavigate,
}: {
  symbol: string
  onNavigate: (r: { name: 'article'; id?: number; url?: string }) => void
}) {
  const [data, setData] = useState<SymbolSummary | null>(null)
  const [bars, setBars] = useState<Bar[]>([])
  const [barSource, setBarSource] = useState<string>('')
  const [barNote, setBarNote] = useState<string>('')
  const [timeframe, setTimeframe] = useState('1Day')
  const [indicators, setIndicators] = useState<IndicatorKey[]>(['vol'])
  const [yearBars, setYearBars] = useState<Bar[]>([])
  const [error, setError] = useState<string | null>(null)
  const [drawTool, setDrawTool] = useState<DrawTool>('pointer')

  // Drawing engine plumbing. The Chart unmounts when bars empty on a
  // timeframe switch, so onReady fires against a NEW chart each time — the
  // refs let one stable callback re-anchor or rebuild as needed.
  const draw = useRef<ChartDraw | null>(null)
  const drawKeyRef = useRef(`${symbol}|1Day`)
  drawKeyRef.current = `${symbol}|${timeframe}`
  const toolRef = useRef(drawTool)
  toolRef.current = drawTool

  useEffect(() => {
    let stop = false
    setData(null)
    setError(null)
    const load = async () => {
      try {
        const d = await api<SymbolSummary>(
          'GET',
          `/api/symbols/${encodeURIComponent(symbol)}/summary`
        )
        if (!stop) {
          setData(d)
          setError(null)
        }
      } catch (e) {
        if (!stop) setError(e instanceof ApiError ? e.message : String(e))
      }
    }
    load()
    const t = window.setInterval(load, 30_000)
    return () => {
      stop = true
      window.clearInterval(t)
    }
  }, [symbol])

  // REVIEW 2026-08-02: no guard here meant out-of-order responses painted
  // the WRONG bars — click 5m (slow cold fetch) then 1D (fast/cached) and
  // the stale 5Min answer landed last, under a highlighted 1D button; a
  // trend line drawn then stored 5Min anchors in the 1Day drawing bucket,
  // permanently invisible. Only the newest request may write.
  const barsSeq = useRef(0)
  const loadBars = useCallback(async () => {
    const mine = ++barsSeq.current
    try {
      const b = await api<{ bars: Bar[]; source: string; reason?: string }>(
        'GET',
        `/api/symbols/${encodeURIComponent(symbol)}/bars?timeframe=${timeframe}&limit=600`
      )
      if (mine !== barsSeq.current) return
      setBars(b.bars)
      setBarSource(b.source)
      setBarNote(b.reason ?? '')
    } catch (e) {
      if (mine !== barsSeq.current) return
      setBars([])
      setBarNote(e instanceof ApiError ? e.message : String(e))
    }
  }, [symbol, timeframe])

  useEffect(() => {
    setBars([])
    loadBars()
  }, [loadBars])

  // The 52-week range is a property of the instrument, not of the timeframe
  // you happen to be looking at — so it is its own daily series, fetched once
  // per symbol rather than recomputed every time you switch to 5m.
  useEffect(() => {
    let stop = false
    setYearBars([])
    api<{ bars: Bar[] }>('GET', `/api/symbols/${encodeURIComponent(symbol)}/bars?timeframe=1Day&limit=260`)
      .then((b) => !stop && setYearBars(b.bars))
      .catch(() => undefined) // the range is a nicety; its absence is not an error
    return () => {
      stop = true
    }
  }, [symbol])

  const toggle = useCallback(
    (k: IndicatorKey) =>
      setIndicators((cur) => (cur.includes(k) ? cur.filter((x) => x !== k) : [...cur, k])),
    []
  )

  const handleChartReady = useCallback(({ chart, mainSeries }: ChartReadyApi) => {
    if (draw.current && draw.current.chart === chart) {
      draw.current.setKey(drawKeyRef.current)
      draw.current.setSeries(mainSeries)
      return
    }
    draw.current?.destroy()
    const d = new ChartDraw(drawKeyRef.current, chart, mainSeries)
    d.setTool(toolRef.current)
    draw.current = d
  }, [])

  useEffect(() => {
    draw.current?.setTool(drawTool)
  }, [drawTool])

  useEffect(
    () => () => {
      draw.current?.destroy()
      draw.current = null
    },
    []
  )

  // Chart-wheel segments land here when the wheel was spawned over this
  // page's chart. 'add'/'hide'/'normalize' belong to charts.gs — ignored.
  useEffect(() => {
    const off = window.grindstone.onChartAction(({ tool }) => {
      if (tool === 'pointer' || tool === 'trend' || tool === 'hline') setDrawTool(tool)
      else if (tool === 'clear') draw.current?.clear()
      else if (tool.startsWith('ind:')) {
        const k = tool.slice(4)
        if (INDICATORS.some((i) => i.key === k)) toggle(k as IndicatorKey)
      }
    })
    return off
  }, [toggle])

  const q = data?.quote

  return (
    <div className="page wide">
      <div className="page-head">
        <ChartMiniIcon />
        <h1>{symbol}</h1>
        <span className="dim">{data?.name}</span>
      </div>

      {error ? <div className="test-result bad">{error}</div> : null}

      <div className="card">
        <Metrics
          quote={q}
          range={barRange(yearBars, yearBars.length >= 200 ? '52-week range' : `${yearBars.length}-day range`)}
        />
      </div>

      <div className="card chart-card">
        <div className="chart-toolbar">
          <div className="seg">
            {TIMEFRAMES.map((t) => (
              <button
                key={t.key}
                className={`seg-btn${timeframe === t.key ? ' on' : ''}`}
                onClick={() => setTimeframe(t.key)}
              >
                {t.label}
              </button>
            ))}
          </div>
          <div className="seg">
            {INDICATORS.map((i) => (
              <button
                key={i.key}
                className={`seg-btn${indicators.includes(i.key) ? ' on' : ''}`}
                onClick={() => toggle(i.key)}
              >
                {i.label}
              </button>
            ))}
          </div>
          {/* Toolbar mirrors the wheel's drawing tools on purpose — the
              no-mouse path must reach everything the wheel reaches. */}
          <div className="seg">
            {DRAW_TOOLS.map((t) => (
              <button
                key={t.key}
                className={`seg-btn${drawTool === t.key ? ' on' : ''}`}
                onClick={() => setDrawTool(t.key)}
              >
                {t.label}
              </button>
            ))}
            <button className="seg-btn" onClick={() => draw.current?.clear()}>
              Clear
            </button>
          </div>
          <span className="subtle chart-source">
            {bars.length > 0 ? `${bars.length} bars · ${barSource}` : barNote || 'loading…'}
          </span>
        </div>
        {bars.length > 0 ? (
          <Chart
            bars={bars}
            indicators={indicators}
            height={indicators.includes('rsi14') ? 480 : 400}
            onReady={handleChartReady}
            symbols={[symbol]}
          />
        ) : (
          <div className="chart-empty dim">{barNote || 'No bars for this timeframe.'}</div>
        )}
      </div>

      <div className="card">
        <h2>News</h2>
        {data == null ? (
          <div className="dim">Loading…</div>
        ) : data.news.length === 0 ? (
          <div className="dim">Nothing recent for {symbol} in the local store.</div>
        ) : (
          data.news.map((n) => (
            <div
              className="news-row"
              key={n.id}
              onClick={() => onNavigate({ name: 'article', id: n.id })}
              title="Read the article"
            >
              <div className="news-head">{n.headline}</div>
              <div className="subtle">
                {n.source} · {age(n.created_at)} · {n.symbols.slice(0, 5).join(' ')}
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  )
}
