/**
 * charts.gs — the multi-symbol comparison chart. Several tickers as line
 * series on one chart; add/remove/show/hide per symbol; normalize rebases
 * each line to % change from its first loaded close. State {symbols,
 * normalize, timeframe, hidden, isolated} persists through the settings
 * door (values.multi_chart) with a 500ms debounce so a burst of edits is
 * one PUT.
 *
 * ISOLATION: isolated=SYM shows only that symbol. The stored hidden list is
 * NOT touched — isolation is a lens over it, so switching it off restores
 * the previous eye states exactly. Effective hidden (what the chart and the
 * wheel context see) = isolated ? all-but-isolated : hidden.
 *
 * Drawings anchor to the FIRST VISIBLE symbol's series — its scale defines
 * {time, price} for everything drawn here (in % mode "price" is a percent).
 * Hide (or isolate away) that symbol and drawings re-project onto the next
 * one's scale.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, ApiError } from '../api'
import { makeChartStore } from '../chartStore'
import { Bar, Chart, ChartReadyApi, CompareLine } from '../components/Chart'
import { ChartDraw, DrawTool } from '../components/ChartDraw'
import { DrawEditor } from '../components/DrawEditor'
import { ChartMiniIcon } from '../components/icons'
import '../charts.css'
import '../charttools.css'

/** Engine report shape — single source for counts/selection (contract). */
type EngineState = ReturnType<ChartDraw['getState']>

interface MultiChartState {
  symbols: string[]
  normalize: boolean
  timeframe: string
  hidden: string[]
  isolated: string | null
}

const TIMEFRAMES: { key: string; label: string }[] = [
  { key: '1Min', label: '1m' },
  { key: '5Min', label: '5m' },
  { key: '15Min', label: '15m' },
  { key: '1Hour', label: '1H' },
  { key: '1Day', label: '1D' },
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
  { key: 'inspect', label: 'Inspect', title: 'Inspect a bar under the cursor' },
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
  isolated: null,
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
  // isolated must name a current symbol or be null — anything else (stale
  // blob edited elsewhere, junk through the door) degrades to "off".
  const isolated =
    typeof o.isolated === 'string' && symbols.includes(o.isolated.toUpperCase())
      ? o.isolated.toUpperCase()
      : null
  return {
    symbols,
    normalize: typeof o.normalize === 'boolean' ? o.normalize : DEFAULT_STATE.normalize,
    timeframe:
      typeof o.timeframe === 'string' && TIMEFRAMES.some((t) => t.key === o.timeframe)
        ? o.timeframe
        : DEFAULT_STATE.timeframe,
    hidden,
    isolated,
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
  const [drawSaveErr, setDrawSaveErr] = useState<string | null>(null)
  const [engine, setEngine] = useState<ChartDraw | null>(null)
  const [drawState, setDrawState] = useState<EngineState | null>(null)
  const [drawingsHidden, setDrawingsHidden] = useState(false)

  const dirty = useRef(false)
  const draw = useRef<ChartDraw | null>(null)
  // Built once — handleChartReady is stable and the engine keeps the adapter
  // for its whole life.
  const chartStore = useMemo(() => makeChartStore(setDrawSaveErr), [])
  const toolRef = useRef(tool)
  toolRef.current = tool
  const drawHiddenRef = useRef(false)

  const symbols = st?.symbols
  const hiddenList = st?.hidden
  const timeframe = st?.timeframe
  const isolated = st?.isolated ?? null

  // REVIEW 2026-08-02: normalize is IN the key. It swaps the whole price
  // scale between dollars and percent; one shared bucket meant $560 anchors
  // projected thousands of pixels off a ±10% axis (drawings "vanished") and
  // the bucket became a permanent dollar/percent mix. Same rule as the
  // timeframe: a key per scale semantics. Isolation is NOT in the key — it
  // changes which series drawings project through, not what a stored
  // {time, price} means.
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
        // No limit param: the user's chart_candles setting decides (default:
        // all available). The featured search card and the 52-week range
        // fetch keep explicit limits — those depths are intentional.
        `/api/symbols/${encodeURIComponent(sym)}/bars?timeframe=${timeframe}`
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

  // Effective visibility: isolation is a lens over the stored hidden list.
  const effectiveHidden = useMemo(
    () => (isolated ? (symbols ?? []).filter((x) => x !== isolated) : hiddenList ?? []),
    [isolated, symbols, hiddenList]
  )

  // Memoized so typing in the add box does not rebuild every chart series.
  const lines: CompareLine[] = useMemo(
    () =>
      (symbols ?? []).map((s, i) => ({
        symbol: s,
        bars: data[s]?.bars ?? [],
        color: PALETTE[i % PALETTE.length],
        hidden: effectiveHidden.includes(s),
      })),
    [symbols, effectiveHidden, data]
  )

  // The engine's constructor wants the anchor series' bars (measure counts,
  // inspect readouts) — that's the first VISIBLE line, same one Chart hands
  // to onReady as mainSeries.
  const mainBarsRef = useRef<Bar[]>([])
  mainBarsRef.current = lines.find((l) => !l.hidden && l.bars.length > 0)?.bars ?? []

  // ---- drawing layer -----------------------------------------------------
  const handleChartReady = useCallback(({ chart, mainSeries }: ChartReadyApi) => {
    if (draw.current && draw.current.chart === chart) {
      draw.current.setKey(drawKeyRef.current)
      draw.current.setSeries(mainSeries)
      // setKey may have switched buckets — re-read counts for the attrs.
      setDrawState(draw.current.getState())
      return
    }
    draw.current?.destroy()
    // A getter, so measure/inspect always read the CURRENT anchor symbol's
    // bars — snapshots went stale on refetch (build-flagged trap, closed).
    const d = new ChartDraw(drawKeyRef.current, chart, mainSeries, {
      bars: () => mainBarsRef.current,
      store: chartStore,
    })
    d.setTool(toolRef.current)
    d.setDrawingsHidden(drawHiddenRef.current)
    d.onChange((s: EngineState) => {
      setDrawState(s)
      // The engine can now disarm itself (Escape's last rung) while the PAGE
      // owns the lit toolbar button. Mirror its tool back or they desync: React
      // would still hold 'trend', so clicking Trend again is a same-value
      // setState — no re-render, no effect, and the toolbar looks dead.
      // setTool()'s `if (tool === this.tool) return` guard makes the round trip
      // a no-op, so this cannot loop.
      setTool(s.tool)
    })
    draw.current = d
    setEngine(d)
    setDrawState(d.getState())
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

  const toggleDrawVis = useCallback(() => {
    const next = !drawHiddenRef.current
    drawHiddenRef.current = next
    setDrawingsHidden(next)
    draw.current?.setDrawingsHidden(next)
  }, [])

  // Delete: with a live selection it IS "delete the selection"; with nothing
  // selected it arms click-to-delete. The wheel's 'delete' routes here too.
  const deleteAction = useCallback(() => {
    const s = draw.current?.getState()
    // `selected` (every kind), not `selection` (drawings only) — otherwise a
    // selected measurement fell through to arming click-to-delete instead of
    // deleting the thing the user had just selected.
    if (s && s.selected.length > 0) draw.current?.deleteSelected()
    else setTool('delete')
  }, [])

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

  // The eye edits the STORED list even while isolation is active — it sets
  // what you come back to, and the chip's eye state shows it all along.
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
        isolated: s.isolated === sym ? null : s.isolated,
      })),
    [edit]
  )

  const toggleSolo = useCallback(
    (sym: string) =>
      edit((s) =>
        s.symbols.includes(sym) ? { ...s, isolated: s.isolated === sym ? null : sym } : s
      ),
    [edit]
  )

  // ---- gesture-wheel actions (this page owns the chart it spawned over) --
  // Full v3 vocabulary. ind:* and 'settings' are single-symbol overlay
  // vocabulary — the compare chart computes no indicators, so there is
  // nothing to toggle or configure: deliberate no-ops, not omissions.
  useEffect(() => {
    const off = window.grindstone.onChartAction(({ tool: t, symbol }) => {
      if ((ARMABLE as readonly string[]).includes(t)) setTool(t as DrawTool)
      else if (t === 'delete') deleteAction()
      else if (t === 'clear') draw.current?.clearDrawings()
      else if (t === 'clearmeasure') draw.current?.clearMeasures()
      else if (t === 'vis:draw') toggleDrawVis()
      else if (t === 'normalize') edit((s) => ({ ...s, normalize: !s.normalize }))
      else if (t === 'isolate') {
        // The wheel action carries no target: solo the first VISIBLE (per
        // the stored hidden list) symbol; if anything is already isolated
        // the same action means "isolation off". Chosen because the wheel
        // is a toggle-shaped gesture — one segment, both directions.
        edit((s) => {
          if (s.isolated !== null) return { ...s, isolated: null }
          const first = s.symbols.find((x) => !s.hidden.includes(x))
          return first ? { ...s, isolated: first } : s
        })
      } else if (t.startsWith('tf:')) {
        const k = t.slice(3)
        if (TIMEFRAMES.some((tf) => tf.key === k)) edit((s) => ({ ...s, timeframe: k }))
      } else if (t === 'add' && symbol) addSymbol(symbol)
      else if (t === 'hide' && symbol) toggleHidden(symbol.toUpperCase())
    })
    return off
  }, [edit, addSymbol, toggleHidden, toggleDrawVis, deleteAction])

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

  const flags = drawingsHidden ? ['drawhidden'] : []
  const selection = drawState?.selection ?? []
  /** Every selected object, not just drawings — a measurement counts as a
   *  selection for the toolbar's sake even though it has no coordinate boxes
   *  to edit. */
  const selectedCount = drawState?.selected.length ?? 0

  const toolBtn = (t: { key: DrawTool; label: string; title: string }) => (
    <button
      key={t.key}
      className={`seg-btn${tool === t.key ? ' on' : ''}`}
      title={t.title}
      onClick={() => setTool(t.key)}
    >
      {t.label}
    </button>
  )

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
          <div className="tgrp">
            <span className="tgrp-label">Time</span>
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
          </div>
          <div className="tgrp">
            <span className="tgrp-label">Draw</span>
            <div className="seg">
              {PLACE_TOOLS.map(toolBtn)}
              <span className="seg-sep" />
              <button
                className={`seg-btn${tool === 'delete' ? ' on' : ''}`}
                title="Delete — removes the selection, or arms click-to-delete"
                onClick={deleteAction}
              >
                Del
              </button>
              {toolBtn(TRIM_TOOL)}
              <span className="seg-sep" />
              <button
                className="seg-btn"
                title="Clear every drawing on this timeframe/mode"
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
                className={`seg-btn${st.normalize ? ' on' : ''}`}
                title="Rebase every line to % change from its first close"
                onClick={() => edit((s) => ({ ...s, normalize: !s.normalize }))}
              >
                % change
              </button>
              {st.isolated ? (
                <button
                  className="seg-btn on"
                  title={`Showing only ${st.isolated} — click to restore the previous eye states`}
                  onClick={() => edit((s) => ({ ...s, isolated: null }))}
                >
                  ⦿ {st.isolated} off
                </button>
              ) : null}
            </div>
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
          {/* Background save, so a failure has no other way to surface. */}
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

        <div className="mc-legend">
          {st.symbols.length === 0 ? (
            <span className="dim">No symbols — add one to start comparing.</span>
          ) : (
            lines.map((l) => {
              const err = data[l.symbol]?.error
              const loading = !err && l.bars.length === 0
              // Chip styling reads STORED hidden (the eye's truth); isolation
              // paints over it: the solo chip glows, everyone else ghosts.
              const storedHidden = st.hidden.includes(l.symbol)
              const solo = st.isolated === l.symbol
              const ghost = st.isolated !== null && !solo
              return (
                <span
                  key={l.symbol}
                  className={`mc-chip${storedHidden ? ' off' : ''}${err ? ' err' : ''}${
                    solo ? ' solo' : ''
                  }${ghost ? ' ghost' : ''}`}
                  title={err ? `${l.symbol}: ${err}` : loading ? `${l.symbol}: loading…` : l.symbol}
                >
                  <span className="mc-dot" style={{ background: l.color }} />
                  <span className="mc-sym">{l.symbol}</span>
                  {err ? <span className="mc-flag">!</span> : null}
                  <button
                    className="mc-chip-btn"
                    title={storedHidden ? 'Show' : 'Hide'}
                    onClick={() => toggleHidden(l.symbol)}
                  >
                    <EyeIcon off={storedHidden} />
                  </button>
                  <button
                    className={`mc-chip-btn${solo ? ' solo-on' : ''}`}
                    title={solo ? 'Isolation off' : `Show only ${l.symbol}`}
                    onClick={() => toggleSolo(l.symbol)}
                  >
                    ⦿
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

        <div className="chart-stage">
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
              hiddenSymbols={effectiveHidden}
              timeframe={st.timeframe}
              flags={flags}
              isolated={st.isolated}
              drawTool={tool}
              drawCount={drawState?.drawings ?? 0}
              measureCount={drawState?.measures ?? 0}
              selectedCount={selectedCount}
              dofFree={drawState?.dof.free ?? null}
            />
          )}
          {/* Selection editor appears with the selection and leaves with it. */}
          {engine && selection.length > 0 && st.symbols.length > 0 ? (
            <div className="float-panel draw-editor-float">
              <DrawEditor
                engine={engine}
                selection={selection}
                lockedSlots={drawState?.lockedSlots}
              />
            </div>
          ) : null}
        </div>
      </div>
    </div>
  )
}
