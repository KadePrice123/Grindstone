/**
 * The gesture-wheel editor on the Settings page (FR-SHELL-4).
 *
 * One wheel shown at a time with arrows to page through — the "wheel select
 * panel". The preview is the SAME WheelFace component the overlay renders,
 * so editing is literal WYSIWYG: change a segment, watch the wheel change.
 * The whole document saves through PUT /api/wheels, which validates and
 * says exactly what it rejected.
 */
import { useCallback, useEffect, useState } from 'react'
import { api, ApiError } from '../api'
import { Quote, WheelConfig, WheelFace, WheelRender, WheelSegment } from './WheelFace'

interface WheelDoc {
  config: WheelConfig
  wheels: (WheelRender & { builtin?: boolean; dynamic?: string })[]
}

const SEG_TYPES: { key: WheelSegment['type']; label: string }[] = [
  { key: 'wheel', label: 'Go to wheel' },
  { key: 'nav', label: 'Page' },
  { key: 'tool', label: 'Tool' },
  { key: 'ticker', label: 'Ticker' },
  { key: 'placeholder', label: 'Placeholder' },
]
const ROUTES = ['idle', 'accounts', 'data', 'settings', 'news']
const TOOLS = ['search']

export function GesturesPanel() {
  const [doc, setDoc] = useState<WheelDoc | null>(null)
  const [idx, setIdx] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    try {
      setDoc(await api<WheelDoc>('GET', '/api/wheels'))
      setError(null)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  /** Persist the whole document; the backend echoes the validated truth. */
  const save = async (next: WheelDoc) => {
    setDoc(next) // optimistic — the preview must track the edit instantly
    setSaving(true)
    try {
      setDoc(await api<WheelDoc>('PUT', '/api/wheels', next))
      setError(null)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
      load() // the stored truth back on screen
    } finally {
      setSaving(false)
    }
  }

  if (doc == null) {
    return (
      <div className="card">
        <h2>Gesture wheels</h2>
        <div className="dim">{error ?? 'Loading…'}</div>
      </div>
    )
  }

  const wheel = doc.wheels[Math.min(idx, doc.wheels.length - 1)]
  const isDynamic = wheel.dynamic === 'tabs'

  const patchWheel = (patch: Partial<WheelRender>) =>
    save({
      ...doc,
      wheels: doc.wheels.map((w) => (w.id === wheel.id ? { ...w, ...patch } : w)),
    })

  const patchSegment = (i: number, seg: WheelSegment) =>
    patchWheel({ segments: wheel.segments.map((s, j) => (j === i ? seg : s)) })

  const moveSegment = (i: number, dir: -1 | 1) => {
    const j = (i + dir + wheel.segments.length) % wheel.segments.length
    const next = [...wheel.segments]
    ;[next[i], next[j]] = [next[j], next[i]]
    patchWheel({ segments: next })
  }

  const addWheel = () => {
    let n = 1
    while (doc.wheels.some((w) => w.id === `custom${n}`)) n++
    const id = `custom${n}`
    save({
      ...doc,
      wheels: [
        ...doc.wheels,
        {
          id,
          name: `Wheel ${n}`,
          symbol: '✦',
          // A new wheel starts with 6 slots (spec): back-to-main plus five
          // placeholders the user replaces.
          segments: [
            { type: 'wheel', wheel: 'main', label: 'Main' },
            ...Array.from({ length: 5 }, () => ({
              type: 'placeholder' as const,
              label: 'Edit me',
            })),
          ],
        },
      ],
    })
    setIdx(doc.wheels.length)
  }

  const removeWheel = () => {
    if (wheel.builtin) return
    save({ ...doc, wheels: doc.wheels.filter((w) => w.id !== wheel.id) })
    setIdx(0)
  }

  const noQuotes: Record<string, Quote> = {}

  return (
    <div className="card">
      <h2>
        Gesture wheels
        {saving ? <span className="subtle"> · saving…</span> : null}
      </h2>
      <div className="subtle" style={{ marginBottom: 12 }}>
        Right-click anywhere: click for a menu you can left-click, or hold and
        drag over a segment and release. The center lock pins the shown wheel
        as your default.
      </div>

      {error ? <div className="test-result bad">{error}</div> : null}

      <div className="wheel-carousel">
        <button
          className="btn"
          title="Previous wheel"
          onClick={() => setIdx((idx - 1 + doc.wheels.length) % doc.wheels.length)}
        >
          ‹
        </button>
        <div className="wheel-preview">
          <WheelFace
            wheel={wheel}
            config={doc.config}
            quotes={noQuotes}
            hover={null}
            mode="preview"
            scale={0.72}
          />
          <div className="wheel-carousel-name">
            <span className="wheel-symbol">{wheel.symbol}</span>
            <strong>{wheel.name}</strong>
            <span className="subtle">
              {idx + 1}/{doc.wheels.length}
              {doc.config.locked === wheel.id ? ' · default' : ''}
            </span>
          </div>
        </div>
        <button
          className="btn"
          title="Next wheel"
          onClick={() => setIdx((idx + 1) % doc.wheels.length)}
        >
          ›
        </button>
      </div>

      <div className="wheel-edit-row">
        <label className="wheel-field">
          Name
          <input
            className="field"
            value={wheel.name}
            maxLength={24}
            onChange={(e) => patchWheel({ name: e.target.value || wheel.name })}
          />
        </label>
        <label className="wheel-field sm">
          Symbol
          <input
            className="field"
            value={wheel.symbol}
            maxLength={4}
            onChange={(e) => patchWheel({ symbol: e.target.value || wheel.symbol })}
          />
        </label>
        <div className="wheel-edit-actions">
          <button className="btn" onClick={addWheel} disabled={doc.wheels.length >= 12}>
            New wheel
          </button>
          <button
            className="btn"
            onClick={removeWheel}
            disabled={!!wheel.builtin}
            title={wheel.builtin ? 'Built-in wheels cannot be deleted' : 'Delete this wheel'}
          >
            Delete
          </button>
        </div>
      </div>

      {isDynamic ? (
        <div className="subtle" style={{ marginTop: 10 }}>
          The Tabs wheel builds itself from your open tabs — more than eight
          and its east/west segments page through them.
        </div>
      ) : (
        <div className="wheel-seg-list">
          {wheel.segments.map((seg, i) => (
            <div className="wheel-seg-row" key={i}>
              <span className="wheel-seg-pos subtle">{i === 0 ? '12 o’clock' : `#${i + 1}`}</span>
              <select
                className="field"
                value={seg.type}
                onChange={(e) => {
                  const t = e.target.value as WheelSegment['type']
                  patchSegment(i,
                    t === 'wheel' ? { type: t, wheel: 'main', label: 'Main' }
                    : t === 'nav' ? { type: t, route: 'idle', label: 'Home' }
                    : t === 'tool' ? { type: t, tool: 'search', label: 'Search' }
                    : t === 'ticker' ? { type: t, ticker: 'SPY', label: '' }
                    : { type: t, label: 'Soon' })
                }}
              >
                {SEG_TYPES.map((t) => (
                  <option key={t.key} value={t.key}>
                    {t.label}
                  </option>
                ))}
              </select>
              {seg.type === 'wheel' ? (
                <select
                  className="field"
                  value={seg.wheel}
                  onChange={(e) => patchSegment(i, { ...seg, wheel: e.target.value })}
                >
                  {doc.wheels.map((w) => (
                    <option key={w.id} value={w.id}>
                      {w.name}
                    </option>
                  ))}
                </select>
              ) : seg.type === 'nav' ? (
                <select
                  className="field"
                  value={seg.route}
                  onChange={(e) => patchSegment(i, { ...seg, route: e.target.value })}
                >
                  {ROUTES.map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </select>
              ) : seg.type === 'tool' ? (
                <select
                  className="field"
                  value={seg.tool}
                  onChange={(e) => patchSegment(i, { ...seg, tool: e.target.value })}
                >
                  {TOOLS.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </select>
              ) : seg.type === 'ticker' ? (
                <input
                  className="field"
                  value={seg.ticker ?? ''}
                  maxLength={8}
                  onChange={(e) =>
                    patchSegment(i, { ...seg, ticker: e.target.value.toUpperCase() })
                  }
                />
              ) : (
                <span className="subtle wheel-seg-note">not built yet — shows dimmed</span>
              )}
              <input
                className="field"
                placeholder="Label"
                value={seg.label}
                maxLength={20}
                onChange={(e) => patchSegment(i, { ...seg, label: e.target.value })}
              />
              <button className="btn" title="Rotate counter-clockwise" onClick={() => moveSegment(i, -1)}>
                ↺
              </button>
              <button className="btn" title="Rotate clockwise" onClick={() => moveSegment(i, 1)}>
                ↻
              </button>
            </div>
          ))}
          <div className="wheel-seg-add">
            <button
              className="btn"
              disabled={wheel.segments.length >= 12}
              onClick={() =>
                patchWheel({
                  segments: [...wheel.segments, { type: 'placeholder', label: 'Edit me' }],
                })
              }
            >
              Add segment
            </button>
            <button
              className="btn"
              disabled={wheel.segments.length <= 2}
              onClick={() => patchWheel({ segments: wheel.segments.slice(0, -1) })}
            >
              Remove last
            </button>
          </div>
        </div>
      )}

      <div className="setting-row" style={{ marginTop: 14 }}>
        <div className="setting-text">
          <div>Ticker segments show</div>
          <div className="subtle">Right in the wheel: today’s move, or the live price.</div>
        </div>
        <div className="setting-control">
          <select
            className="field"
            value={doc.config.ticker_display}
            onChange={(e) =>
              save({
                ...doc,
                config: { ...doc.config, ticker_display: e.target.value as 'percent' | 'price' },
              })
            }
          >
            <option value="percent">% change</option>
            <option value="price">price</option>
          </select>
        </div>
      </div>
      <div className="setting-row">
        <div className="setting-text">
          <div>Color by day direction</div>
          <div className="subtle">
            Green bullish, red bearish — frozen while the wheel is open, so it
            never flashes.
          </div>
        </div>
        <div className="setting-control">
          <button
            className={`toggle${doc.config.ticker_colors ? ' on' : ''}`}
            onClick={() =>
              save({
                ...doc,
                config: { ...doc.config, ticker_colors: !doc.config.ticker_colors },
              })
            }
          >
            <span className="knob" />
          </button>
        </div>
      </div>
    </div>
  )
}
