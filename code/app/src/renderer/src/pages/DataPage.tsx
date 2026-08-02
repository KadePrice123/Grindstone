import { FormEvent, useCallback, useEffect, useState } from 'react'
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

export function DataPage({ onBack }: { onBack: () => void }) {
  const [jobs, setJobs] = useState<RecordJob[] | null>(null)
  const [usage, setUsage] = useState<DataUsage | null>(null)
  const [form, setForm] = useState({ ...EMPTY })
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const refresh = useCallback(async () => {
    try {
      setJobs(await api<RecordJob[]>('GET', '/api/datamgmt/jobs'))
      setUsage(await api<DataUsage>('GET', '/api/datamgmt/usage'))
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    }
  }, [])

  useEffect(() => {
    refresh()
    const t = window.setInterval(refresh, 20_000)
    return () => window.clearInterval(t)
  }, [refresh])

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
    await api('PATCH', `/api/datamgmt/jobs/${j.id}`, { enabled: !j.enabled })
    await refresh()
  }
  const remove = async (j: RecordJob) => {
    if (!confirm(`Delete this ${j.kind} job? Recorded data is kept.`)) return
    await api('DELETE', `/api/datamgmt/jobs/${j.id}`)
    await refresh()
  }

  return (
    <div className="page">
      <div className="page-head">
        <button className="back" onClick={onBack} title="Back">
          ←
        </button>
        <DataIcon />
        <h1>Data management</h1>
        {usage ? <span className="dim">store: {bytes(usage.db_bytes)}</span> : null}
      </div>

      <div className="card">
        <h2>Recording jobs</h2>
        <div className="subtle" style={{ marginBottom: 10 }}>
          Jobs run while you are signed in (your keys stay in the encrypted vault — recording
          pauses when locked). Options chains use Alpaca's indicative feed and are labeled as such.
          Futures recording arrives with the TastyTrade adapter.
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
