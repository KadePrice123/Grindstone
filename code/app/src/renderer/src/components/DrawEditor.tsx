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
import type { ChartDraw, Drawing, Pt } from './ChartDraw'

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

function Field({
  label,
  initial,
  parse,
  onCommit,
}: {
  label: string
  initial: string
  parse: (s: string) => number | null
  onCommit: (v: number) => void
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
    </label>
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
}: {
  engine: ChartDraw
  /** The selected drawings, in selection order (engine state's `selection`). */
  selection: Drawing[]
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
  const priceField = (i: number, label: string) => (
    <Field
      key={`${d.id}:${i}:p:${d.points[i].price}`}
      label={label}
      initial={fmtPrice(d.points[i].price)}
      parse={parsePrice}
      onCommit={(v) => setPt(i, { price: v })}
    />
  )
  const timeField = (i: number, label: string) => (
    <Field
      key={`${d.id}:${i}:t:${d.points[i].time}`}
      label={label}
      initial={fmtTime(d.points[i].time)}
      parse={parseTime}
      onCommit={(v) => setPt(i, { time: v as Pt['time'] })}
    />
  )

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
