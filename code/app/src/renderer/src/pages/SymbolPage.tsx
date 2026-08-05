/**
 * Single-symbol chart page. This page OWNS the chart-tool ux state (armed
 * tool, visibility flags, indicator periods); the ChartDraw engine owns the
 * drawings themselves and reports back through onChange, which is what
 * feeds the data-draw-* testability attrs and the floating DrawEditor.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, ApiError, SymbolSummary } from '../api'
import { makeChartStore } from '../chartStore'
import {
  Bar,
  Chart,
  ChartReadyApi,
  IndicatorKey,
  IndicatorParams,
} from '../components/Chart'
import { ChartDraw, DrawTool } from '../components/ChartDraw'
import { DrawEditor } from '../components/DrawEditor'
import { IndicatorSettings } from '../components/IndicatorSettings'
import { Metrics, barRange } from '../components/Metrics'
import { ChartMiniIcon } from '../components/icons'
import '../charts.css'
import '../charttools.css'

/** Engine report shape — single source for counts/selection (contract). */
type EngineState = ReturnType<ChartDraw['getState']>

const TIMEFRAMES: { key: string; label: string }[] = [
  { key: '1Min', label: '1m' },
  { key: '5Min', label: '5m' },
  { key: '15Min', label: '15m' },
  { key: '1Hour', label: '1H' },
  { key: '1Day', label: '1D' },
]

const INDICATORS: { key: IndicatorKey; label: string }[] = [
  { key: 'vol', label: 'Volume' },
  { key: 'sma20', label: 'SMA' },
  { key: 'sma50', label: 'SMA 2' },
  { key: 'ema20', label: 'EMA' },
  { key: 'rsi14', label: 'RSI' },
]

/* Toolbar groups mirror the wheel's chart vocabulary exactly — every wheel
   action has a button twin, so the no-mouse path reaches everything. */
const PLACE_TOOLS: { key: DrawTool; label: string; title: string }[] = [
  { key: 'pointer', label: 'Ptr', title: 'Pointer — click to select, drag to pan' },
  { key: 'trend', label: 'Line', title: 'Trend line — click two anchors' },
  { key: 'hline', label: 'H', title: 'Horizontal price line' },
  { key: 'vline', label: 'V', title: 'Vertical time line' },
  { key: 'circle', label: 'Circle', title: 'Circle — click center, then edge' },
]
/* There is no Select button: plain left-click in Pointer picks whatever is
   under it. A mode for selecting was a mode you always wanted on, and
   forgetting to arm it read as "the measurements are not clickable". */
const TRIM_TOOL: { key: DrawTool; label: string; title: string } = {
  key: 'trim',
  label: 'Trim',
  title: 'Trim a line back to an intersection',
}
const MEASURE_TOOLS: { key: DrawTool; label: string; title: string }[] = [
  { key: 'measure', label: 'Measure', title: 'Measure Δprice / Δbars between two anchors' },
  { key: 'inspect', label: 'Inspect', title: 'Inspect a candle — OHLC, body, volume' },
]

/** Tools that ARM a mode via engine.setTool; the rest are one-shot actions. */
const ARMABLE = [
  'pointer',
  'trend',
  'hline',
  'vline',
  'circle',
  'trim',
  'measure',
  'inspect',
] as const

const NO_IND: IndicatorKey[] = []
const DEFAULT_PARAMS: IndicatorParams = {}

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
  // Engine mirror: instance in state for rendering DrawEditor, report for
  // counts/selection. The ref twin serves the stable wheel handler.
  const [engine, setEngine] = useState<ChartDraw | null>(null)
  const [drawState, setDrawState] = useState<EngineState | null>(null)
  const [drawingsHidden, setDrawingsHidden] = useState(false)
  const [drawSaveErr, setDrawSaveErr] = useState<string | null>(null)
  const [indHidden, setIndHidden] = useState(false)
  const [showIndSettings, setShowIndSettings] = useState(false)
  // Session-scoped periods (IndicatorSettings notes this); stable identity —
  // it sits in the Chart's rebuild-effect deps.
  const [indicatorParams, setIndicatorParams] = useState<IndicatorParams>(DEFAULT_PARAMS)

  // Drawing engine plumbing. The Chart unmounts when bars empty on a
  // timeframe switch, so onReady fires against a NEW chart each time — the
  // refs let one stable callback re-anchor or rebuild as needed.
  const draw = useRef<ChartDraw | null>(null)
  // Built once: handleChartReady is a stable callback and the engine holds the
  // adapter for its whole life, so a new object per render would hand every
  // rebuilt engine a different store for no reason.
  const chartStore = useMemo(() => makeChartStore(setDrawSaveErr), [])
  const drawKeyRef = useRef(`${symbol}|1Day`)
  drawKeyRef.current = `${symbol}|${timeframe}`
  const toolRef = useRef(drawTool)
  toolRef.current = drawTool
  const barsRef = useRef<Bar[]>([])
  barsRef.current = bars
  const indicatorsRef = useRef(indicators)
  indicatorsRef.current = indicators
  const drawHiddenRef = useRef(false)
  // vis:ind stash — the EXACT indicator array parks here while hidden and is
  // restored identically on re-toggle. Lives outside setState updaters:
  // StrictMode double-invokes updaters, and a stash mutated inside one
  // toggles twice (net nothing).
  const indStash = useRef<IndicatorKey[] | null>(null)

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
      // No limit param: the backend applies the user's chart_candles setting
      // (default = ALL history the source has for this ticker).
      const b = await api<{ bars: Bar[]; source: string; reason?: string }>(
        'GET',
        `/api/symbols/${encodeURIComponent(symbol)}/bars?timeframe=${timeframe}`
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

  const toggle = useCallback((k: IndicatorKey) => {
    // Editing the set while 'indicators hidden' EXITS hidden mode and applies
    // the edit to the empty set the user is looking at. The wheel's ○ marker
    // promised "turn this on" against what's visible — restoring the stash
    // underneath would flip the meaning of the click.
    if (indStash.current !== null) {
      indStash.current = null
      setIndHidden(false)
    }
    setIndicators((cur) => (cur.includes(k) ? cur.filter((x) => x !== k) : [...cur, k]))
  }, [])

  // vis:ind round-trip: stash current set -> [], restore EXACTLY on re-toggle.
  const toggleIndVis = useCallback(() => {
    if (indStash.current === null) {
      indStash.current = indicatorsRef.current
      setIndHidden(true)
      setIndicators(NO_IND)
    } else {
      const back = indStash.current
      indStash.current = null
      setIndHidden(false)
      setIndicators(back)
    }
  }, [])

  const toggleDrawVis = useCallback(() => {
    const next = !drawHiddenRef.current
    drawHiddenRef.current = next
    setDrawingsHidden(next)
    draw.current?.setDrawingsHidden(next)
  }, [])

  // Delete: with a live selection it IS "delete the selection"; with nothing
  // selected it arms click-to-delete. One button, two honest readings — the
  // wheel's 'delete' action routes through the same choice.
  const deleteAction = useCallback(() => {
    const s = draw.current?.getState()
    // See ChartsPage: `selected` covers measures and pins too, `selection`
    // is drawings only.
    if (s && s.selected.length > 0) draw.current?.deleteSelected()
    else setDrawTool('delete')
  }, [])

  const handleChartReady = useCallback(({ chart, mainSeries }: ChartReadyApi) => {
    if (draw.current && draw.current.chart === chart) {
      draw.current.setKey(drawKeyRef.current)
      draw.current.setSeries(mainSeries)
      // setKey may have switched buckets — re-read counts for the attrs.
      setDrawState(draw.current.getState())
      return
    }
    draw.current?.destroy()
    // A GETTER, not a snapshot: the engine reads bars for measure/inspect at
    // interaction time, and a bars refetch on this page must never leave it
    // reading a stale array (the constructor-only-snapshot trap the build
    // flagged — closed by handing it the ref).
    const d = new ChartDraw(drawKeyRef.current, chart, mainSeries, {
      bars: () => barsRef.current,
      store: chartStore,
    })
    d.setTool(toolRef.current)
    d.setDrawingsHidden(drawHiddenRef.current)
    d.onChange((s: EngineState) => {
      setDrawState(s)
      // See ChartsPage: Escape's last rung disarms the engine, and the page
      // owns the lit toolbar button — mirror the tool back or it goes dead.
      setDrawTool(s.tool)
    })
    draw.current = d
    setEngine(d)
    setDrawState(d.getState())
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
  // page's chart — the FULL v3 vocabulary. 'normalize'/'isolate'/'add'/
  // 'hide' are charts.gs vocabulary: a single-symbol chart has nothing to
  // rebase against, solo, or add — deliberate no-ops.
  useEffect(() => {
    const off = window.grindstone.onChartAction(({ tool }) => {
      if ((ARMABLE as readonly string[]).includes(tool)) setDrawTool(tool as DrawTool)
      else if (tool === 'delete') deleteAction()
      else if (tool === 'clear') draw.current?.clearDrawings()
      else if (tool === 'clearmeasure') draw.current?.clearMeasures()
      else if (tool === 'vis:draw') toggleDrawVis()
      else if (tool === 'vis:ind') toggleIndVis()
      else if (tool === 'settings') setShowIndSettings(true)
      else if (tool.startsWith('tf:')) {
        const k = tool.slice(3)
        if (TIMEFRAMES.some((t) => t.key === k)) setTimeframe(k)
      } else if (tool.startsWith('ind:')) {
        const k = tool.slice(4)
        if (INDICATORS.some((i) => i.key === k)) toggle(k as IndicatorKey)
      }
    })
    return off
  }, [toggle, toggleIndVis, toggleDrawVis, deleteAction])

  const q = data?.quote
  const flags = [
    ...(drawingsHidden ? ['drawhidden'] : []),
    ...(indHidden ? ['indhidden'] : []),
  ]
  const selection = drawState?.selection ?? []
  /** See ChartsPage: every selected object, measures and pins included. */
  const selectedCount = drawState?.selected.length ?? 0

  const toolBtn = (t: { key: DrawTool; label: string; title: string }) => (
    <button
      key={t.key}
      className={`seg-btn${drawTool === t.key ? ' on' : ''}`}
      title={t.title}
      onClick={() => setDrawTool(t.key)}
    >
      {t.label}
    </button>
  )

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
          <div className="tgrp">
            <span className="tgrp-label">Time</span>
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
          </div>
          <div className="tgrp">
            <span className="tgrp-label">Indicators</span>
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
              <span className="seg-sep" />
              <button
                className={`seg-btn${showIndSettings ? ' on' : ''}`}
                title="Indicator settings — edit periods"
                onClick={() => setShowIndSettings((v) => !v)}
              >
                ⚙
              </button>
            </div>
          </div>
          <div className="tgrp">
            <span className="tgrp-label">Draw</span>
            <div className="seg">
              {PLACE_TOOLS.map(toolBtn)}
              <span className="seg-sep" />
              <button
                className={`seg-btn${drawTool === 'delete' ? ' on' : ''}`}
                title="Delete — removes the selection, or arms click-to-delete"
                onClick={deleteAction}
              >
                Del
              </button>
              {toolBtn(TRIM_TOOL)}
              <span className="seg-sep" />
              <button
                className="seg-btn"
                title="Clear every drawing on this timeframe"
                onClick={() => draw.current?.clearDrawings()}
              >
                Clear
              </button>
            </div>
          </div>
          <div className="tgrp">
            <span className="tgrp-label">Measure</span>
            <div className="seg">
              {MEASURE_TOOLS.map(toolBtn)}
              <button
                className="seg-btn"
                title="Clear all measurements"
                onClick={() => draw.current?.clearMeasures()}
              >
                Clear M
              </button>
            </div>
          </div>
          <div className="tgrp">
            <span className="tgrp-label">View</span>
            <div className="seg">
              <button
                className={`seg-btn${drawingsHidden ? ' vis-off' : ''}`}
                title={drawingsHidden ? 'Show drawings' : 'Hide drawings (kept, just hidden)'}
                onClick={toggleDrawVis}
              >
                Drawings
              </button>
              <button
                className={`seg-btn${indHidden ? ' vis-off' : ''}`}
                title={indHidden ? 'Restore indicators' : 'Hide all indicators (set is remembered)'}
                onClick={toggleIndVis}
              >
                Indicators
              </button>
            </div>
          </div>
          <span className="subtle chart-source">
            {bars.length > 0 ? `${bars.length} bars · ${barSource}` : barNote || 'loading…'}
          </span>
          {/* Drawings are saved in the background, so a failure has no other
              way to reach the user — and "my lines vanished on restart" is
              the complaint it prevents. */}
          {drawSaveErr ? (
            <span className="subtle draw-save-err" data-draw-save-err="1">
              drawings not saved: {drawSaveErr}
            </span>
          ) : null}
          {/* A refusal the engine could not carry out — a locked drawing that
              will not drag, a constraint that adds nothing. Silence here would
              make the gesture look broken. */}
          {drawState?.issue ? (
            <span className="subtle draw-issue" data-draw-issue={drawState.issue.code}>
              {drawState.issue.message}
            </span>
          ) : null}
          {drawState && drawState.dof.total > 0 ? (
            <span
              className={`subtle draw-dof${drawState.dof.free === 0 ? ' draw-dof-fixed' : ''}`}
              title="Free coordinates left in this sketch. 0 = fully defined: nothing can shift under you."
            >
              {drawState.dof.free === 0
                ? 'fully defined'
                : `${drawState.dof.free} free`}
            </span>
          ) : null}
        </div>
        <div className="chart-stage">
          {bars.length > 0 ? (
            <Chart
              bars={bars}
              indicators={indicators}
              indicatorParams={indicatorParams}
              height={indicators.includes('rsi14') ? 480 : 400}
              onReady={handleChartReady}
              symbols={[symbol]}
              timeframe={timeframe}
              flags={flags}
              drawTool={drawTool}
              drawCount={drawState?.drawings ?? 0}
              measureCount={drawState?.measures ?? 0}
              selectedCount={selectedCount}
              dofFree={drawState?.dof.free ?? null}
            />
          ) : (
            <div className="chart-empty dim">{barNote || 'No bars for this timeframe.'}</div>
          )}
          {/* Selection editor appears the moment something is selected and
              leaves with the selection — gated on bars so it can't float
              over an empty box driving a disposed engine. */}
          {engine && selection.length > 0 && bars.length > 0 ? (
            <div className="float-panel draw-editor-float">
              <DrawEditor engine={engine} selection={selection} />
            </div>
          ) : null}
          {showIndSettings ? (
            <div className="float-panel indset-float">
              <IndicatorSettings
                params={indicatorParams}
                onApply={setIndicatorParams}
                onClose={() => setShowIndSettings(false)}
              />
            </div>
          ) : null}
        </div>
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
