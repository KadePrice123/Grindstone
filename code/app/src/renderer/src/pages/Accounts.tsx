import { FormEvent, useCallback, useEffect, useState } from 'react'
import { AccountRow, TestResult, api, ApiError } from '../api'
import { AccountsIcon } from '../components/icons'

const EMPTY_FORM = { kind: 'paper', nickname: '', key_id: '', secret_key: '' }

export function Accounts() {
  const [rows, setRows] = useState<AccountRow[] | null>(null)
  const [form, setForm] = useState({ ...EMPTY_FORM })
  const [test, setTest] = useState<TestResult | null>(null)
  const [busy, setBusy] = useState<'test' | 'save' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [rowTest, setRowTest] = useState<Record<number, TestResult>>({})

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

  const credentials = () => ({ key_id: form.key_id.trim(), secret_key: form.secret_key.trim() })

  const runTest = async () => {
    setBusy('test')
    setTest(null)
    setError(null)
    try {
      setTest(
        await api<TestResult>('POST', '/api/accounts/test', {
          broker: 'alpaca',
          kind: form.kind,
          credentials: credentials(),
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
        broker: 'alpaca',
        kind: form.kind,
        nickname: form.nickname.trim() || `Alpaca ${form.kind}`,
        credentials: credentials(),
      })
      setForm({ ...EMPTY_FORM })
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
        [id]: { ok: false, error: e instanceof ApiError ? e.message : String(e) },
      }))
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
              <div className="acct-row" key={r.id}>
                <span className={`badge ${r.kind}`}>{r.kind}</span>
                <strong>{r.nickname}</strong>
                <span className="subtle">
                  {r.broker}
                  {r.key_hints.key_id ? ` · key …${r.key_hints.key_id}` : ''}
                </span>
                {t ? (
                  <span className={t.ok ? 'subtle' : 'error-text'} style={{ fontSize: 12 }}>
                    {t.ok
                      ? `✓ ${t.status ?? t.detail ?? 'ok'}${t.equity != null ? ` · equity $${t.equity.toLocaleString()}` : ''}`
                      : (t.error ?? t.detail ?? '…')}
                  </span>
                ) : null}
                <div className="row-actions">
                  <button className="btn" onClick={() => testSaved(r.id)}>
                    Test
                  </button>
                  <button className="btn" onClick={() => remove(r.id)}>
                    Delete
                  </button>
                </div>
              </div>
            )
          })
        )}
      </div>

      <form className="card" onSubmit={save}>
        <h2>Add an Alpaca account</h2>
        <div className="form-grid">
          <label>Type</label>
          <select
            className="field"
            value={form.kind}
            onChange={(e) => setForm({ ...form, kind: e.target.value })}
          >
            <option value="paper">Paper</option>
            <option value="live">Live</option>
            <option value="data">Data only</option>
          </select>
          <label>Nickname</label>
          <input
            className="field"
            placeholder={`Alpaca ${form.kind}`}
            value={form.nickname}
            onChange={(e) => setForm({ ...form, nickname: e.target.value })}
          />
          <label>API key ID</label>
          <input
            className="field"
            value={form.key_id}
            onChange={(e) => setForm({ ...form, key_id: e.target.value })}
            spellCheck={false}
          />
          <label>API secret</label>
          <input
            className="field"
            type="password"
            value={form.secret_key}
            onChange={(e) => setForm({ ...form, secret_key: e.target.value })}
          />
        </div>
        {test ? (
          <div className={`test-result ${test.ok ? 'ok' : 'bad'}`}>
            {test.ok
              ? `Connected — status ${test.status ?? 'OK'}${
                  test.equity != null ? `, equity $${test.equity.toLocaleString()}` : ''
                }${test.options_level != null ? `, options level ${test.options_level}` : ''}`
              : `Failed — ${test.error ?? 'unknown error'}`}
          </div>
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
            disabled={busy !== null || !form.key_id || !form.secret_key}
            onClick={runTest}
          >
            {busy === 'test' ? 'Testing…' : 'Test connection'}
          </button>
          <button
            type="submit"
            className="btn primary"
            disabled={busy !== null || !form.key_id || !form.secret_key}
          >
            {busy === 'save' ? 'Saving…' : 'Save (encrypted)'}
          </button>
        </div>
        <div className="subtle" style={{ marginTop: 10 }}>
          Keys are encrypted with your login password before they touch disk; only the last four
          characters stay readable.
        </div>
      </form>
    </div>
  )
}
