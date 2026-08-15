/**
 * backtest.gs — the options backtest engine as a page.
 *
 * Everything here is job + poll: a run takes 60–110s in its own process, so
 * the page starts it, polls while it is live (2.5s), and settles back to a
 * slow tick when idle. Numbers are model-derived from end-of-day snapshots
 * and labeled estimated (NFR-2) — the engine's own calibration against real
 * tastytrade exports is one click away ("Verify engine"), which is also how
 * a modified engine proves it still makes the trades it is known to have
 * made from the exact data.
 */
import { announce, buildFormPayloadRaw, grab, listPad, mostRecentCompatible, specFromContracts } from '../datapad'
import { FormEvent, useCallback, useEffect, useRef, useState } from 'react'
import {
  api,
  ApiError,
  BacktestPreset,
  BacktestRun,
  BacktestStatus,
} from '../api'
import { BacktestIcon } from '../components/icons'
import {
  DEFAULT_FORM,
  SpecForm,
  SpecFormState,
  compileForm,
  tryDecompile,
} from './BacktestSpecForm'

const POLL_ACTIVE_MS = 2_500
const POLL_IDLE_MS = 20_000

function pct(v: unknown): string {
  return typeof v === 'number' ? `${v.toFixed(1)}%` : '—'
}
function num(v: unknown, digits = 2): string {
  return typeof v === 'number' ? v.toFixed(digits) : '—'
}
function money(v: unknown): string {
  return typeof v === 'number'
    ? v.toLocaleString(undefined, { maximumFractionDigits: 0 })
    : '—'
}

/** The calibration scorecard columns, straight from the engine's layers. */
function calibCells(r: NonNullable<BacktestRun['calib']>[number]) {
  const l1 = r.layers['L1 structure'] ?? {}
  const l2 = r.layers['L2 fills'] ?? {}
  const l3 = r.layers['L3 trades'] ?? {}
  const l4 = r.layers['L4 equity'] ?? {}
  return {
    trades: `${l3['reference trades'] ?? '—'} / ${l3['engine trades'] ?? '—'}`,
    strike: pct(l1['strike within $1 %']),
    fill: pct(l2['within 1c %']),
    close: pct(l3['same close date %']),
    corr: num(l4['curve correlation'], 3),
  }
}

export function BacktestPage() {
  const [status, setStatus] = useState<BacktestStatus | null>(null)
  const [presets, setPresets] = useState<BacktestPreset[] | null>(null)
  const [runs, setRuns] = useState<BacktestRun[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // Editor state: a preset loaded for editing, or a scratch spec. Two views
  // of ONE truth (edSpec): the form compiles into it, the JSON textarea edits
  // it directly — an AI agent and a human drive the same validator and the
  // same run endpoint.
  const [edName, setEdName] = useState('')
  const [edSpec, setEdSpec] = useState(() => JSON.stringify(compileForm(DEFAULT_FORM), null, 2))
  const [edFrom, setEdFrom] = useState<number | null>(null) // preset id or null
  const [edCheck, setEdCheck] = useState<{ ok: boolean; text: string } | null>(null)
  const [range, setRange] = useState({ start: '', end: '' })
  const [edMode, setEdMode] = useState<'form' | 'json'>('form')
  const [formInitial, setFormInitial] = useState<SpecFormState>(DEFAULT_FORM)
  const [formKey, setFormKey] = useState(0) // remounts SpecForm on load/switch
  const [formNote, setFormNote] = useState<string | null>(null)
  const [dataNote, setDataNote] = useState<string | null>(null)

  const active = runs?.find((r) => r.status === 'running') ?? null

  // Get/Post data (docs/DATA_EXCHANGE.md). GET grabs the spec the editor
  // holds -- edSpec is already the single truth both views compile into.
  // POST maps a chain or contract into spec JSON and lands it in the editor,
  // where the existing debounced validate gives the engine's verdict free.
  useEffect(() => {
    const off = window.grindstone.onDataAction(({ tool, entryId }) => {
      if (tool === 'data:get') {
        let spec: Record<string, unknown>
        try {
          spec = JSON.parse(edSpec) as Record<string, unknown>
        } catch {
          announce('the spec editor holds invalid JSON -- fix it before grabbing')
          return
        }
        grab(buildFormPayloadRaw({
          kind: 'backtest-spec',
          data: { name: edName, spec },
          page: 'backtest', address: 'backtest.gs',
        }))
          .then((e) => announce(`grabbed: ${e.label || 'backtest spec'}`))
          .catch((err) => announce(`get data failed: ${err instanceof Error ? err.message : err}`))
        return
      }
      if (tool === 'data:post' || tool === 'data:apply') {
        void listPad()
          .then((entries) => {
            const entry = entryId
              ? entries.find((e) => e.id === entryId)
              : mostRecentCompatible(entries, 'backtest-form')
            if (!entry) {
              announce(entryId ? 'that notepad entry is gone'
                               : 'nothing compatible in the notepad')
              return
            }
            const pk = entry.payload.kind
            if (pk === 'backtest-spec') {
              const d = entry.payload.data as { name?: string; spec?: unknown }
              setEdSpec(JSON.stringify(d.spec ?? d, null, 2))
              setEdMode('json')
              announce(`posted: ${entry.label || 'backtest spec'}`)
              return
            }
            const rows = pk === 'chain'
              ? ((entry.payload.data as { contracts?: Array<Record<string, unknown>> }).contracts ?? [])
              : [entry.payload.data as Record<string, unknown>]
            const today = new Date().toISOString().slice(0, 10)
            const r = specFromContracts(rows, today)
            if (!r.ok) {
              announce(r.reason)
              return
            }
            setEdSpec(JSON.stringify(r.spec, null, 2))
            setEdMode('json')  // tryDecompile-null is the existing fall-back signal
            announce(`posted: ${entry.label} as a ${(r.spec.legs as unknown[]).length}-leg spec`)
          })
          .catch((err) => announce(`post failed: ${err instanceof Error ? err.message : err}`))
      }
    })
    return off
  }, [edSpec, edName])

  // Monotonic guard, same reason as DataPage: a slow poll racing a
  // post-action refresh must never resurrect stale rows.
  const seq = useRef(0)
  // Whether the visible error came from the background poll: only those may
  // be cleared by a later successful poll — an action's error must survive
  // until the user acts again, not vanish on the next 2.5s tick.
  const errorFromPoll = useRef(false)
  const refresh = useCallback(async () => {
    const mine = ++seq.current
    try {
      const st = await api<BacktestStatus>('GET', '/api/backtests/status')
      const pr = await api<BacktestPreset[]>('GET', '/api/backtests/presets')
      const ru = await api<BacktestRun[]>('GET', '/api/backtests/runs')
      if (mine !== seq.current) return
      setStatus(st)
      setPresets(pr)
      setRuns(ru)
      if (errorFromPoll.current) {
        errorFromPoll.current = false
        setError(null)
      }
    } catch (e) {
      if (mine !== seq.current) return
      errorFromPoll.current = true
      setError(e instanceof ApiError ? e.message : String(e))
    }
  }, [])

  useEffect(() => {
    refresh()
    const t = window.setInterval(refresh, active ? POLL_ACTIVE_MS : POLL_IDLE_MS)
    return () => window.clearInterval(t)
  }, [refresh, active !== null])

  // Debounced validation: the engine's own validator, verbatim errors. The
  // seq guard matters: two in-flight validations can resolve out of order,
  // and a stale OK on since-broken text would enable the Run button.
  const checkSeq = useRef(0)
  useEffect(() => {
    const mine = ++checkSeq.current
    if (!edSpec.trim()) {
      setEdCheck(null)
      return
    }
    const t = window.setTimeout(async () => {
      let parsed: unknown
      try {
        parsed = JSON.parse(edSpec)
      } catch (e) {
        if (mine === checkSeq.current) {
          setEdCheck({ ok: false, text: `not JSON yet: ${String(e).slice(0, 120)}` })
        }
        return
      }
      try {
        const r = await api<{ ok: boolean; describe?: string; error?: string }>(
          'POST', '/api/backtests/validate', { spec: parsed })
        if (mine !== checkSeq.current) return
        setEdCheck(r.ok ? { ok: true, text: r.describe ?? 'valid' }
                        : { ok: false, text: r.error ?? 'invalid' })
      } catch (e) {
        if (mine !== checkSeq.current) return
        setEdCheck({ ok: false, text: e instanceof ApiError ? e.message : String(e) })
      }
    }, 500)
    return () => window.clearTimeout(t)
  }, [edSpec])

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try {
      await fn()
      await refresh()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const loadIntoEditor = (p: BacktestPreset) => {
    setEdName(p.builtin ? `${p.name} copy` : p.name)
    setEdSpec(JSON.stringify(p.spec, null, 2))
    setEdFrom(p.builtin ? null : p.id)
    setEdCheck(null)
    setFormNote(null)
    // Open in the friendliest mode the spec allows: the form when it fits,
    // JSON (with a why) when the spec uses features beyond it.
    const f = tryDecompile(p.spec)
    if (f) {
      setFormInitial(f)
      setFormKey((k) => k + 1)
      setEdMode('form')
    } else {
      setEdMode('json')
      setFormNote('this spec uses features beyond the form — editing as JSON')
    }
  }

  const switchMode = (mode: 'form' | 'json') => {
    setFormNote(null)
    if (mode === 'json' || edMode === mode) {
      setEdMode(mode)
      return
    }
    // JSON -> form only when nothing would be dropped.
    try {
      const f = tryDecompile(JSON.parse(edSpec))
      if (!f) {
        setFormNote('this spec uses features beyond the form — keep editing JSON')
        return
      }
      setFormInitial(f)
      setFormKey((k) => k + 1)
      setEdMode('form')
    } catch {
      setFormNote('fix the JSON first — it does not parse yet')
    }
  }

  const syncNow = () =>
    act(async () => {
      await api('POST', '/api/backtests/data/sync', { underlying: 'SPY' })
      setDataNote('sync started — the store updates in the background')
    })

  const setupRecording = () =>
    act(async () => {
      const r = await api<{ created: string[]; note: string }>(
        'POST', '/api/backtests/data/setup-recording', { underlying: 'SPY' })
      setDataNote(r.created.length
        ? `recording jobs created: ${r.created.join(', ')} — manage them on data.gs`
        : 'recording jobs already exist — manage them on data.gs')
    })

  const savePreset = (e: FormEvent) => {
    e.preventDefault()
    act(async () => {
      const spec = JSON.parse(edSpec) // inside act(): a parse race surfaces as the error card
      if (edFrom !== null) {
        await api('PATCH', `/api/backtests/presets/${edFrom}`, { name: edName, spec })
      } else {
        const r = await api<{ id: number }>('POST', '/api/backtests/presets',
          { name: edName, spec })
        // The editor now owns the created row — saving again must UPDATE it,
        // not 409 on its own name.
        setEdFrom(r.id)
      }
    })
  }

  const DATE_RE = /^\d{4}-\d{2}-\d{2}$/
  const datesOk =
    (!range.start || DATE_RE.test(range.start)) && (!range.end || DATE_RE.test(range.end))

  const startRun = (presetId?: number) =>
    act(() =>
      api('POST', '/api/backtests/runs', {
        kind: 'run',
        ...(presetId !== undefined
          ? { preset_id: presetId }
          : { spec: JSON.parse(edSpec) as Record<string, unknown>, name: edName || undefined }),
        ...(range.start ? { start: range.start } : {}),
        ...(range.end ? { end: range.end } : {}),
      })
    )

  const verify = () => act(() => api('POST', '/api/backtests/runs', { kind: 'calibration' }))

  // Cancel is its own verb: sending DELETE here could race a finishing run
  // and destroy its results instead of stopping it.
  const cancel = (id: number) => act(() => api('POST', `/api/backtests/runs/${id}/cancel`))
  const removeRun = (id: number) => {
    if (!confirm('Delete this run and its report?')) return
    act(() => api('DELETE', `/api/backtests/runs/${id}`))
  }
  const removePreset = (p: BacktestPreset) => {
    if (!confirm(`Delete preset "${p.name}"?`)) return
    act(() => api('DELETE', `/api/backtests/presets/${p.id}`))
  }

  const lastCalib = runs?.find((r) => r.kind === 'calibration' && r.status === 'done')

  return (
    <div className="page" data-wheel-context="backtest-form">
      <div className="page-head">
        <BacktestIcon />
        <h1>Backtest</h1>
        {status ? (
          <span className="dim">
            {status.source === 'recorded'
              ? `your recorded data: ${status.recorded.days} day${status.recorded.days === 1 ? '' : 's'}`
              : status.can_run
                ? `chains: ${status.options_db.size_mb.toLocaleString()} MB`
                : 'chain data missing'}
          </span>
        ) : null}
      </div>

      {status ? (
        <div className="card">
          <h2>Data</h2>
          <div className="subtle" style={{ marginBottom: 10 }}>
            {status.source === 'deep' || status.source.startsWith('legacy') ? (
              <>
                Reading this machine's chain database (
                {status.options_db.size_mb.toLocaleString()} MB at{' '}
                <code>{status.options_db.path}</code>). Recording still works alongside
                it — the app keeps its own store for machines without this file.
                {status.source.startsWith('legacy') ? (
                  <strong>
                    {' '}This file is in the LEGACY location — run{' '}
                    <code>python tools/consolidate.py --apply</code> to move it
                    into the uniform data tree.
                  </strong>
                ) : null}
              </>
            ) : status.source === 'custom' ? (
              <>
                Reading the database set in Settings: <code>{status.options_db.path}</code>
                {status.options_db.present ? '' : ' — not found there'}.
              </>
            ) : status.recorded.days > 0 ? (
              <>
                Reading the app's own store, built from your recorded snapshots:{' '}
                <strong>
                  {status.recorded.days} days ({status.recorded.first} →{' '}
                  {status.recorded.last})
                </strong>
                , {status.recorded.contracts.toLocaleString()} contract rows. Every run
                syncs the latest recordings in first. Short histories make honest but
                thin backtests — the store grows every day recording runs.
              </>
            ) : (
              <>
                No chain data yet. The app has created its own store and will fill it
                from recorded chain snapshots: click <em>Set up recording</em> (needs a
                data-capable account on accounts.gs), or point Settings at an existing
                chain database. Equity chains record through Alpaca and futures chains
                through TastyTrade; a hosted data feed can fill the same store later.
              </>
            )}
          </div>
          {status.sync.state === 'running' ? (
            <div className="subtle">syncing recorded data…</div>
          ) : status.sync.state === 'error' ? (
            <div className="test-result bad">last sync failed: {status.sync.error}</div>
          ) : status.sync.state === 'done' ? (
            <div className="subtle">
              last sync: +{status.sync.days} day{status.sync.days === 1 ? '' : 's'},{' '}
              {status.sync.contracts?.toLocaleString()} contracts
            </div>
          ) : null}
          {dataNote ? <div className="test-result ok">{dataNote}</div> : null}
          <div className="row-actions" style={{ marginTop: 8 }}>
            <button className="btn" disabled={busy || status.sync.state === 'running'}
                    onClick={syncNow}>
              {status.sync.state === 'running' ? 'Syncing…' : 'Sync recorded data'}
            </button>
            <button className="btn" disabled={busy} onClick={setupRecording}>
              Set up recording
            </button>
          </div>
        </div>
      ) : null}

      {active ? (
        <div className="card">
          <h2>Running: {active.name}</h2>
          <div className="acct-row">
            <span className="badge paper">running</span>
            <strong>
              {active.progress?.phase ?? 'starting'}
              {typeof active.progress?.pct === 'number' ? ` · ${active.progress.pct}%` : ''}
            </strong>
            <span className="subtle">started {active.started_at.replace('T', ' ').slice(0, 19)}</span>
            <div className="row-actions">
              <button className="btn" disabled={busy} onClick={() => cancel(active.id)}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      ) : null}

      <div className="card">
        <h2>Presets</h2>
        <div className="subtle" style={{ marginBottom: 10 }}>
          Strategy specs, stored per user. Built-ins are read-only references — the three
          calibration specs reproduce the shipped tastytrade exports. Results are
          model-derived estimates from end-of-day chain snapshots.
        </div>
        {presets === null ? (
          <div className="dim">Loading…</div>
        ) : (
          presets.map((p) => (
            <div className="acct-row" key={p.id}>
              {p.calibration ? (
                <span className="badge data">calibration</span>
              ) : p.builtin ? (
                <span className="badge data">built-in</span>
              ) : (
                <span className="badge paper">yours</span>
              )}
              <strong>{p.name}</strong>
              <span className="subtle">
                {(p.spec.legs as unknown[] | undefined)?.length ?? 0} leg
                {((p.spec.legs as unknown[] | undefined)?.length ?? 0) === 1 ? '' : 's'}
              </span>
              <div className="row-actions">
                <button
                  className="btn"
                  disabled={busy || !!active || !status?.can_run || !datesOk}
                  onClick={() => startRun(p.id)}
                >
                  Run
                </button>
                <button className="btn" onClick={() => loadIntoEditor(p)}>
                  {p.builtin ? 'Duplicate' : 'Edit'}
                </button>
                {!p.builtin ? (
                  <button className="btn" disabled={busy} onClick={() => removePreset(p)}>
                    Delete
                  </button>
                ) : null}
              </div>
            </div>
          ))
        )}
      </div>

      <form className="card" onSubmit={savePreset}>
        <h2>{edFrom !== null ? 'Edit preset' : 'New preset'}</h2>
        <div className="row-actions" style={{ marginBottom: 10 }}>
          <button
            className={`btn${edMode === 'form' ? ' primary' : ''}`}
            type="button"
            onClick={() => switchMode('form')}
          >
            Form
          </button>
          <button
            className={`btn${edMode === 'json' ? ' primary' : ''}`}
            type="button"
            onClick={() => switchMode('json')}
          >
            JSON
          </button>
          <span className="subtle">
            {edMode === 'form'
              ? 'the form writes the same spec an AI agent would'
              : 'full spec language — sweeps, params, custom rules'}
          </span>
        </div>
        {formNote ? <div className="test-result bad">{formNote}</div> : null}
        <div className="form-grid">
          <label>Name</label>
          <input
            className="field"
            value={edName}
            onChange={(e) => setEdName(e.target.value)}
            placeholder="my_put_spread"
            spellCheck={false}
          />
        </div>
        {edMode === 'form' ? (
          <SpecForm
            key={formKey}
            initial={formInitial}
            onSpec={(spec) => setEdSpec(JSON.stringify(spec, null, 2))}
          />
        ) : (
          <div className="form-grid">
            <label>Spec (JSON)</label>
            <textarea
              className="field"
              value={edSpec}
              onChange={(e) => setEdSpec(e.target.value)}
              placeholder='{"legs": [{"action": "sell", "right": "put", "delta": 0.3, "dte": 45}], "exits": {"dte": 21, "take_profit": 0.5}}'
              rows={12}
              spellCheck={false}
              style={{ fontFamily: 'ui-monospace, monospace', fontSize: 12 }}
            />
          </div>
        )}
        <div className="form-grid">
          <label>From</label>
          <input
            className="field"
            value={range.start}
            onChange={(e) => setRange({ ...range, start: e.target.value })}
            placeholder="2013-01-02 (optional)"
            spellCheck={false}
          />
          <label>To</label>
          <input
            className="field"
            value={range.end}
            onChange={(e) => setRange({ ...range, end: e.target.value })}
            placeholder="2026-04-17 (optional)"
            spellCheck={false}
          />
        </div>
        {edCheck ? (
          <div className={`test-result ${edCheck.ok ? 'ok' : 'bad'}`}>
            <pre style={{ margin: 0, whiteSpace: 'pre-wrap', fontFamily: 'inherit' }}>
              {edCheck.text}
            </pre>
          </div>
        ) : null}
        {error ? <div className="test-result bad">{error}</div> : null}
        <div className="row-actions">
          <button
            className="btn primary"
            type="submit"
            disabled={busy || !edName.trim() || !edCheck?.ok}
          >
            {busy ? 'Saving…' : edFrom !== null ? 'Save preset' : 'Save as preset'}
          </button>
          <button
            className="btn"
            type="button"
            disabled={busy || !!active || !edCheck?.ok || !status?.can_run || !datesOk}
            onClick={() => startRun()}
          >
            {active ? 'A run is active' : 'Run this spec'}
          </button>
          {!datesOk ? (
            <span className="subtle">dates must be YYYY-MM-DD (or empty)</span>
          ) : null}
        </div>
      </form>

      <div className="card">
        <h2>Verify engine</h2>
        <div className="subtle" style={{ marginBottom: 10 }}>
          Replays the {status?.calibration.references.length ?? 3} tastytrade reference
          backtests shipped with the app — the trades the engine is known to have made
          from this exact data — and compares strike selection, fills, trades and the
          equity curve. Run it after changing engine code: if the layers still agree,
          the mechanics survived your change.
        </div>
        <button
          className="btn primary"
          disabled={busy || !!active || !status?.can_run}
          onClick={verify}
        >
          {busy ? 'Starting…' : 'Verify engine'}
        </button>
        {lastCalib?.calib ? (
          <div style={{ marginTop: 12 }}>
            <div className="subtle">
              last verification: {lastCalib.finished_at.replace('T', ' ').slice(0, 19)}
            </div>
            {lastCalib.calib.map((r) => {
              const c = calibCells(r)
              return (
                <div className="acct-row" key={r.reference}>
                  <strong>{r.reference}</strong>
                  <span className="subtle">
                    trades {c.trades} · strike ±$1 {c.strike} · fill ≤1¢ {c.fill} · same
                    close {c.close} · corr {c.corr}
                  </span>
                  <div className="row-actions">
                    <button
                      className="btn"
                      onClick={() => window.grindstone.openBacktestReport(lastCalib.id, r.report)}
                    >
                      Report
                    </button>
                  </div>
                </div>
              )
            })}
          </div>
        ) : null}
      </div>

      <div className="card">
        <h2>Run history</h2>
        {runs === null ? (
          <div className="dim">Loading…</div>
        ) : runs.length === 0 ? (
          <div className="dim">No runs yet — pick a preset above.</div>
        ) : (
          runs.map((r) => (
            <div className="acct-row" key={r.id}>
              <span className={`badge ${r.status === 'done' ? 'paper' : 'data'}`}>
                {r.status}
              </span>
              <strong>
                {r.name}
                {r.kind === 'calibration' ? ' · calibration' : ''}
              </strong>
              <span className="subtle">
                {r.status === 'done' && r.kind === 'run' ? (
                  <>
                    CAGR {pct(r.summary.cagr_pct)} · net{' '}
                    {money(r.summary.final_net_liq)} · DD {pct(r.summary.max_drawdown_pct)} ·{' '}
                    {r.summary.trades ?? '—'} trades · est.
                  </>
                ) : r.status === 'error' ? (
                  r.error.slice(0, 90)
                ) : (
                  r.started_at.replace('T', ' ').slice(0, 19)
                )}
              </span>
              <div className="row-actions">
                {r.status === 'done' && r.report_files.length > 0 && r.kind === 'run' ? (
                  <button
                    className="btn"
                    onClick={() => window.grindstone.openBacktestReport(r.id)}
                  >
                    Report
                  </button>
                ) : null}
                {r.status === 'running' ? (
                  <button className="btn" disabled={busy} onClick={() => cancel(r.id)}>
                    Cancel
                  </button>
                ) : (
                  <button className="btn" disabled={busy} onClick={() => removeRun(r.id)}>
                    Delete
                  </button>
                )}
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  )
}
