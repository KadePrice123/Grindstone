/**
 * The human half of the spec editor: a form that compiles to the SAME spec
 * JSON the engine validates and an AI agent would write directly. The form
 * deliberately covers the common shapes (delta/strike/width legs, the sugar
 * exits, fixed / buying-power sizing, an entry gate expression); anything
 * richer — params, sizing ladders, rule lists — lives in JSON mode, and
 * tryDecompile() returning null is how the page knows a spec is beyond the
 * form rather than silently dropping pieces of it.
 */
import { useState } from 'react'

export interface FormLeg {
  action: 'sell' | 'buy'
  right: 'put' | 'call'
  selector: 'delta' | 'strike' | 'width'
  value: string
  dte: string
  anchor: number // index of the anchor leg for width selectors
}

interface ExitToggle {
  on: boolean
  v: string
}

export interface SpecFormState {
  underlying: string
  capital: string
  legs: FormLeg[]
  maxActive: string
  minDaysBetween: string
  entryWhen: string
  exits: {
    dte: ExitToggle
    take_profit: ExitToggle // shown as % of premium
    stop_loss: ExitToggle //   shown as % of premium
    vix_above: ExitToggle
    delta_above: ExitToggle
  }
  sizingMode: 'fixed' | 'bp_fraction'
  contracts: string
  fraction: string // shown as % of buying power
}

export const DEFAULT_FORM: SpecFormState = {
  underlying: 'SPY',
  capital: '100000',
  legs: [{ action: 'sell', right: 'put', selector: 'delta', value: '0.30', dte: '45', anchor: 0 }],
  maxActive: '1',
  minDaysBetween: '',
  entryWhen: '',
  exits: {
    dte: { on: true, v: '21' },
    take_profit: { on: true, v: '50' },
    stop_loss: { on: false, v: '200' },
    vix_above: { on: false, v: '25' },
    delta_above: { on: false, v: '0.45' },
  },
  sizingMode: 'fixed',
  contracts: '1',
  fraction: '5',
}

const legName = (i: number, l: FormLeg) => `${l.action}_${l.right}_${i + 1}`

/** Form -> spec dict. Only meaningful values are emitted, so the JSON stays
 *  as small as a hand-written one. */
export function compileForm(f: SpecFormState): Record<string, unknown> {
  const legs = f.legs.map((l, i) => {
    const leg: Record<string, unknown> = {
      name: legName(i, l),
      action: l.action,
      right: l.right,
    }
    if (l.selector === 'width') {
      leg.width = Number(l.value)
      leg.anchor = legName(l.anchor, f.legs[l.anchor])
    } else {
      leg[l.selector] = Number(l.value)
      if (l.dte) leg.dte = Number(l.dte)
    }
    return leg
  })
  const exits: Record<string, unknown> = {}
  if (f.exits.dte.on) exits.dte = Number(f.exits.dte.v)
  if (f.exits.take_profit.on) exits.take_profit = Number(f.exits.take_profit.v) / 100
  if (f.exits.stop_loss.on) exits.stop_loss = Number(f.exits.stop_loss.v) / 100
  if (f.exits.vix_above.on) exits.vix_above = Number(f.exits.vix_above.v)
  if (f.exits.delta_above.on) exits.delta_above = Number(f.exits.delta_above.v)
  const entry: Record<string, unknown> = { frequency: 'daily' }
  if (f.maxActive && f.maxActive !== '1') entry.max_active = Number(f.maxActive)
  if (f.minDaysBetween) entry.min_days_between = Number(f.minDaysBetween)
  if (f.entryWhen.trim()) entry.when = f.entryWhen.trim()
  const spec: Record<string, unknown> = {
    underlying: f.underlying.toUpperCase() || 'SPY',
    capital: Number(f.capital) || 100000,
    legs,
    entry,
    exits,
  }
  if (f.sizingMode === 'bp_fraction') {
    spec.sizing = { mode: 'bp_fraction', fraction: Number(f.fraction) / 100 }
  } else if (f.contracts && f.contracts !== '1') {
    spec.sizing = { mode: 'fixed', contracts: Number(f.contracts) }
  }
  return spec
}

/** Spec dict -> form, or null when the spec uses anything the form cannot
 *  represent (then the page stays in JSON mode — never lossy round-trips). */
export function tryDecompile(spec: unknown): SpecFormState | null {
  if (spec === null || typeof spec !== 'object' || Array.isArray(spec)) return null
  const s = spec as Record<string, unknown>
  const known = new Set(['name', 'underlying', 'capital', 'legs', 'entry', 'exits',
    'sizing', 'start', 'end'])
  if (Object.keys(s).some((k) => !known.has(k))) return null
  if (!Array.isArray(s.legs) || s.legs.length === 0) return null

  const names: string[] = []
  const legs: FormLeg[] = []
  for (const raw of s.legs) {
    if (raw === null || typeof raw !== 'object') return null
    const l = raw as Record<string, unknown>
    const legKnown = new Set(['name', 'action', 'right', 'delta', 'strike', 'width',
      'anchor', 'dte', 'ratio'])
    if (Object.keys(l).some((k) => !legKnown.has(k))) return null
    if (l.ratio !== undefined && l.ratio !== 1) return null
    if (l.action !== 'sell' && l.action !== 'buy') return null
    if (l.right !== 'put' && l.right !== 'call') return null
    let selector: FormLeg['selector']
    let value: number
    let anchor = 0
    if (typeof l.delta === 'number') {
      selector = 'delta'
      value = l.delta
    } else if (typeof l.strike === 'number') {
      selector = 'strike'
      value = l.strike
    } else if (typeof l.width === 'number') {
      selector = 'width'
      value = l.width
      const i = names.indexOf(String(l.anchor ?? ''))
      if (i < 0) return null
      anchor = i
    } else {
      return null
    }
    names.push(String(l.name ?? `leg${names.length + 1}`))
    legs.push({
      action: l.action, right: l.right, selector,
      value: String(value), dte: l.dte !== undefined ? String(l.dte) : '',
      anchor,
    })
  }

  const f: SpecFormState = JSON.parse(JSON.stringify(DEFAULT_FORM))
  f.legs = legs
  f.underlying = String(s.underlying ?? 'SPY')
  f.capital = String(s.capital ?? '100000')

  const entry = (s.entry ?? {}) as Record<string, unknown>
  const entryKnown = new Set(['frequency', 'max_active', 'min_days_between', 'when'])
  if (Object.keys(entry).some((k) => !entryKnown.has(k))) return null
  if (typeof entry.max_active === 'string') return null // expression: JSON-only
  f.maxActive = String(entry.max_active ?? '1')
  f.minDaysBetween = entry.min_days_between !== undefined ? String(entry.min_days_between) : ''
  f.entryWhen = typeof entry.when === 'string' ? entry.when : ''

  const exits = (s.exits ?? {}) as Record<string, unknown>
  const exitKnown = new Set(['dte', 'take_profit', 'stop_loss', 'vix_above', 'delta_above'])
  if (Object.keys(exits).some((k) => !exitKnown.has(k))) return null
  f.exits = {
    dte: { on: exits.dte !== undefined, v: String(exits.dte ?? '21') },
    take_profit: {
      on: exits.take_profit !== undefined,
      v: String(exits.take_profit !== undefined ? Number(exits.take_profit) * 100 : 50),
    },
    stop_loss: {
      on: exits.stop_loss !== undefined,
      v: String(exits.stop_loss !== undefined ? Number(exits.stop_loss) * 100 : 200),
    },
    vix_above: { on: exits.vix_above !== undefined, v: String(exits.vix_above ?? 25) },
    delta_above: { on: exits.delta_above !== undefined, v: String(exits.delta_above ?? 0.45) },
  }

  const sizing = (s.sizing ?? {}) as Record<string, unknown>
  if (Object.keys(sizing).length) {
    const sizingKnown = new Set(['mode', 'contracts', 'fraction', 'max_contracts', 'min_contracts'])
    if (Object.keys(sizing).some((k) => !sizingKnown.has(k))) return null
    if (sizing.mode === 'fixed' || sizing.mode === undefined) {
      f.sizingMode = 'fixed'
      f.contracts = String(sizing.contracts ?? '1')
    } else if (sizing.mode === 'bp_fraction') {
      f.sizingMode = 'bp_fraction'
      f.fraction = String(Number(sizing.fraction ?? 0.05) * 100)
    } else {
      return null
    }
  }
  return f
}

const EXIT_LABELS: { key: keyof SpecFormState['exits']; label: string; hint: string }[] = [
  { key: 'dte', label: 'Close at DTE', hint: 'days to expiry' },
  { key: 'take_profit', label: 'Take profit', hint: '% of premium' },
  { key: 'stop_loss', label: 'Stop loss', hint: '% of premium' },
  { key: 'vix_above', label: 'VIX above', hint: 'index level' },
  { key: 'delta_above', label: 'Short delta above', hint: 'absolute delta' },
]

export function SpecForm({
  initial,
  onSpec,
}: {
  initial: SpecFormState
  onSpec: (spec: Record<string, unknown>) => void
}) {
  const [f, setF] = useState<SpecFormState>(initial)

  const update = (next: SpecFormState) => {
    setF(next)
    onSpec(compileForm(next))
  }
  const setLeg = (i: number, patch: Partial<FormLeg>) => {
    const legs = f.legs.map((l, j) => (j === i ? { ...l, ...patch } : l))
    update({ ...f, legs })
  }

  return (
    <div className="form-grid">
      <label>Underlying</label>
      <input
        className="field"
        value={f.underlying}
        onChange={(e) => update({ ...f, underlying: e.target.value })}
        spellCheck={false}
      />
      <label>Capital $</label>
      <input
        className="field"
        value={f.capital}
        onChange={(e) => update({ ...f, capital: e.target.value })}
        spellCheck={false}
      />

      <label>Legs</label>
      <div>
        {f.legs.map((l, i) => (
          <div key={i} style={{ display: 'flex', gap: 6, marginBottom: 6, flexWrap: 'wrap' }}>
            <button
              className="btn"
              type="button"
              title="Sell collects premium; buy pays it"
              onClick={() => setLeg(i, { action: l.action === 'sell' ? 'buy' : 'sell' })}
            >
              {l.action}
            </button>
            <button
              className="btn"
              type="button"
              onClick={() => setLeg(i, { right: l.right === 'put' ? 'call' : 'put' })}
            >
              {l.right}
            </button>
            <select
              className="field"
              value={l.selector}
              onChange={(e) => setLeg(i, { selector: e.target.value as FormLeg['selector'] })}
            >
              <option value="delta">delta</option>
              <option value="strike">strike</option>
              <option value="width">width from</option>
            </select>
            {l.selector === 'width' ? (
              <select
                className="field"
                value={l.anchor}
                onChange={(e) => setLeg(i, { anchor: Number(e.target.value) })}
              >
                {f.legs.map((a, j) =>
                  j !== i ? (
                    <option key={j} value={j}>
                      {a.action} {a.right} #{j + 1}
                    </option>
                  ) : null
                )}
              </select>
            ) : null}
            <input
              className="field"
              style={{ width: 90 }}
              value={l.value}
              onChange={(e) => setLeg(i, { value: e.target.value })}
              placeholder={l.selector === 'delta' ? '0.30' : l.selector === 'strike' ? '500' : '10'}
              spellCheck={false}
            />
            {l.selector !== 'width' ? (
              <>
                <span className="subtle" style={{ alignSelf: 'center' }}>DTE</span>
                <input
                  className="field"
                  style={{ width: 70 }}
                  value={l.dte}
                  onChange={(e) => setLeg(i, { dte: e.target.value })}
                  placeholder="45"
                  spellCheck={false}
                />
              </>
            ) : null}
            {f.legs.length > 1 ? (
              <button
                className="btn"
                type="button"
                onClick={() => update({ ...f, legs: f.legs.filter((_, j) => j !== i) })}
              >
                ✕
              </button>
            ) : null}
          </div>
        ))}
        <button
          className="btn"
          type="button"
          onClick={() =>
            update({
              ...f,
              legs: [...f.legs, { action: 'buy', right: 'put', selector: 'width',
                                  value: '10', dte: '', anchor: 0 }],
            })
          }
        >
          + Add leg
        </button>
      </div>

      <label>Exits</label>
      <div>
        {EXIT_LABELS.map(({ key, label, hint }) => (
          <div key={key} style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 4 }}>
            <button
              className={`toggle${f.exits[key].on ? ' on' : ''}`}
              type="button"
              onClick={() =>
                update({ ...f, exits: { ...f.exits, [key]: { ...f.exits[key], on: !f.exits[key].on } } })
              }
            >
              <span className="knob" />
            </button>
            <span style={{ minWidth: 130 }}>{label}</span>
            <input
              className="field"
              style={{ width: 90 }}
              disabled={!f.exits[key].on}
              value={f.exits[key].v}
              onChange={(e) =>
                update({ ...f, exits: { ...f.exits, [key]: { ...f.exits[key], v: e.target.value } } })
              }
              spellCheck={false}
            />
            <span className="subtle">{hint}</span>
          </div>
        ))}
      </div>

      <label>Max active</label>
      <input
        className="field"
        value={f.maxActive}
        onChange={(e) => update({ ...f, maxActive: e.target.value })}
        spellCheck={false}
      />
      <label>Days between entries</label>
      <input
        className="field"
        value={f.minDaysBetween}
        onChange={(e) => update({ ...f, minDaysBetween: e.target.value })}
        placeholder="none"
        spellCheck={false}
      />
      <label>Entry condition</label>
      <input
        className="field"
        value={f.entryWhen}
        onChange={(e) => update({ ...f, entryWhen: e.target.value })}
        placeholder="optional, e.g. vix < 40 and above_sma_200"
        spellCheck={false}
      />

      <label>Sizing</label>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <select
          className="field"
          value={f.sizingMode}
          onChange={(e) => update({ ...f, sizingMode: e.target.value as SpecFormState['sizingMode'] })}
        >
          <option value="fixed">fixed contracts</option>
          <option value="bp_fraction">% of buying power</option>
        </select>
        {f.sizingMode === 'fixed' ? (
          <input
            className="field"
            style={{ width: 80 }}
            value={f.contracts}
            onChange={(e) => update({ ...f, contracts: e.target.value })}
            spellCheck={false}
          />
        ) : (
          <>
            <input
              className="field"
              style={{ width: 80 }}
              value={f.fraction}
              onChange={(e) => update({ ...f, fraction: e.target.value })}
              spellCheck={false}
            />
            <span className="subtle">% per position</span>
          </>
        )}
      </div>
    </div>
  )
}
