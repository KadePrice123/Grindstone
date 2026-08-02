/**
 * Period editor for the parametrized chart indicators. Opened by the chart
 * wheel's 'settings' action and the toolbar gear; floats over the chart.
 *
 * SESSION-SCOPED by design this round: the edited periods live in the
 * page's React state and reset on reload. Persisting them (settings blob)
 * is a later, deliberate step — not forgotten.
 *
 * Drafts are strings so typing is never fought; validation happens at the
 * edge (int 1..400). A bad field turns red and disables Apply — no silent
 * clamping, no dead clicks.
 */
import { useState } from 'react'
import { DEFAULT_INDICATOR_PERIODS, IndicatorParams } from './Chart'

const FIELDS: { key: keyof IndicatorParams; label: string }[] = [
  { key: 'sma20', label: 'SMA #1' },
  { key: 'sma50', label: 'SMA #2' },
  { key: 'ema20', label: 'EMA' },
  { key: 'rsi14', label: 'RSI' },
]

function parsePeriod(s: string): number | null {
  const n = Number(s)
  return Number.isInteger(n) && n >= 1 && n <= 400 ? n : null
}

export function IndicatorSettings({
  params,
  onApply,
  onClose,
}: {
  params: IndicatorParams
  onApply: (p: IndicatorParams) => void
  onClose: () => void
}) {
  const [draft, setDraft] = useState<Record<keyof IndicatorParams, string>>(() => ({
    sma20: String(params.sma20?.period ?? DEFAULT_INDICATOR_PERIODS.sma20),
    sma50: String(params.sma50?.period ?? DEFAULT_INDICATOR_PERIODS.sma50),
    ema20: String(params.ema20?.period ?? DEFAULT_INDICATOR_PERIODS.ema20),
    rsi14: String(params.rsi14?.period ?? DEFAULT_INDICATOR_PERIODS.rsi14),
  }))

  const invalid = FIELDS.some((f) => parsePeriod(draft[f.key]) === null)

  const apply = () => {
    if (invalid) return
    // Apply keeps the panel open on purpose — nudge a period, watch the
    // line move, nudge again. Close is its own explicit act.
    onApply({
      sma20: { period: parsePeriod(draft.sma20)! },
      sma50: { period: parsePeriod(draft.sma50)! },
      ema20: { period: parsePeriod(draft.ema20)! },
      rsi14: { period: parsePeriod(draft.rsi14)! },
    })
  }

  return (
    <div
      className="indset"
      onKeyDown={(e) => {
        if (e.key === 'Escape') onClose()
        else if (e.key === 'Enter') apply()
      }}
    >
      <div className="indset-head">
        <span>Indicator periods</span>
        <button className="indset-x" title="Close" onClick={onClose}>
          ×
        </button>
      </div>
      {FIELDS.map((f, i) => (
        <label className="indset-row" key={f.key}>
          <span>{f.label}</span>
          <input
            type="number"
            min={1}
            max={400}
            step={1}
            autoFocus={i === 0}
            className={parsePeriod(draft[f.key]) === null ? 'bad' : ''}
            value={draft[f.key]}
            onChange={(e) => setDraft((d) => ({ ...d, [f.key]: e.target.value }))}
          />
        </label>
      ))}
      <div className="indset-note">whole bars, 1–400 · this session only</div>
      <div className="indset-actions">
        <button className="btn" disabled={invalid} onClick={apply}>
          Apply
        </button>
        <button className="btn" onClick={onClose}>
          Close
        </button>
      </div>
    </div>
  )
}
