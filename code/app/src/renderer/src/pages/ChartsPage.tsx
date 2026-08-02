/**
 * charts.gs — the multi-symbol comparison chart. Several tickers as line
 * series on one chart; add/remove/show/hide per symbol; normalize rebases
 * each line to % change from its first loaded close. State {symbols,
 * normalize, timeframe, hidden} persists through the settings door
 * (values.multi_chart) with a 500ms debounce so a burst of edits is one PUT.
 *
 * Drawings anchor to the FIRST VISIBLE symbol's series — its scale defines
 * {time, price} for everything drawn here (in % mode "price" is a percent).
 * Hide that symbol and drawings re-project onto the next one's scale.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, ApiError } from '../api'
import { Bar, Chart, ChartReadyApi, CompareLine } from '../components/Chart'
import { ChartDraw, DrawTool } from '../components/ChartDraw'
import { ChartMiniIcon } from '../components/icons'
import '../charts.css'

interface MultiChartState {
  symbols: string[]
  normalize: boolean
  timeframe: string
  hidden: string[]
}

const TIMEFRAMES: { key: string; label: string }[] = [
  { key: '1Min', label: '1m' },
  { key: '5Min', label: '5m' },
  { key: '15Min', label: '15m' },
  { key: '1Hour', label: '1H' },
  { key: '1Day', label: '1D' },
]

const DRAW_TOOLS: { key: DrawTool; label: string }[] = [
  { key: 'pointer', label: 'Pointer' },
  { key: 'trend', label: 'Trend' },
  { key: 'hline', label: 'H-line' },
]

/** Eight distinguishable series colors on the dark surface. Assignment is by
 *  slot (index in the symbols list), so colors shift when an earlier symbol
 *  is removed — stable identity would cost a persisted map for no real gain. */
const PALETTE = [
  '#d98324',
  '#6ba4e8',
  '#2ebd85',
  '#c77dd6',
  '#e5484d',
  '#e8c55b',
  '#5bc8c8',
  '#9aa0a6',
]

const DEFAULT_STATE: MultiChartState = {
  symbols: ['SPY'],
  normalize: true,
  timeframe: '1Day',
  hidden: [],
}

/** The settings blob is user data from a generic json door — trust nothing. */
function sane(v: unknown): MultiChartState {
  const o = (v ?? {}) as Partial<MultiChartState>
  const symbols = Array.isArray(o.symbols)
    ? o.symbols
        .filter((s): s is string => typeof s === 'string')
        .map((s) => s.toUpperCase())
        .slice(0, 12)
    : DEFAULT_STATE.symbols
  const hidden = Array.isArray(o.hidden)
    ? o.hidden
        .filter((s): s is string => typeof s === 'string')
        .map((s) => s.toUpperCase())
        .filter((s) => symbols.includes(s))
    : []
  return {
    symbols,
    normalize: typeof o.normalize === 'boolean' ? o.normalize : DEFAULT_STATE.normalize,
    timeframe:
      typeof o.timeframe === 'string' && TIMEFRAMES.some((t) => t.key === o.timeframe)
        ? o.timeframe
        : DEFAULT_STATE.timeframe,
    hidden,
  }
}

function EyeIcon({ off }: { off: boolean }) {
  return (
    <svg
      viewBox="0 0 24 24"
      width="13"
      height="13"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6S2 12 2 12z" />
      <circle cx="12" cy="12" r="2.6" />
      {off ? <line x1="4" y1="20" x2="20" y2="4" /> : null}
    </svg>
  )
}

export function ChartsPage() {
  const [st, setSt] = useState<MultiChartState | null>(null) // null until hydrated
  const [data, setData] = useState<Record<string, { bars: Bar[]; error?: string }>>({})
  const [draft, setDraft] = useState('')
  const [tool, setTool] = useState<DrawTool>('pointer')
  const [loadErr, setLoadErr] = useState<string | null>(null)
  const [saveErr, setSaveErr] = useState<string | null>(null)

  const dirty = useRef(false)
  const draw = useRef<ChartDraw | null>(null)
  const toolRef = useRef(tool)
  toolRef.current = tool

  const symbols = st?.symbols
  const hiddenList = st?.hidden
  const timeframe = st?.timeframe

  // REVIEW 2026-08-02: normalize is IN the key. It swaps the whole price
  // scale between dollars and percent; one shared bucket meant $560 anchors
  // projected thousands of pixels off a ±10% axis (drawings "vanished") and
  // the bucket became a permanent dollar/percent mix. Same rule as the
  // timeframe: a key per scale semantics.
  const drawKeyRef = useRef('multi|1Day|%')
  drawKeyRef.current = `multi|${timeframe ?? '1Day'}|${st?.normalize ? '%' : '$'}`

  // ---- hydrate from settings --------------------------------------------
  useEffect(() => {
    let stop = false
    api<{ values: Record<string, unknown> }>('GET', '/api/settings')
      .then((r) => {
        if (!stop) setSt(sane(r.values.multi_chart))
      })
      .catch((e) => {
        if (!stop) {
          // Start usable anyway, but say the saved layout could not load.
          setLoadErr(e instanceof ApiError ? e.message : String(e))
          setSt(DEFAULT_STATE)
        }
      })
    return () => {
      stop = true
    }
  }, [])

  // ---- persist after edits only (dirty flag) -----------------------------
  // The hydrating GET must not echo itself back as a PUT.
  const stRef = useRef(st)
  stRef.current = st
  useEffect(() => {
    if (st === null || !dirty.current) return
    const t = window.setTimeout(() => {
      dirty.current = false
      api('PUT', '/api/settings', { multi_chart: st })
        .then(() => setSaveErr(null))
        .catch((e) => setSaveErr(e instanceof ApiError ? e.message : String(e)))
    }, 500)
    return () => window.clearTimeout(t)
  }, [st])
  // REVIEW 2026-08-02: the debounce cleanup CANCELLED the PUT — edit, then
  // navigate away inside 500ms, and the change silently never persisted
  // despite this page's whole contract being that it does. Unmount flushes
  // the pending edit (fire-and-forget: there is no one left to tell).
  // Closing the tab outright still destroys the renderer before anything
  // runs — that residual window is the tab teardown, not this component.
  useEffect(
    () => () => {
      if (dirty.current && stRef.current !== null) {
        dirty.current = false
        void api('PUT', '/api/settings', { multi_chart: stRef.current })
      }
    },
    []
  )

  const edit = useCallback((fn: (s: MultiChartState) => MultiChartState) => {
    setSt((s) => {
      if (s === null) return s // pre-hydration: nothing to edit, nothing to save
      dirty.current = true
      return fn(s)
    })
  }, [])

  // ---- bars, one fetch per symbol ---------------------------------------
  // Successes are cached per symbol|timeframe; failures are NOT, so any later
  // change (add, timeframe switch) retries them. While a refetch is in
  // flight the previous line stays up (a blank flash on every timeframe
  // switch is worse); a failure then blanks it honestly via the chip.
  const cache = useRef(new Map<string, Bar[]>())
  useEffect(() => {
    if (!symbols || !timeframe) return
    let stop = false
    for (const sym of symbols) {
      const key = `${sym}|${timeframe}`
      const hit = cache.current.get(key)
      if (hit) {
        setData((d) => (d[sym]?.bars === hit ? d : { ...d, [sym]: { bars: hit } }))
        continue
      }
      api<{ bars: Bar[]; source: string; reason?: string }>(
        'GET',
        `/api/symbols/${encodeURIComponent(sym)}/bars?timeframe=${timeframe}&limit=400`
      )
        .then((r) => {
          if (stop) return
          if (r.bars.length === 0) {
            setData((d) => ({ ...d, [sym]: { bars: [], error: r.reason ?? 'no bars returned' } }))
          } else {
            cache.current.set(key, r.bars)
            setData((d) => ({ ...d, [sym]: { bars: r.bars } }))
          }
        })
        .catch((e) => {
          if (!stop)
            setData((d) => ({
              ...d,
              [sym]: { bars: [], error: e instanceof ApiError ? e.message : String(e) },
            }))
        })
    }
    return () => {
      stop = true
    }
  }, [symbols, timeframe])

  // Memoized so typing in the add box does not rebuild every chart series.
  const lines: CompareLine[] = useMemo(
    () =>
      (symbols ?? []).map((s, i) => ({
        symbol: s,
        bars: data[s]?.bars ?? [],
        color: PALETTE[i % PALETTE.length],
        hidden: hiddenList?.includes(s),
      })),
    [symbols, hiddenList, data]
  )

  // ---- drawing layer -----------------------------------------------------
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
    draw.current?.setTool(tool)
  }, [tool])

  useEffect(
    () => () => {
      draw.current?.destroy()
      draw.current = null
    },
    []
  )

  // ---- symbol actions ----------------------------------------------------
  const addSymbol = useCallback(
    (raw: string) => {
      const sym = raw.trim().toUpperCase()
      if (!/^[A-Z0-9.\-]{1,10}$/.test(sym)) return
      // REVIEW 2026-08-02: the cap must live on the WRITE side. Adds were
      // uncapped but hydration slices to 12, so a 13th symbol worked all
      // session and then silently vanished on the next visit.
      const cur = stRef.current
      if (cur && !cur.symbols.includes(sym) && cur.symbols.length >= 12) {
        setSaveErr('12 symbols max — remove one to add another')
        return
      }
      edit((s) =>
        s.symbols.includes(sym) || s.symbols.length >= 12
          ? s
          : { ...s, symbols: [...s.symbols, sym] }
      )
    },
    [edit]
  )

  const toggleHidden = useCallback(
    (sym: string) =>
      edit((s) =>
        s.symbols.includes(sym)
          ? {
              ...s,
              hidden: s.hidden.includes(sym)
                ? s.hidden.filter((x) => x !== sym)
                : [...s.hidden, sym],
            }
          : s
      ),
    [edit]
  )

  const removeSymbol = useCallback(
    (sym: string) =>
      edit((s) => ({
        ...s,
        symbols: s.symbols.filter((x) => x !== sym),
        hidden: s.hidden.filter((x) => x !== sym),
      })),
    [edit]
  )

  // ---- gesture-wheel actions (this page owns the chart it spawned over) --
  useEffect(() => {
    const off = window.grindstone.onChartAction(({ tool: t, symbol }) => {
      if (t === 'pointer' || t === 'trend' || t === 'hline') setTool(t)
      else if (t === 'clear') draw.current?.clear()
      else if (t === 'normalize') edit((s) => ({ ...s, normalize: !s.normalize }))
      else if (t === 'add' && symbol) addSymbol(symbol)
      else if (t === 'hide' && symbol) toggleHidden(symbol.toUpperCase())
      // ind:* is single-symbol overlay vocabulary — a no-op on the compare chart
    })
    return off
  }, [edit, addSymbol, toggleHidden])

  if (st === null) {
    return (
      <div className="page wide">
        <div className="page-head">
          <ChartMiniIcon />
          <h1>Charts</h1>
        </div>
        <div className="card dim">Loading…</div>
      </div>
    )
  }

  return (
    <div className="page wide">
      <div className="page-head">
        <ChartMiniIcon />
        <h1>Charts</h1>
        <span className="dim">multi-symbol compare</span>
      </div>

      {loadErr ? (
        <div className="test-result bad">Saved layout failed to load — starting fresh: {loadErr}</div>
      ) : null}

      <div className="card chart-card">
        <div className="chart-toolbar">
          <div className="seg">
            {TIMEFRAMES.map((t) => (
              <button
                key={t.key}
                className={`seg-btn${st.timeframe === t.key ? ' on' : ''}`}
                onClick={() => edit((s) => ({ ...s, timeframe: t.key }))}
              >
                {t.label}
              </button>
            ))}
          </div>
          <div className="seg">
            <button
              className={`seg-btn${st.normalize ? ' on' : ''}`}
              title="Rebase every line to % change from its first close"
              onClick={() => edit((s) => ({ ...s, normalize: !s.normalize }))}
            >
              % change
            </button>
          </div>
          {/* Toolbar mirrors the wheel's chart tools on purpose — the
              no-mouse path must reach everything the wheel reaches. */}
          <div className="seg">
            {DRAW_TOOLS.map((t) => (
              <button
                key={t.key}
                className={`seg-btn${tool === t.key ? ' on' : ''}`}
                onClick={() => setTool(t.key)}
              >
                {t.label}
              </button>
            ))}
            <button className="seg-btn" onClick={() => draw.current?.clear()}>
              Clear
            </button>
          </div>
          <form
            className="mc-add"
            onSubmit={(e) => {
              e.preventDefault()
              addSymbol(draft)
              setDraft('')
            }}
          >
            <input
              className="field mc-add-field"
              placeholder="Add symbol"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
            />
            <button className="btn" type="submit">
              Add
            </button>
          </form>
          {saveErr ? <span className="subtle mc-save-err">layout not saved: {saveErr}</span> : null}
        </div>

        <div className="mc-legend">
          {st.symbols.length === 0 ? (
            <span className="dim">No symbols — add one to start comparing.</span>
          ) : (
            lines.map((l) => {
              const err = data[l.symbol]?.error
              const loading = !err && l.bars.length === 0
              return (
                <span
                  key={l.symbol}
                  className={`mc-chip${l.hidden ? ' off' : ''}${err ? ' err' : ''}`}
                  title={err ? `${l.symbol}: ${err}` : loading ? `${l.symbol}: loading…` : l.symbol}
                >
                  <span className="mc-dot" style={{ background: l.color }} />
                  <span className="mc-sym">{l.symbol}</span>
                  {err ? <span className="mc-flag">!</span> : null}
                  <button
                    className="mc-chip-btn"
                    title={l.hidden ? 'Show' : 'Hide'}
                    onClick={() => toggleHidden(l.symbol)}
                  >
                    <EyeIcon off={!!l.hidden} />
                  </button>
                  <button
                    className="mc-chip-btn"
                    title="Remove"
                    onClick={() => removeSymbol(l.symbol)}
                  >
                    ×
                  </button>
                </span>
              )
            })
          )}
        </div>

        {st.symbols.length === 0 ? (
          <div className="chart-empty dim">
            Add a symbol above, or right-click any chart and pick a ticker.
          </div>
        ) : (
          <Chart
            lines={lines}
            normalize={st.normalize}
            height={460}
            onReady={handleChartReady}
            symbols={st.symbols}
            hiddenSymbols={st.hidden}
          />
        )}
      </div>
    </div>
  )
}
