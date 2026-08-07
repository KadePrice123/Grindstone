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
  kind: 'bool' | 'float' | 'choice' | 'json' | 'path'
  label: string
  help: string
  default: unknown
  min?: number
  max?: number
  step?: number
  choices?: string[]
  /** State blobs owned by other UIs (multi-chart layout) — not rendered here. */
  hidden?: boolean
  /** Which card this belongs under. The page used to render EVERY setting
   *  inside one card headed "Search", so the theme, the chart depth and the
   *  backtest database paths all sat under a heading that described none of
   *  them — and any new setting inherited the mislabel. */
  group?: string
}

/** Settings by card, in a declared order, with anything unclassified kept
 *  in "Other" rather than dropped. A control the page silently refuses to
 *  render is indistinguishable from a setting that does not exist. */
const GROUP_ORDER = ['Search', 'Charts', 'Data', 'Backtesting', 'Appearance', 'Other']

function groupsOf(schema: SettingSpec[]): [string, SettingSpec[]][] {
  const by = new Map<string, SettingSpec[]>()
  for (const s of schema.filter((x) => !x.hidden)) {
    const g = s.group || 'Other'
    if (!by.has(g)) by.set(g, [])
    by.get(g)!.push(s)
  }
  return [...by.entries()].sort(
    (a, b) => (GROUP_ORDER.indexOf(a[0]) + 1 || 99) - (GROUP_ORDER.indexOf(b[0]) + 1 || 99)
  )
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
        <>
        {groupsOf(data.schema).map(([group, rows]) => (
        <div className="card" key={group}>
          <h2>{group}</h2>
          {rows.map((s) => {
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
                  ) : s.kind === 'path' ? (
                    <input
                      className="field"
                      style={{ minWidth: 280 }}
                      // Keyed on the stored value: a failed save reloads the
                      // truth, and the remount discards the rejected text
                      // instead of displaying it as if it stuck.
                      key={`${s.key}:${String(value ?? '')}`}
                      defaultValue={String(value ?? '')}
                      placeholder="auto"
                      spellCheck={false}
                      // Commit on blur/Enter, not per keystroke — a path is
                      // typed in one go and half a path is never valid.
                      onBlur={(e) => {
                        if (e.target.value !== String(value ?? '')) save(s.key, e.target.value)
                      }}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
                      }}
                    />
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
          {group === 'Search' && data.web ? (
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
        ))}
        </>
      )}

      <GesturesPanel />
    </div>
  )
}
