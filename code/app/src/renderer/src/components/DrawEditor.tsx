/**
 * DrawEditor — the floating exact-value editor for the selected drawing(s).
 *
 * Pages get `selection` (Drawing objects) from ChartDraw.onChange and render
 * this inside their .float-panel wrapper over the chart (charttools.css owns
 * the panel chrome; charts.css owns this component's internals). One selected
 * object shows its editable coordinates; several show a count + Delete all.
 * Edits commit through engine.updateDrawing, which keeps the selection, so
 * the panel stays up across commits.
 *
 * Field contract (Kade's exact-value boxes):
 *   - commit on Enter or blur -> engine.updateDrawing(id, points)
 *   - invalid input: red border, no commit, no crash — the draft stays so
 *     the user can fix it instead of losing what they typed
 *   - Escape reverts the draft and leaves the field (stopped there, so the
 *     engine's Escape-clears-selection doesn't also fire)
 *   - prices parse as floats; dates as YYYY-MM-DD or YYYY-MM-DD HH:mm,
 *     interpreted UTC to match the bar timestamps under the chart. The
 *     engine snaps committed times to the nearest bar so an off-calendar
 *     date can't silently hide the drawing.
 */
import { useEffect, useRef, useState, type ReactNode } from 'react'
import type { ChartDraw, Drawing, Pt, ResolvedLeg } from './ChartDraw'
import { LEG_PALETTE } from './ChartDraw'

function fmtPrice(p: number): string {
  // Trim float noise but keep real precision: 264.5 not 264.50000000000003.
  return String(Number(p.toFixed(4)))
}

function fmtTime(t: number): string {
  const d = new Date(t * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  const date = `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())}`
  return d.getUTCHours() === 0 && d.getUTCMinutes() === 0
    ? date
    : `${date} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`
}

function parsePrice(s: string): number | null {
  const t = s.trim().replace(/^\$/, '')
  if (t === '') return null
  const v = Number(t)
  return Number.isFinite(v) ? v : null
}

function parseTime(s: string): number | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?$/.exec(s.trim())
  if (!m) return null
  const y = +m[1]
  const mo = +m[2]
  const da = +m[3]
  const hh = m[4] ? +m[4] : 0
  const mi = m[5] ? +m[5] : 0
  const ms = Date.UTC(y, mo - 1, da, hh, mi)
  const d = new Date(ms)
  // Date.UTC silently rolls 2026-02-31 into March — reject, don't "fix".
  if (
    d.getUTCFullYear() !== y ||
    d.getUTCMonth() !== mo - 1 ||
    d.getUTCDate() !== da ||
    d.getUTCHours() !== hh ||
    d.getUTCMinutes() !== mi
  )
    return null
  return ms / 1000
}

/** The per-field padlock.
 *
 *  Kade: "In these forms I have posted I should also be able to type and lock
 *  the prices and dates as well." So each coordinate gets its own, and locking
 *  one leaves the other free — lock a trend endpoint's price and it can still
 *  slide in time; lock its date and it can only move vertically.
 *
 *  It lights for a coordinate pinned THROUGH an equality too, not only for one
 *  you locked directly: a price held by a lock on the line it rides is just as
 *  immovable, and drawing it open would be a lie. */
function Padlock({ on, onToggle }: { on: boolean; onToggle: () => void }) {
  return (
    <button
      type="button"
      className={`de-lock${on ? ' on' : ''}`}
      title={on ? 'Locked — click to release' : 'Lock this value'}
      aria-pressed={on}
      onClick={onToggle}
    >
      <svg viewBox="0 0 14 14" width="12" height="12" aria-hidden="true">
        <rect x="3" y="6.5" width="8" height="6" rx="1.2" fill="currentColor" />
        {on ? (
          <path d="M4.8 6.5V4.6a2.2 2.2 0 0 1 4.4 0v1.9" fill="none" stroke="currentColor" strokeWidth="1.3" />
        ) : (
          <path d="M4.8 6.5V4.6a2.2 2.2 0 0 1 4.4 0" fill="none" stroke="currentColor" strokeWidth="1.3" />
        )}
      </svg>
    </button>
  )
}

function Field({
  label,
  initial,
  parse,
  onCommit,
  locked,
  onToggleLock,
}: {
  label: string
  initial: string
  parse: (s: string) => number | null
  onCommit: (v: number) => void
  locked?: boolean
  onToggleLock?: () => void
}) {
  const [draft, setDraft] = useState(initial)
  const [bad, setBad] = useState(false)
  // Escape sets this so the revert's blur doesn't commit the stale draft
  // (the setState hasn't rendered yet when blur fires synchronously).
  const skipBlur = useRef(false)

  // External change (engine re-render, another field's commit) resets the draft.
  useEffect(() => {
    setDraft(initial)
    setBad(false)
  }, [initial])

  const commit = () => {
    const v = parse(draft)
    if (v === null) {
      setBad(true)
      return
    }
    setBad(false)
    onCommit(v)
  }

  return (
    <label className="de-row">
      <span className="de-lbl">{label}</span>
      <input
        className={`field de-input${bad ? ' bad' : ''}`}
        value={draft}
        spellCheck={false}
        onChange={(e) => {
          setDraft(e.target.value)
          if (bad) setBad(false)
        }}
        onBlur={() => {
          if (skipBlur.current) {
            skipBlur.current = false
            return
          }
          commit()
        }}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault()
            commit()
          } else if (e.key === 'Escape') {
            e.stopPropagation()
            skipBlur.current = true
            setDraft(initial)
            setBad(false)
            ;(e.target as HTMLInputElement).blur()
          }
        }}
      />
      {onToggleLock ? <Padlock on={!!locked} onToggle={onToggleLock} /> : null}
    </label>
  )
}

/** The selected LEG's exact-value editor — the type-in path Kade asked for
 *  ("you can also just type it in if you want"). Reuses Field's contract
 *  (commit on Enter/blur, red on invalid, Escape reverts). Typing a value the
 *  host drives goes through updateLeg, which UNBINDS first — the typed number
 *  wins over the line, and the engine says so by the binding disappearing. */
export function LegEditor({ engine, leg }: { engine: ChartDraw; leg: ResolvedLeg }) {
  const color = LEG_PALETTE[leg.slot % LEG_PALETTE.length]
  const patch = (p: Parameters<ChartDraw['updateLeg']>[1]) => engine.updateLeg(leg.id, p)
  return (
    <div className="draw-editor">
      <div className="de-head">
        <span>
          <span className="cp-swatch" style={{ background: color }} /> Option leg
          {leg.resolved.hosted ? <span className="dim"> · rides {leg.resolved.hosted}</span> : null}
        </span>
        <button className="de-x" title="Deselect (Esc)" onClick={() => engine.clearSelection()}>
          ×
        </button>
      </div>
      <div className="de-row">
        <span className="de-lbl">Side</span>
        <div className="seg de-seg">
          <button
            className={`seg-btn${leg.side === 'long' ? ' on' : ''}`}
            onClick={() => patch({ side: 'long' })}
          >
            Buy
          </button>
          <button
            className={`seg-btn${leg.side === 'short' ? ' on' : ''}`}
            onClick={() => patch({ side: 'short' })}
          >
            Sell
          </button>
        </div>
      </div>
      <div className="de-row">
        <span className="de-lbl">Right</span>
        <div className="seg de-seg">
          <button
            className={`seg-btn${leg.right === 'C' ? ' on' : ''}`}
            onClick={() => patch({ right: 'C' })}
          >
            Call
          </button>
          <button
            className={`seg-btn${leg.right === 'P' ? ' on' : ''}`}
            onClick={() => patch({ right: 'P' })}
          >
            Put
          </button>
        </div>
      </div>
      <Field
        key={`${leg.id}:strike:${leg.resolved.strike}`}
        label="Strike"
        initial={fmtPrice(leg.resolved.strike)}
        parse={parsePrice}
        onCommit={(v) => patch({ strike: v })}
      />
      <Field
        key={`${leg.id}:exp:${leg.resolved.expiration}`}
        label="Expiry"
        initial={leg.resolved.expiration}
        parse={parseTime}
        // parseTime hands back epoch seconds; the leg stores the calendar date.
        onCommit={(v) => patch({ expiration: new Date(v * 1000).toISOString().slice(0, 10) })}
      />
      {/* ±days and ±$ used to be typed here. They are the leg's WINDOW, and
          the window is now the bounding lines — drag them and the tolerance
          follows. Two ways to set one value is two ways to disagree about
          it, and the lines are the one you can see. The fields are gone;
          the values are not. */}
      <button className="btn de-del" onClick={() => engine.deleteLeg(leg.id)}>
        Delete leg
      </button>
    </div>
  )
}

const KIND_TITLE: Record<Drawing['kind'], string> = {
  trend: 'Trend line',
  hline: 'Horizontal line',
  vline: 'Vertical line',
  circle: 'Circle',
}

export function DrawEditor({
  engine,
  selection,
  lockedSlots = [],
}: {
  engine: ChartDraw
  /** The selected drawings, in selection order (engine state's `selection`). */
  selection: Drawing[]
  /** Pinned coordinate slots from engine state — which padlocks are closed. */
  lockedSlots?: string[]
}) {
  if (selection.length === 0) return null

  const del = (
    <button className="btn de-del" onClick={() => engine.deleteSelected()}>
      {selection.length > 1 ? `Delete all (${selection.length})` : 'Delete'}
    </button>
  )
  const close = (
    <button className="de-x" title="Deselect (Esc)" onClick={() => engine.clearSelection()}>
      ×
    </button>
  )

  if (selection.length > 1) {
    return (
      <div className="draw-editor">
        <div className="de-head">
          <span>{selection.length} selected</span>
          {close}
        </div>
        {del}
      </div>
    )
  }

  const d = selection[0]
  const setPt = (i: number, patch: Partial<Pt>) => {
    engine.updateDrawing(
      d.id,
      d.points.map((p, j) => (j === i ? { ...p, ...patch } : p))
    )
  }
  // Which coordinate each field edits, in the engine's own slot vocabulary.
  // An hline/vline is `line`; a trend's points are `a` and `b`.
  const part = (i: number): 'line' | 'a' | 'b' =>
    d.kind === 'trend' || d.kind === 'circle' ? (i === 0 ? 'a' : 'b') : 'line'
  const held = new Set(lockedSlots)
  const isHeld = (i: number, code: 'p' | 'i') => held.has(`${d.id}:${part(i)}:${code}`)
  // Circles carry no constrainable coordinates (ellipsePx is the in-tree proof
  // the plane is not isotropic), so they get plain fields and no padlocks.
  const lockable = d.kind !== 'circle'

  const priceField = (i: number, label: string) => (
    <Field
      key={`${d.id}:${i}:p:${d.points[i].price}`}
      label={label}
      initial={fmtPrice(d.points[i].price)}
      parse={parsePrice}
      // Typing a value SETS AND HOLDS it — "setting any dimension of an item
      // will lock that dimension until the user changes it". setSlotValue also
      // writes through any equality, so a price typed on a trend endpoint moves
      // the line it rides, and refuses with a reason when something else holds
      // it. Circles fall back to the plain path.
      onCommit={(v) =>
        lockable
          ? engine.setSlotValue({ id: d.id, part: part(i), axis: 'price' }, v)
          : setPt(i, { price: v })
      }
      locked={isHeld(i, 'p')}
      onToggleLock={
        lockable
          ? () =>
              isHeld(i, 'p')
                ? engine.clearSlotLock({ id: d.id, part: part(i), axis: 'price' })
                : engine.setSlotValue(
                    { id: d.id, part: part(i), axis: 'price' },
                    d.points[i].price
                  )
          : undefined
      }
    />
  )
  const timeField = (i: number, label: string) => (
    <Field
      key={`${d.id}:${i}:t:${d.points[i].time}`}
      label={label}
      initial={fmtTime(d.points[i].time)}
      parse={parseTime}
      onCommit={(v) =>
        lockable
          ? engine.setSlotValue({ id: d.id, part: part(i), axis: 'time' }, v)
          : setPt(i, { time: v as Pt['time'] })
      }
      locked={isHeld(i, 'i')}
      onToggleLock={
        lockable
          ? () =>
              isHeld(i, 'i')
                ? engine.clearSlotLock({ id: d.id, part: part(i), axis: 'time' })
                : engine.setSlotValue(
                    { id: d.id, part: part(i), axis: 'time' },
                    d.points[i].time
                  )
          : undefined
      }
    />
  )

  // A trend's SLOPE is a driving dimension, not a coordinate: locking it holds
  // the angle of the line while both its ends stay free to move, so dragging an
  // h-line makes the far end run out instead of tilting the line. Price per
  // hour of chart time — never degrees, which would go false as the price scale
  // drifts under an untouched chart.
  const slopeLocked = engine.slopeLockOf(d.id)
  const slopeNow = engine.slopeOf(d.id)
  const slopeField =
    d.kind === 'trend' && (slopeNow !== null || slopeLocked !== null) ? (
      <Field
        key={`${d.id}:slope:${slopeLocked ?? slopeNow}`}
        label="Slope"
        initial={fmtPrice(slopeLocked ?? slopeNow ?? 0)}
        parse={parsePrice}
        onCommit={(v) => engine.setSlopeLock(d.id, v)}
        locked={slopeLocked !== null}
        onToggleLock={() =>
          engine.setSlopeLock(d.id, slopeLocked !== null ? null : (slopeNow ?? 0))
        }
      />
    ) : null

  let fields: ReactNode
  if (d.kind === 'hline') {
    fields = priceField(0, 'Price')
  } else if (d.kind === 'vline') {
    fields = timeField(0, 'Date')
  } else {
    const [gA, gB] = d.kind === 'circle' ? ['Center', 'Edge'] : ['Start', 'End']
    fields = (
      <>
        <div className="de-group">{gA}</div>
        {priceField(0, 'Price')}
        {timeField(0, 'Date')}
        <div className="de-group">{gB}</div>
        {priceField(1, 'Price')}
        {timeField(1, 'Date')}
        {slopeField ? (
          <>
            <div className="de-group">Shape</div>
            {slopeField}
          </>
        ) : null}
      </>
    )
  }

  return (
    <div className="draw-editor">
      <div className="de-head">
        <span>{KIND_TITLE[d.kind]}</span>
        {close}
      </div>
      {fields}
      {del}
    </div>
  )
}
