import { FormEvent, useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, DataUsage, RecordJob } from '../api'
import { DataIcon } from '../components/icons'

const KINDS = [
  { key: 'bars', label: 'Price bars (OHLCV)' },
  { key: 'chain', label: 'Options chain snapshots' },
  { key: 'news', label: 'News capture' },
]
const TIMEFRAMES = ['1Min', '5Min', '15Min', '1Hour', '1Day']
const INTERVALS = [
  { s: 60, label: 'every minute' },
  { s: 300, label: 'every 5 minutes' },
  { s: 900, label: 'every 15 minutes' },
  { s: 3600, label: 'hourly' },
  { s: 86400, label: 'daily' },
]

const EMPTY = { kind: 'chain', symbol: '', timeframe: '15Min', interval_seconds: 900, retention_days: 90 }

function bytes(n: number): string {
  if (n > 1 << 30) return `${(n / (1 << 30)).toFixed(2)} GB`
  if (n > 1 << 20) return `${(n / (1 << 20)).toFixed(1)} MB`
  return `${(n / 1024).toFixed(0)} KB`
}

interface ImportStatus {
  state: 'idle' | 'running' | 'done' | 'refused' | 'error'
  file?: string
  reason?: string
  error?: string
  days?: number
  contracts?: number
  bars?: number
  done?: number
  total?: number
}

interface PullStatus {
  state: 'idle' | 'running' | 'done' | 'stopped' | 'error'
  window_from: string
  window_to: string
  symbols?: string[]
  from?: string
  to?: string
  total?: number
  done?: number
  written?: number
  skipped?: number
  note?: string
  last?: string
  error?: string
  eta_seconds?: number
  delay?: number
}

interface SysKey {
  enrolled: boolean
  readable: boolean
  platform_ok: boolean
  verdict?: string
  enrolled_at?: string
  key_id_last4?: string
}

interface Probe {
  verdict: 'LIVE_CAPABLE' | 'PAPER_ONLY' | 'UNDETERMINED'
  detail: string
  safe_to_store_unattended: boolean
}

interface Autostart {
  supported: boolean
  registered: boolean
  method: string
  note?: string
  reason?: string
}

function hhmm(sec: number): string {
  if (sec < 90) return `${Math.round(sec)}s`
  if (sec < 5400) return `${Math.round(sec / 60)} min`
  return `${(sec / 3600).toFixed(1)} hours`
}

interface CoverageReport {
  have: number
  absent: number
  failed: number
  first?: string | null
  last?: string | null
  remaining: number
  window?: { start: string; end: string }
}

interface BackfillPlan {
  enabled: boolean
  chunks: Array<{ start: string; end: string; days: number }>
  planned_days: number
  remaining_days: number
  truncated: boolean
}

export function DataPage() {
  const [jobs, setJobs] = useState<RecordJob[] | null>(null)
  const [covSym, setCovSym] = useState('')
  const [cov, setCov] = useState<CoverageReport | null>(null)
  const [plan, setPlan] = useState<BackfillPlan | null>(null)
  const [covErr, setCovErr] = useState<string | null>(null)

  // Coverage and the plan are fetched TOGETHER: the summary says what is on
  // disk, the plan says what the next run would actually do about it, and
  // showing one without the other invites "why is it still missing?".
  const loadCoverage = async (): Promise<void> => {
    const sym = covSym.trim().toUpperCase()
    if (!sym) return
    setCovErr(null)
    try {
      const q = `symbol=${encodeURIComponent(sym)}&kind=bars&timeframe=1Day`
      const [c, pl] = await Promise.all([
        api<CoverageReport>('GET', `/api/datamgmt/coverage?${q}`),
        api<BackfillPlan>('GET', `/api/datamgmt/backfill?${q}`),
      ])
      setCov(c)
      setPlan(pl)
    } catch (e) {
      setCov(null); setPlan(null)
      setCovErr(e instanceof ApiError ? e.message : String(e))
    }
  }
  const [usage, setUsage] = useState<DataUsage | null>(null)
  const [form, setForm] = useState({ ...EMPTY })
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // Import + puller. Both are start-then-poll: a synchronous import would hit
  // the IPC bridge's 30s deadline, and a timeout mid-commit is the one outcome
  // DATA_IMPORT.md calls worse than a refusal, because it looks like success.
  const [imp, setImp] = useState<ImportStatus | null>(null)
  const [impKind, setImpKind] = useState('option_chain')
  const [impSym, setImpSym] = useState('SPY')
  const [pull, setPull] = useState<PullStatus | null>(null)
  const [pullSyms, setPullSyms] = useState('SPY')
  const [pullFrom, setPullFrom] = useState('')
  const [pullTo, setPullTo] = useState('')

  // The recording key. Deliberately a two-step flow: check, SEE the verdict,
  // then store. A single "save" button would make the probe decoration, and
  // the whole point is that the user consents to what the key can actually do
  // rather than to the label they picked for it.
  const [sk, setSk] = useState<SysKey | null>(null)
  const [skId, setSkId] = useState('')
  const [skSecret, setSkSecret] = useState('')
  const [probe, setProbe] = useState<Probe | null>(null)
  const [skBusy, setSkBusy] = useState(false)
  const [auto, setAuto] = useState<Autostart | null>(null)

  // Monotonic guard: the 20s tick racing a post-add refresh made a
  // just-added job vanish when the older response landed last.
  const refreshSeq = useRef(0)
  const refresh = useCallback(async () => {
    const mine = ++refreshSeq.current
    try {
      const j = await api<RecordJob[]>('GET', '/api/datamgmt/jobs')
      const u = await api<DataUsage>('GET', '/api/datamgmt/usage')
      if (mine !== refreshSeq.current) return
      setJobs(j)
      setUsage(u)
      setError(null)
    } catch (e) {
      if (mine !== refreshSeq.current) return
      setError(e instanceof ApiError ? e.message : String(e))
    }
  }, [])

  useEffect(() => {
    refresh()
    const t = window.setInterval(refresh, 20_000)
    return () => window.clearInterval(t)
  }, [refresh])

  // Job status on its own clock, and a FASTER one while something is running:
  // a puller stepping one ticker-day every 5-10s looks frozen on a 20s tick.
  const pollJobs = useCallback(async () => {
    try {
      setImp(await api<ImportStatus>('GET', '/api/datamgmt/import'))
      setPull(await api<PullStatus>('GET', '/api/datamgmt/pull'))
    } catch {
      /* the page's own error line owns real failures; a poll miss is noise */
    }
  }, [])
  const running = imp?.state === 'running' || pull?.state === 'running'
  useEffect(() => {
    pollJobs()
    const t = window.setInterval(pollJobs, running ? 2_500 : 15_000)
    return () => window.clearInterval(t)
  }, [pollJobs, running])

  const loadSk = useCallback(async () => {
    try {
      setSk(await api<SysKey>('GET', '/api/syskey'))
      setAuto(await api<Autostart>('GET', '/api/datamgmt/autostart'))
    } catch {
      /* the page's error line owns real failures */
    }
  }, [])

  const toggleAuto = async (on: boolean) => {
    setError(null)
    try {
      setAuto(await api<Autostart>(on ? 'POST' : 'DELETE', '/api/datamgmt/autostart'))
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    }
  }
  useEffect(() => {
    loadSk()
  }, [loadSk])

  const checkKey = async () => {
    setSkBusy(true)
    setError(null)
    setProbe(null)
    try {
      setProbe(await api<Probe>('POST', '/api/syskey/probe', {
        key_id: skId.trim(), secret_key: skSecret.trim()
      }))
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setSkBusy(false)
    }
  }

  const storeKey = async (acceptLive: boolean) => {
    setSkBusy(true)
    setError(null)
    try {
      await api('POST', '/api/syskey', {
        key_id: skId.trim(), secret_key: skSecret.trim(), accept_live_risk: acceptLive
      })
      setSkId(''); setSkSecret(''); setProbe(null)
      await loadSk()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setSkBusy(false)
    }
  }

  const removeKey = async () => {
    if (!confirm('Remove the recording key? Unattended recording will stop.')) return
    try {
      await api('DELETE', '/api/syskey')
      await loadSk()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    }
  }

  const pickAndImport = async () => {
    setError(null)
    const path = await window.grindstone.pickFile()
    if (!path) return
    try {
      setImp(await api<ImportStatus>('POST', '/api/datamgmt/import', {
        path, kind: impKind, underlying: impSym.trim().toUpperCase() || 'SPY'
      }))
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    }
  }

  const startPull = async () => {
    setError(null)
    try {
      setPull(await api<PullStatus>('POST', '/api/datamgmt/pull', {
        symbols: pullSyms.split(/[,\s]+/).filter(Boolean),
        start: pullFrom || pull?.window_from,
        end: pullTo || pull?.window_to,
        seconds_between: 6
      }))
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    }
  }

  const stopPull = async () => {
    try {
      setPull(await api<PullStatus>('DELETE', '/api/datamgmt/pull'))
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    }
  }

  const add = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await api('POST', '/api/datamgmt/jobs', {
        ...form,
        symbol: form.kind === 'news' && !form.symbol ? '' : form.symbol.toUpperCase().trim(),
        timeframe: form.kind === 'bars' ? form.timeframe : '',
      })
      setForm({ ...EMPTY })
      await refresh()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const toggle = async (j: RecordJob) => {
    try {
      await api('PATCH', `/api/datamgmt/jobs/${j.id}`, { enabled: !j.enabled })
      await refresh()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    }
  }
  const remove = async (j: RecordJob) => {
    if (!confirm(`Delete this ${j.kind} job? Recorded data is kept.`)) return
    try {
      await api('DELETE', `/api/datamgmt/jobs/${j.id}`)
      await refresh()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    }
  }

  return (
    <div className="page">
      <div className="page-head">
        <DataIcon />
        <h1>Data management</h1>
        {usage ? <span className="dim">store: {bytes(usage.db_bytes)}</span> : null}
      </div>

      <div className="card">
        <h2>Coverage &amp; backfill</h2>
        <div className="subtle" style={{ marginBottom: 10 }}>
          What is actually on disk for a symbol, and how much history is still
          missing. A day the provider positively reports as empty — a holiday,
          or a date before the symbol listed — is recorded as such and never
          asked for again; a day that <em>failed</em> stays on the list.
        </div>
        <div className="row" style={{ gap: 8, marginBottom: 10 }}>
          <input
            className="field" placeholder="Symbol (e.g. SPY)" value={covSym}
            onChange={(e) => setCovSym(e.target.value.toUpperCase())}
            style={{ maxWidth: 160 }}
          />
          <button className="btn" onClick={() => void loadCoverage()}
                  disabled={!covSym.trim()}>Check coverage</button>
        </div>
        {covErr ? <div className="test-result bad">{covErr}</div> : null}
        {cov ? (
          <div className="np-rows">
            <div className="np-row"><span className="dim">have</span>
              <span>{cov.have} day(s){cov.first ? ` · ${cov.first} → ${cov.last}` : ''}</span></div>
            <div className="np-row"><span className="dim">reported empty</span>
              <span>{cov.absent} day(s) — holidays and pre-listing dates</span></div>
            <div className="np-row"><span className="dim">failed</span>
              <span>{cov.failed} day(s) — these are retried</span></div>
            <div className="np-row"><span className="dim">still missing</span>
              <span>
                {cov.remaining} day(s) in {cov.window?.start} → {cov.window?.end}
              </span></div>
            {plan ? (
              <div className="np-row"><span className="dim">next run would fetch</span>
                <span>
                  {plan.planned_days} day(s) in {plan.chunks.length} request(s)
                  {/* NO SILENT CAPS: a run that cannot cover the whole gap
                      must say so, or a partial backfill reads as finished. */}
                  {plan.truncated
                    ? ` — capped; ${plan.remaining_days - plan.planned_days} more after that`
                    : ''}
                  {plan.enabled ? '' : ' (backfill is off in Settings)'}
                </span></div>
            ) : null}
          </div>
        ) : null}
      </div>

      <div className="card">
        <h2>Recording jobs</h2>
        <div className="subtle" style={{ marginBottom: 10 }}>
          Jobs run while you are signed in (your keys stay in the encrypted vault — recording
          pauses when locked). Options chains use Alpaca's indicative feed and are labeled as such.
          Futures option chains (e.g. /ES) record through a TastyTrade account — greeks are blank
          on those rows because the snapshot feed carries none. Futures bars have no source yet.
        </div>
        {jobs === null ? (
          <div className="dim">Loading…</div>
        ) : jobs.length === 0 ? (
          <div className="dim">No jobs yet — add one below.</div>
        ) : (
          jobs.map((j) => (
            <div className="acct-row" key={j.id}>
              <span className={`badge ${j.enabled ? 'paper' : 'data'}`}>
                {j.enabled ? 'on' : 'off'}
              </span>
              <strong>
                {j.kind}
                {j.symbol ? ` · ${j.symbol}` : ''}
                {j.timeframe ? ` · ${j.timeframe}` : ''}
              </strong>
              <span className="subtle">
                {INTERVALS.find((i) => i.s === j.interval_seconds)?.label ?? `${j.interval_seconds}s`}
                {' · keep '}
                {j.retention_days}d
              </span>
              <span
                className="subtle"
                style={{ color: j.last_status === 'ok' ? 'var(--gain)' : undefined }}
              >
                {j.last_run_at ? `${j.last_status} · ${j.last_rows} rows` : 'not yet run'}
              </span>
              <div className="row-actions">
                <button className="btn" onClick={() => toggle(j)}>
                  {j.enabled ? 'Pause' : 'Resume'}
                </button>
                <button className="btn" onClick={() => remove(j)}>
                  Delete
                </button>
              </div>
            </div>
          ))
        )}
      </div>

      <form className="card" onSubmit={add}>
        <h2>Add a recording job</h2>
        <div className="form-grid">
          <label>What</label>
          <select
            className="field"
            value={form.kind}
            onChange={(e) => setForm({ ...form, kind: e.target.value })}
          >
            {KINDS.map((k) => (
              <option key={k.key} value={k.key}>
                {k.label}
              </option>
            ))}
          </select>
          <label>{form.kind === 'news' ? 'Symbol (optional)' : 'Symbol'}</label>
          <input
            className="field"
            placeholder={form.kind === 'chain' ? 'SPY' : form.kind === 'news' ? 'blank = all news' : 'SPY'}
            value={form.symbol}
            onChange={(e) => setForm({ ...form, symbol: e.target.value })}
            spellCheck={false}
          />
          {form.kind === 'bars' ? (
            <>
              <label>Timeframe</label>
              <select
                className="field"
                value={form.timeframe}
                onChange={(e) => setForm({ ...form, timeframe: e.target.value })}
              >
                {TIMEFRAMES.map((t) => (
                  <option key={t}>{t}</option>
                ))}
              </select>
            </>
          ) : null}
          <label>Record</label>
          <select
            className="field"
            value={form.interval_seconds}
            onChange={(e) => setForm({ ...form, interval_seconds: Number(e.target.value) })}
          >
            {INTERVALS.map((i) => (
              <option key={i.s} value={i.s}>
                {i.label}
              </option>
            ))}
          </select>
          <label>Keep for</label>
          <select
            className="field"
            value={form.retention_days}
            onChange={(e) => setForm({ ...form, retention_days: Number(e.target.value) })}
          >
            {[7, 30, 90, 365, 1825].map((d) => (
              <option key={d} value={d}>
                {d >= 365 ? `${d / 365} year${d > 365 ? 's' : ''}` : `${d} days`}
              </option>
            ))}
          </select>
        </div>
        {error ? <div className="test-result bad">{error}</div> : null}
        <button className="btn primary" disabled={busy} type="submit">
          {busy ? 'Adding…' : 'Add job'}
        </button>
      </form>

      <div className="card">
        <h2>Recording key</h2>
        <div className="subtle" style={{ marginBottom: 10 }}>
          A separate broker key for background recording, sealed to your Windows
          account so the recorder can run without you signing in. Use a{' '}
          <strong>paper</strong> key — it cannot move real money. Your trading
          keys stay in the encrypted vault and are never used for this. The key is
          Alpaca-shaped, so unattended mode covers <strong>equity</strong> jobs only:
          futures chain jobs (TastyTrade) record while you are signed in and report
          “locked” otherwise.
        </div>

        {sk?.enrolled ? (
          <>
            <div className={`acct-row${sk.readable ? '' : ' bad'}`}>
              <span className={`badge ${sk.readable ? 'paper' : 'data'}`}>
                {sk.readable ? sk.verdict === 'PAPER_ONLY' ? 'paper only' : 'live capable' : 'unreadable'}
              </span>
              <strong>key ····{sk.key_id_last4}</strong>
              <span className="subtle">
                {sk.readable
                  ? `enrolled ${(sk.enrolled_at ?? '').slice(0, 10)}`
                  : 'this key was sealed on a different Windows account or before a password reset — enter it again'}
              </span>
              <button className="btn" onClick={removeKey}>Remove</button>
            </div>

            {sk.readable && auto?.supported ? (
              <div className="acct-row">
                <label className="opt-toggle">
                  <input
                    type="checkbox"
                    checked={auto.registered}
                    onChange={(e) => toggleAuto(e.target.checked)}
                  />
                  Record automatically
                </label>
                <span className="subtle">
                  {auto.registered
                    ? `on — ${auto.note}${auto.method === 'startup-folder'
                        ? ' (via your Startup folder; a scheduled task needs administrator rights on this PC)'
                        : ''}`
                    : 'off — recording only runs while the app is open and signed in'}
                </span>
              </div>
            ) : null}
          </>
        ) : null}

        {!sk?.platform_ok ? (
          <div className="subtle">
            Unattended recording needs Windows on this build. Elsewhere, sign in
            and the recorder runs while the app is open.
          </div>
        ) : (
          <>
            <div className="acct-row">
              <input
                className="field" value={skId} spellCheck={false}
                onChange={(e) => { setSkId(e.target.value); setProbe(null) }}
                placeholder="key id" style={{ maxWidth: 240 }}
              />
              <input
                className="field" type="password" value={skSecret} spellCheck={false}
                onChange={(e) => { setSkSecret(e.target.value); setProbe(null) }}
                placeholder="secret key" style={{ maxWidth: 300 }}
              />
              <button
                className="btn" onClick={checkKey}
                disabled={skBusy || !skId.trim() || !skSecret.trim()}
              >
                {skBusy ? 'checking…' : 'Check key'}
              </button>
            </div>

            {probe ? (
              <div className={`acct-row${probe.verdict === 'PAPER_ONLY' ? '' : ' bad'}`}>
                <span className={`badge ${probe.verdict === 'PAPER_ONLY' ? 'paper' : 'data'}`}>
                  {probe.verdict === 'PAPER_ONLY' ? 'paper only'
                    : probe.verdict === 'LIVE_CAPABLE' ? 'can trade' : 'unknown'}
                </span>
                <span className="subtle">{probe.detail}</span>
                {probe.verdict === 'PAPER_ONLY' ? (
                  <button className="btn" onClick={() => storeKey(false)} disabled={skBusy}>
                    Store it
                  </button>
                ) : probe.verdict === 'LIVE_CAPABLE' ? (
                  <button className="btn" onClick={() => storeKey(true)} disabled={skBusy}>
                    Store anyway
                  </button>
                ) : null}
              </div>
            ) : null}
          </>
        )}
      </div>

      <div className="card">
        <h2>Import a file</h2>
        <div className="subtle" style={{ marginBottom: 10 }}>
          CSV or JSON, from any broker export — an archived chain file imports
          unchanged. The file is validated completely before a single row is
          written, and a refusal names the line and the reason. Rows land in
          the backtest store, so imported data is immediately backtestable.
        </div>
        <div className="acct-row">
          <select className="field" value={impKind} onChange={(e) => setImpKind(e.target.value)}>
            <option value="option_chain">Option chains</option>
            <option value="bars">Price bars</option>
          </select>
          <input
            className="field"
            style={{ maxWidth: 110 }}
            value={impSym}
            onChange={(e) => setImpSym(e.target.value)}
            placeholder="SPY"
            spellCheck={false}
            title="Which underlying's store to write. One symbol per file."
          />
          <button className="btn" onClick={pickAndImport} disabled={imp?.state === 'running'}>
            {imp?.state === 'running' ? 'importing…' : 'Choose a file…'}
          </button>
        </div>
        {imp && imp.state !== 'idle' ? (
          <div className={`acct-row${imp.state === 'refused' || imp.state === 'error' ? ' bad' : ''}`}>
            <strong>{imp.file}</strong>
            <span className="subtle">
              {imp.state === 'running'
                ? `importing… ${imp.done ?? 0}/${imp.total ?? '?'} days`
                : imp.state === 'done'
                  ? `imported ${(imp.contracts ?? imp.bars ?? 0).toLocaleString()} rows` +
                    (imp.days ? ` over ${imp.days} days` : '')
                  : imp.state === 'refused'
                    ? `refused — ${imp.reason}`
                    : `failed — ${imp.error}`}
            </span>
          </div>
        ) : null}
      </div>

      <div className="card">
        <h2>Chain puller</h2>
        <div className="subtle" style={{ marginBottom: 10 }}>
          Downloads end-of-day option chains, one ticker-day every 6 seconds,
          from a free service that asks nothing in return — so it goes slowly on
          purpose. OFF unless you start it.
          {pull ? (
            <>
              {' '}It can only reach back to <strong>{pull.window_from}</strong>{' '}
              (a rolling ~6 months) and stops at <strong>{pull.window_to}</strong>,
              because an open session has no greeks yet.
            </>
          ) : null}
        </div>
        <div className="acct-row">
          <input
            className="field"
            value={pullSyms}
            onChange={(e) => setPullSyms(e.target.value)}
            placeholder="SPY QQQ"
            spellCheck={false}
            style={{ maxWidth: 180 }}
          />
          <input className="field" type="date" value={pullFrom}
            min={pull?.window_from} max={pull?.window_to}
            onChange={(e) => setPullFrom(e.target.value)} />
          <input className="field" type="date" value={pullTo}
            min={pull?.window_from} max={pull?.window_to}
            onChange={(e) => setPullTo(e.target.value)} />
          {pull?.state === 'running' ? (
            <button className="btn" onClick={stopPull}>Stop</button>
          ) : (
            <button className="btn" onClick={startPull}>Start</button>
          )}
        </div>
        {pull && pull.state !== 'idle' ? (
          <div className={`acct-row${pull.state === 'error' ? ' bad' : ''}`}>
            <strong>
              {pull.state === 'running' ? 'pulling' : pull.state}
              {pull.total ? ` ${pull.done ?? 0}/${pull.total}` : ''}
            </strong>
            <span className="subtle">
              {pull.state === 'error'
                ? pull.error
                : `${(pull.written ?? 0).toLocaleString()} contracts stored` +
                  (pull.skipped ? `, ${pull.skipped} days skipped` : '') +
                  (pull.state === 'running' && pull.total
                    ? ` · about ${hhmm(((pull.total ?? 0) - (pull.done ?? 0)) * (pull.delay ?? 6))} left`
                    : '')}
              {pull.last ? ` · ${pull.last}` : ''}
            </span>
          </div>
        ) : null}
        {pull?.note ? <div className="subtle">{pull.note}</div> : null}
      </div>

      {usage ? (
        <div className="card">
          <h2>What's stored</h2>
          {usage.chain.map((c) => (
            <div className="acct-row" key={c.underlying}>
              <strong>{c.underlying} chains</strong>
              <span className="subtle">
                {c.snapshots} snapshots · {c.n.toLocaleString()} contract rows ·{' '}
                {c.oldest?.slice(0, 10)} → {c.newest?.slice(0, 10)}
              </span>
            </div>
          ))}
          {usage.bars.map((b) => (
            <div className="acct-row" key={`${b.symbol}-${b.timeframe}`}>
              <strong>
                {b.symbol} {b.timeframe} bars
              </strong>
              <span className="subtle">
                {b.n.toLocaleString()} bars · {b.oldest?.slice(0, 10)} → {b.newest?.slice(0, 10)}
              </span>
            </div>
          ))}
          <div className="acct-row">
            <strong>News</strong>
            <span className="subtle">
              {usage.news.count.toLocaleString()} articles
              {usage.news.oldest ? ` · ${usage.news.oldest.slice(0, 10)} → ${usage.news.newest?.slice(0, 10)}` : ''}
            </span>
          </div>
        </div>
      ) : null}
    </div>
  )
}
