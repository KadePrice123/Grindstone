import { Fragment, FormEvent, useCallback, useEffect, useState } from 'react'
import { AccountRow, TestResult, api, ApiError } from '../api'
import { AccountsIcon } from '../components/icons'

// Per-broker credential fields, mirroring backend brokers/base.py
// CREDENTIAL_FIELDS. The refresh token is a secret and re-enterable: a
// TastyTrade grant regenerated on my.tastytrade.com invalidates the old one.
const BROKERS = {
  alpaca: {
    label: 'Alpaca',
    kinds: ['paper', 'live', 'data'] as const,
    fields: [
      { key: 'key_id', label: 'API key ID', secret: false },
      { key: 'secret_key', label: 'API secret', secret: true }
    ],
    note: 'Keys are encrypted with your login password before they touch disk; only the last four characters stay readable.'
  },
  tastytrade: {
    label: 'TastyTrade',
    kinds: ['data', 'live'] as const,
    fields: [
      { key: 'client_secret', label: 'Client secret', secret: true },
      { key: 'refresh_token', label: 'Refresh token', secret: true }
    ],
    note: 'A personal OAuth grant (my.tastytrade.com → API access). Both values are encrypted; nothing stays readable. If you regenerate the grant, use “Update token” on the saved account — no need to re-add it. Futures and futures options data come through this account.'
  }
} as const

type BrokerKey = keyof typeof BROKERS

const emptyForm = (broker: BrokerKey) => ({
  kind: BROKERS[broker].kinds[0] as string,
  nickname: '',
  values: Object.fromEntries(BROKERS[broker].fields.map((f) => [f.key, ''])) as Record<
    string,
    string
  >
})

function testSummary(t: TestResult): string {
  if (!t.ok) return `Failed — ${t.error ?? 'unknown error'}`
  const bits: string[] = ['Connected']
  if (t.status) bits.push(`status ${t.status}`)
  if (t.equity != null) bits.push(`equity $${t.equity.toLocaleString()}`)
  if (t.accounts != null)
    bits.push(`${t.accounts} account${t.accounts === 1 ? '' : 's'}`)
  if (t.account_last4) bits.push(`…${t.account_last4}`)
  if (t.margin_or_cash) bits.push(t.margin_or_cash.toLowerCase())
  if (t.futures_approved != null)
    bits.push(t.futures_approved ? 'futures approved' : 'no futures approval')
  if (t.options_level != null) bits.push(`options: ${t.options_level}`)
  return bits.join(' · ')
}

export function Accounts() {
  const [rows, setRows] = useState<AccountRow[] | null>(null)
  const [broker, setBroker] = useState<BrokerKey>('alpaca')
  const [form, setForm] = useState(emptyForm('alpaca'))
  const [test, setTest] = useState<TestResult | null>(null)
  const [busy, setBusy] = useState<'test' | 'save' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [rowTest, setRowTest] = useState<Record<number, TestResult>>({})
  // id of the account whose token re-entry form is open, and its draft value
  const [rekeyId, setRekeyId] = useState<number | null>(null)
  const [rekeyValue, setRekeyValue] = useState('')
  const [rekeyBusy, setRekeyBusy] = useState(false)

  const refresh = useCallback(async () => {
    try {
      setRows(await api<AccountRow[]>('GET', '/api/accounts'))
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    }
  }, [])

  useEffect(() => {
    refresh()
  }, [refresh])

  const spec = BROKERS[broker]
  const filled = spec.fields.every((f) => form.values[f.key]?.trim())
  const credentials = () =>
    Object.fromEntries(spec.fields.map((f) => [f.key, form.values[f.key].trim()]))

  const pickBroker = (b: BrokerKey) => {
    setBroker(b)
    setForm(emptyForm(b))
    setTest(null)
    setError(null)
  }

  const runTest = async () => {
    setBusy('test')
    setTest(null)
    setError(null)
    try {
      setTest(
        await api<TestResult>('POST', '/api/accounts/test', {
          broker,
          kind: form.kind,
          credentials: credentials()
        })
      )
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  const save = async (e: FormEvent) => {
    e.preventDefault()
    setBusy('save')
    setError(null)
    try {
      await api('POST', '/api/accounts', {
        broker,
        kind: form.kind,
        nickname: form.nickname.trim() || `${spec.label} ${form.kind}`,
        credentials: credentials()
      })
      setForm(emptyForm(broker))
      setTest(null)
      await refresh()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(null)
    }
  }

  const remove = async (id: number) => {
    if (!confirm('Delete this account and its stored keys?')) return
    try {
      await api('DELETE', `/api/accounts/${id}`)
      await refresh()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    }
  }

  const testSaved = async (id: number) => {
    setRowTest((m) => ({ ...m, [id]: { ok: false, detail: '…' } as TestResult }))
    try {
      const r = await api<TestResult>('POST', `/api/accounts/${id}/test`)
      setRowTest((m) => ({ ...m, [id]: r }))
    } catch (e) {
      setRowTest((m) => ({
        ...m,
        [id]: { ok: false, error: e instanceof ApiError ? e.message : String(e) }
      }))
    }
  }

  const submitRekey = async (id: number) => {
    if (!rekeyValue.trim()) return
    setRekeyBusy(true)
    setError(null)
    try {
      await api('PUT', `/api/accounts/${id}/credentials`, {
        credentials: { refresh_token: rekeyValue.trim() }
      })
      setRekeyId(null)
      setRekeyValue('')
      // Prove the new token immediately — a token that saves but cannot mint
      // is exactly what the user came here to fix.
      await testSaved(id)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setRekeyBusy(false)
    }
  }

  return (
    <div className="page">
      <div className="page-head">
        <AccountsIcon />
        <h1>Accounts</h1>
      </div>

      <div className="card">
        <h2>Connected accounts</h2>
        {rows === null ? (
          <div className="dim">Loading…</div>
        ) : rows.length === 0 ? (
          <div className="dim">None yet — add your first below.</div>
        ) : (
          rows.map((r) => {
            const t = rowTest[r.id]
            return (
              <div key={r.id}>
                <div className="acct-row">
                  <span className={`badge ${r.kind}`}>{r.kind}</span>
                  <strong>{r.nickname}</strong>
                  <span className="subtle">
                    {r.broker}
                    {r.key_hints.key_id ? ` · key …${r.key_hints.key_id}` : ''}
                  </span>
                  {t ? (
                    <span className={t.ok ? 'subtle' : 'error-text'} style={{ fontSize: 12 }}>
                      {t.ok
                        ? `✓ ${t.detail ?? t.status ?? 'ok'}${t.equity != null ? ` · equity $${t.equity.toLocaleString()}` : ''}`
                        : (t.error ?? t.detail ?? '…')}
                    </span>
                  ) : null}
                  <div className="row-actions">
                    <button className="btn" onClick={() => testSaved(r.id)}>
                      Test
                    </button>
                    {r.broker === 'tastytrade' ? (
                      <button
                        className="btn"
                        onClick={() => {
                          setRekeyId(rekeyId === r.id ? null : r.id)
                          setRekeyValue('')
                        }}
                      >
                        Update token
                      </button>
                    ) : null}
                    <button className="btn" onClick={() => remove(r.id)}>
                      Delete
                    </button>
                  </div>
                </div>
                {rekeyId === r.id ? (
                  <div className="acct-row" style={{ gap: 10 }}>
                    <input
                      className="field"
                      type="password"
                      placeholder="New refresh token"
                      value={rekeyValue}
                      onChange={(e) => setRekeyValue(e.target.value)}
                      style={{ flex: 1 }}
                      spellCheck={false}
                    />
                    <button
                      className="btn primary"
                      disabled={rekeyBusy || !rekeyValue.trim()}
                      onClick={() => submitRekey(r.id)}
                    >
                      {rekeyBusy ? 'Saving…' : 'Save (encrypted)'}
                    </button>
                  </div>
                ) : null}
              </div>
            )
          })
        )}
      </div>

      <form className="card" onSubmit={save}>
        <h2>Add an account</h2>
        <div className="form-grid">
          <label>Brokerage</label>
          <select
            className="field"
            value={broker}
            onChange={(e) => pickBroker(e.target.value as BrokerKey)}
          >
            {(Object.keys(BROKERS) as BrokerKey[]).map((b) => (
              <option key={b} value={b}>
                {BROKERS[b].label}
              </option>
            ))}
          </select>
          <label>Type</label>
          <select
            className="field"
            value={form.kind}
            onChange={(e) => setForm({ ...form, kind: e.target.value })}
          >
            {spec.kinds.map((k) => (
              <option key={k} value={k}>
                {k === 'data' ? 'Data only' : k[0].toUpperCase() + k.slice(1)}
              </option>
            ))}
          </select>
          <label>Nickname</label>
          <input
            className="field"
            placeholder={`${spec.label} ${form.kind}`}
            value={form.nickname}
            onChange={(e) => setForm({ ...form, nickname: e.target.value })}
          />
          {spec.fields.map((f) => (
            <Fragment key={f.key}>
              <label>{f.label}</label>
              <input
                className="field"
                type={f.secret ? 'password' : 'text'}
                value={form.values[f.key]}
                onChange={(e) =>
                  setForm({ ...form, values: { ...form.values, [f.key]: e.target.value } })
                }
                spellCheck={false}
              />
            </Fragment>
          ))}
        </div>
        {test ? (
          <div className={`test-result ${test.ok ? 'ok' : 'bad'}`}>{testSummary(test)}</div>
        ) : null}
        {error ? (
          <div className="test-result bad" style={{ borderColor: 'var(--loss)' }}>
            {error}
          </div>
        ) : null}
        <div style={{ display: 'flex', gap: 10 }}>
          <button
            type="button"
            className="btn"
            disabled={busy !== null || !filled}
            onClick={runTest}
          >
            {busy === 'test' ? 'Testing…' : 'Test connection'}
          </button>
          <button type="submit" className="btn primary" disabled={busy !== null || !filled}>
            {busy === 'save' ? 'Saving…' : 'Save (encrypted)'}
          </button>
        </div>
        <div className="subtle" style={{ marginTop: 10 }}>
          {spec.note}
        </div>
      </form>
    </div>
  )
}
