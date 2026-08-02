/**
 * Settings — rendered from the backend's own declaration, so adding a
 * setting is a one-place change (backend/settings.py SPEC).
 */
import { useCallback, useEffect, useState } from 'react'
import { api, ApiError } from '../api'
import { GesturesPanel } from '../components/GesturesPanel'
import { SettingsIcon } from '../components/icons'

interface SettingSpec {
  key: string
  kind: 'bool' | 'float' | 'choice' | 'json'
  label: string
  help: string
  default: unknown
  min?: number
  max?: number
  step?: number
  choices?: string[]
  /** State blobs owned by other UIs (multi-chart layout) — not rendered here. */
  hidden?: boolean
}

interface SettingsPayload {
  values: Record<string, unknown>
  schema: SettingSpec[]
  web?: { installed: boolean; available: boolean }
}

export function SettingsPage() {
  const [data, setData] = useState<SettingsPayload | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    try {
      setData(await api<SettingsPayload>('GET', '/api/settings'))
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const save = async (key: string, value: unknown) => {
    setData((d) => (d ? { ...d, values: { ...d.values, [key]: value } } : d)) // optimistic
    setSaving(true)
    try {
      const res = await api<{ values: Record<string, unknown> }>('PUT', '/api/settings', {
        [key]: value,
      })
      setData((d) => (d ? { ...d, values: res.values } : d))
      setError(null)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
      load() // put the truth back on screen
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="page">
      <div className="page-head">
        <SettingsIcon />
        <h1>Settings</h1>
        {saving ? <span className="subtle">saving…</span> : null}
      </div>

      {error ? <div className="test-result bad">{error}</div> : null}

      {data == null ? (
        <div className="card dim">Loading…</div>
      ) : (
        <div className="card">
          <h2>Search</h2>
          {data.schema.filter((s) => !s.hidden).map((s) => {
            const value = data.values[s.key]
            return (
              <div className="setting-row" key={s.key}>
                <div className="setting-text">
                  <div>{s.label}</div>
                  <div className="subtle">{s.help}</div>
                </div>
                <div className="setting-control">
                  {s.kind === 'bool' ? (
                    <button
                      className={`toggle${value ? ' on' : ''}`}
                      onClick={() => save(s.key, !value)}
                      title={value ? 'On' : 'Off'}
                    >
                      <span className="knob" />
                    </button>
                  ) : s.kind === 'choice' ? (
                    <select
                      className="field"
                      value={String(value)}
                      onChange={(e) => save(s.key, e.target.value)}
                    >
                      {s.choices?.map((c) => (
                        <option key={c} value={c}>
                          {c}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <div className="slider-wrap">
                      <input
                        type="range"
                        min={s.min}
                        max={s.max}
                        step={s.step}
                        value={Number(value)}
                        onChange={(e) => save(s.key, Number(e.target.value))}
                      />
                      <span className="slider-val">{Number(value).toFixed(2)}</span>
                    </div>
                  )}
                </div>
              </div>
            )
          })}
          {data.web ? (
            <div className="subtle" style={{ marginTop: 12 }}>
              Web search backend:{' '}
              {!data.web.installed
                ? 'not installed'
                : data.web.available
                  ? 'ready (DuckDuckGo)'
                  : 'temporarily paused after repeated failures — it retries automatically'}
            </div>
          ) : null}
        </div>
      )}

      <GesturesPanel />
    </div>
  )
}
