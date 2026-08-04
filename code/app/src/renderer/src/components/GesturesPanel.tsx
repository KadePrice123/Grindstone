/**
 * The gesture-wheel editor on the Settings page (FR-SHELL-4, redesigned).
 *
 * Direct manipulation: click a segment ON the preview wheel, then pick what
 * it becomes from a searchable, category-filtered catalog (wheelCatalog.ts).
 * The old row-per-segment form made "change one slot" an eight-row hunt —
 * Kade: click the wheel segment you want to change, then select from a list
 * of tools with filters by category and search.
 *
 * The preview is the SAME WheelFace component the overlay renders, so
 * editing is literal WYSIWYG. The whole document saves through
 * PUT /api/wheels: optimistic local set for an instant preview, then the
 * backend's validated echo — or the stored truth reloaded on rejection,
 * with the actual reason shown.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../api'
import type { Favorite } from '../favorites'
import { CatalogEntry, CATEGORIES, favoriteEntry, searchCatalog } from '../wheelCatalog'
import { Quote, WheelConfig, WheelFace, WheelRender, WheelSegment } from './WheelFace'
import '../gestures.css'

interface WheelDoc {
  config: WheelConfig
  wheels: (WheelRender & { builtin?: boolean; dynamic?: string })[]
}

/** Dynamic wheels build themselves at spawn time — there is nothing stored
 *  to edit, and pretending otherwise would be a lie the overlay overwrites. */
const DYNAMIC_NOTE: Record<string, string> = {
  tabs:
    'The Tabs wheel builds itself from your open tabs — more than eight and ' +
    'its east/west segments page through them.',
  favorites:
    'The Favorites wheel builds itself from your starred pages — ticker ' +
    'favorites keep the quote display and day-direction colors, pages and ' +
    'sites just open. More than eight and it pages through them, like Tabs.',
  'chart-add':
    'Builds itself when spawned over a chart: every open ticker tab not ' +
    'already on that chart, ready to add.',
  'chart-ind':
    'Builds itself when spawned over a chart: the indicator toggles, marked ' +
    'with that chart’s current on/off state.',
  'chart-tickers':
    'Builds itself when spawned over a chart: that chart’s symbols, to hide ' +
    'or show.',
}

function posName(i: number): string {
  return i === 0 ? '12 o’clock' : `#${i + 1}`
}

/** The picker panel: search + category chips + the entry list. Remounted
 *  (keyed) per selected segment so search resets and autofocus fires. */
function SegmentPicker({
  doc,
  onPick,
}: {
  doc: WheelDoc
  onPick: (seg: WheelSegment) => void
}) {
  const [query, setQuery] = useState('')
  const [cat, setCat] = useState<string | null>(null)
  const [favs, setFavs] = useState<Favorite[] | null>(null)

  // Favorites are live, like the go-to-wheel entries below: fetched when the
  // picker opens, refreshed whenever a star toggles anywhere in the app.
  useEffect(() => {
    let live = true
    const load = () =>
      api<{ favorites: Favorite[] }>('GET', '/api/favorites')
        .then((r) => {
          if (live) setFavs(r.favorites)
        })
        .catch(() => {
          // offline blip: no rows beats a dead picker — the panel itself
          // already surfaces API errors when saving
          if (live) setFavs([])
        })
    load()
    const off = window.grindstone.onFavoritesChanged(load)
    return () => {
      live = false
      off()
    }
  }, [])

  // Go-to-wheel entries come from the LIVE document, not the static catalog —
  // custom wheels are offered the moment they exist.
  const q = query.trim().toLowerCase()
  const wheelEntries: CatalogEntry[] = doc.wheels
    .map((w) => ({
      id: `wheel:${w.id}`,
      category: 'Wheels',
      label: `Go to ${w.name}`,
      keywords: `wheel ${w.id}`,
      segment: { type: 'wheel' as const, wheel: w.id, label: w.name.slice(0, 20) },
    }))
    .filter(
      (e) =>
        (cat === null || cat === 'Wheels') &&
        (!q || e.label.toLowerCase().includes(q) || e.keywords.includes(q))
    )
  // Anything you have starred can sit on a wheel: a symbol favorite becomes
  // a ticker segment (same quote/color rules), a page or site a link segment.
  const favEntries: CatalogEntry[] = (favs ?? [])
    .map(favoriteEntry)
    .filter(
      (e) =>
        (cat === null || cat === 'Favorites') &&
        (!q || e.label.toLowerCase().includes(q) || e.keywords.includes(q))
    )
  const entries = [...searchCatalog(query, cat), ...wheelEntries, ...favEntries]

  return (
    <>
      <input
        className="field gp-search"
        placeholder="Search tools…"
        value={query}
        autoFocus
        onChange={(e) => setQuery(e.target.value)}
      />
      <div className="gp-cats">
        <button
          className={`gp-cat${cat === null ? ' on' : ''}`}
          onClick={() => setCat(null)}
        >
          All
        </button>
        {CATEGORIES.map((c) => (
          <button
            key={c}
            className={`gp-cat${cat === c ? ' on' : ''}`}
            onClick={() => setCat(cat === c ? null : c)}
          >
            {c}
          </button>
        ))}
      </div>
      <div className="gp-list">
        {entries.map((e) => (
          <button
            key={e.id}
            className="gp-entry"
            onClick={() => onPick({ ...e.segment })}
          >
            <span>{e.label}</span>
            <span className="gp-entry-cat">{e.category}</span>
          </button>
        ))}
        {entries.length === 0 ? (
          cat === 'Favorites' && (favs === null || favs.length === 0) ? (
            <div className="gp-empty">
              {favs === null
                ? 'Loading favorites…'
                : 'No favorites yet — star pages with the ★ at the end of the address bar.'}
            </div>
          ) : (
            <div className="gp-empty">Nothing matches</div>
          )
        ) : null}
      </div>
    </>
  )
}

export function GesturesPanel() {
  const [doc, setDoc] = useState<WheelDoc | null>(null)
  const [idx, setIdx] = useState(0)
  const [selected, setSelected] = useState<number | null>(null)
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

  /** Persist the whole document; the backend echoes the validated truth.
   *
   * Two REVIEW 2026-08-02 rules encoded here:
   * 1. SEQUENCED. Every text keystroke fires a full-doc PUT; without a
   *    guard, echo #1 landing after optimistic edit #2 snapped inputs
   *    backwards and could persist the older doc. Only the newest save may
   *    write the echo back into state.
   * 2. LOCK-PRESERVING. This panel has NO lock control, but it PUTs the
   *    whole doc from a mount-time snapshot — which silently reverted a
   *    default the user had just locked via the wheel's hub. The live
   *    locked value is re-read right before each PUT and carried through
   *    (unless this save is itself deleting that wheel).
   */
  const saveSeq = useRef(0)
  const save = async (next: WheelDoc, opts?: { droppedWheelId?: string }) => {
    const mine = ++saveSeq.current
    setDoc(next) // optimistic — the preview must track the edit instantly
    setSaving(true)
    try {
      let carryLocked = next.config.locked
      try {
        const fresh = await api<WheelDoc>('GET', '/api/wheels')
        carryLocked = fresh.config.locked
      } catch {
        /* offline blip: fall back to what we have rather than blocking */
      }
      if (opts?.droppedWheelId && carryLocked === opts.droppedWheelId) {
        carryLocked = null // deleting the locked wheel unlocks it
      }
      const echoed = await api<WheelDoc>('PUT', '/api/wheels', {
        ...next,
        config: { ...next.config, locked: carryLocked },
      })
      if (mine !== saveSeq.current) return // a newer save owns the truth now
      setDoc(echoed)
      setError(null)
    } catch (e) {
      if (mine !== saveSeq.current) return
      setError(e instanceof ApiError ? e.message : String(e))
      load() // the stored truth back on screen
    } finally {
      if (mine === saveSeq.current) setSaving(false)
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
  const isDynamic = !!wheel.dynamic
  // Saves can shrink the segment list out from under the selection.
  const sel =
    selected !== null && selected < wheel.segments.length ? selected : null

  const gotoWheel = (nextIdx: number) => {
    setIdx(nextIdx)
    setSelected(null)
  }

  const patchWheel = (patch: Partial<WheelRender>) =>
    save({
      ...doc,
      wheels: doc.wheels.map((w) => (w.id === wheel.id ? { ...w, ...patch } : w)),
    })

  const patchSegment = (i: number, seg: WheelSegment) =>
    patchWheel({ segments: wheel.segments.map((s, j) => (j === i ? seg : s)) })

  const rotateSelected = (dir: -1 | 1) => {
    if (sel === null) return
    const j = (sel + dir + wheel.segments.length) % wheel.segments.length
    const next = [...wheel.segments]
    ;[next[sel], next[j]] = [next[j], next[sel]]
    setSelected(j) // the selection follows the segment it was on
    patchWheel({ segments: next })
  }

  const removeSelected = () => {
    if (sel === null || wheel.segments.length <= 2) return
    setSelected(null)
    patchWheel({ segments: wheel.segments.filter((_, i) => i !== sel) })
  }

  const addSegment = () => {
    if (wheel.segments.length >= 12) return
    setSelected(wheel.segments.length) // select the new slot for immediate pick
    patchWheel({
      segments: [...wheel.segments, { type: 'placeholder', label: 'Edit me' }],
    })
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
    gotoWheel(doc.wheels.length)
  }

  const removeWheel = () => {
    if (wheel.builtin) return
    // REVIEW 2026-08-02: deleting a wheel that was the locked default, or
    // that another wheel's go-to segment pointed at, ALWAYS 422'd ("locked
    // wheel 'custom1' does not exist" — nonsense while it was on screen).
    // Deleting must also retarget every reference: dangling go-to segments
    // become placeholders, and the lock clears via droppedWheelId.
    const gone = wheel.id
    save(
      {
        ...doc,
        wheels: doc.wheels
          .filter((w) => w.id !== gone)
          .map((w) => ({
            ...w,
            segments: w.segments.map((s) =>
              s.type === 'wheel' && s.wheel === gone
                ? { type: 'placeholder' as const, label: 'Edit me' }
                : s
            ),
          })),
      },
      { droppedWheelId: gone }
    )
    gotoWheel(0)
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
        as your default. Click a segment on the preview to change what it does.
      </div>

      {error ? <div className="test-result bad">{error}</div> : null}

      <div className="gp-editor">
        <button
          className="btn gp-arrow"
          title="Previous wheel"
          onClick={() => gotoWheel((idx - 1 + doc.wheels.length) % doc.wheels.length)}
        >
          ‹
        </button>
        <div className="gp-preview">
          <WheelFace
            wheel={wheel}
            config={doc.config}
            quotes={noQuotes}
            hover={sel}
            mode="preview"
            scale={0.72}
            onSegment={(i) => {
              if (!isDynamic) setSelected(sel === i ? null : i)
            }}
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
          className="btn gp-arrow"
          title="Next wheel"
          onClick={() => gotoWheel((idx + 1) % doc.wheels.length)}
        >
          ›
        </button>

        <div className="gp-side">
          {isDynamic ? (
            <div className="gp-note">{DYNAMIC_NOTE[wheel.dynamic ?? ''] ?? ''}</div>
          ) : sel === null ? (
            <>
              <div className="gp-hint">
                Click a segment on the wheel to change it.
              </div>
              <div className="gp-ops">
                <button
                  className="btn"
                  disabled={wheel.segments.length >= 12}
                  onClick={addSegment}
                >
                  Add segment
                </button>
              </div>
            </>
          ) : (
            <>
              <div className="gp-selected-pos">Segment {posName(sel)}</div>
              <SegmentPicker
                // Remount per slot: fresh search, autofocus back to it.
                key={`${wheel.id}:${sel}`}
                doc={doc}
                onPick={(seg) => patchSegment(sel, seg)}
              />
              <div className="gp-ops">
                <button
                  className="btn"
                  title="Swap with the counter-clockwise neighbor"
                  onClick={() => rotateSelected(-1)}
                >
                  ↺
                </button>
                <button
                  className="btn"
                  title="Swap with the clockwise neighbor"
                  onClick={() => rotateSelected(1)}
                >
                  ↻
                </button>
                <button
                  className="btn"
                  disabled={wheel.segments.length <= 2}
                  title={
                    wheel.segments.length <= 2
                      ? 'A wheel needs at least 2 segments'
                      : 'Remove this segment'
                  }
                  onClick={removeSelected}
                >
                  Remove
                </button>
                <button
                  className="btn"
                  disabled={wheel.segments.length >= 12}
                  onClick={addSegment}
                >
                  Add
                </button>
                <label className="gp-label-field">
                  Label
                  <input
                    className="field"
                    value={wheel.segments[sel].label}
                    maxLength={20}
                    onChange={(e) =>
                      patchSegment(sel, {
                        ...wheel.segments[sel],
                        label: e.target.value,
                      })
                    }
                  />
                </label>
              </div>
            </>
          )}
        </div>
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

      <div className="setting-row" style={{ marginTop: 14 }}>
        <div className="setting-text">
          <div>Ticker segments show</div>
          <div className="subtle">
            On the Favorites wheel and any wheel you put a ticker favorite on:
            today’s move, or the live price.
          </div>
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
