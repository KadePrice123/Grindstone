/** Strategy presets — the one-click door into option legs.
 *
 *  Placement is a PURE function of (spot, today): deterministic from bar data
 *  alone, so a preset instantiates identically with or without chain data, and
 *  the gate can pin every strike offline. The primitive is % OF SPOT, never
 *  delta — indicative-feed greeks are legitimately absent (0DTE, zero-bid),
 *  and a preset that needs a greek would place nothing exactly when the feed
 *  is thinnest. The rough dictionary, for reading the offsets: ATM ~ Δ.50,
 *  ±3% ~ Δ.30, ±5% ~ Δ.16, ±8% ~ Δ.05 on a ~45-DTE SPY-like surface.
 *
 *  Strikes are left CONTINUOUS (612.4, not 612): the acceptance band does the
 *  matching against the real chain grid, which is the whole design — a filter
 *  window, not a contract picker. Expirations seed to the target DTE in
 *  calendar days, rolled OFF a weekend to Friday; real listed dates are caught
 *  by the ±DTE tolerance, so no Fridays-rule generation exists anywhere
 *  (Good-Friday Thursdays and SPY dailies come from the chain, not from us).
 */
import type { LegRight, LegSide, OptionLeg } from './components/ChartDraw'

export type LegSpec = Omit<OptionLeg, 'id' | 'slot' | 'group'>

export interface Preset {
  key: string
  label: string
  /** Picker grouping only (Robinhood's taxonomy) — no behaviour attached. */
  outlook: 'bullish' | 'bearish' | 'neutral' | 'volatility'
  legs: Array<{ side: LegSide; right: LegRight; pct: number; wing?: boolean }>
}

/** side/right at a strike offset (% of spot); wing widens the tolerance. */
export const PRESETS: Preset[] = [
  { key: 'long_call', label: 'Long call', outlook: 'bullish',
    legs: [{ side: 'long', right: 'C', pct: 0 }] },
  { key: 'long_put', label: 'Long put', outlook: 'bearish',
    legs: [{ side: 'long', right: 'P', pct: 0 }] },
  { key: 'covered_call', label: 'Covered call', outlook: 'neutral',
    legs: [{ side: 'short', right: 'C', pct: 3 }] },
  { key: 'csp', label: 'Cash-secured put', outlook: 'bullish',
    legs: [{ side: 'short', right: 'P', pct: -3 }] },
  { key: 'call_debit', label: 'Call debit spread', outlook: 'bullish',
    legs: [{ side: 'long', right: 'C', pct: 0 }, { side: 'short', right: 'C', pct: 3 }] },
  { key: 'put_debit', label: 'Put debit spread', outlook: 'bearish',
    legs: [{ side: 'long', right: 'P', pct: 0 }, { side: 'short', right: 'P', pct: -3 }] },
  { key: 'put_credit', label: 'Put credit spread', outlook: 'bullish',
    legs: [{ side: 'short', right: 'P', pct: -3 }, { side: 'long', right: 'P', pct: -4, wing: true }] },
  { key: 'call_credit', label: 'Call credit spread', outlook: 'bearish',
    legs: [{ side: 'short', right: 'C', pct: 3 }, { side: 'long', right: 'C', pct: 4, wing: true }] },
  { key: 'iron_condor', label: 'Iron condor', outlook: 'neutral',
    legs: [
      { side: 'short', right: 'P', pct: -5 }, { side: 'long', right: 'P', pct: -6, wing: true },
      { side: 'short', right: 'C', pct: 5 }, { side: 'long', right: 'C', pct: 6, wing: true },
    ] },
  { key: 'iron_butterfly', label: 'Iron butterfly', outlook: 'neutral',
    legs: [
      { side: 'short', right: 'P', pct: 0 }, { side: 'short', right: 'C', pct: 0 },
      { side: 'long', right: 'P', pct: -5, wing: true }, { side: 'long', right: 'C', pct: 5, wing: true },
    ] },
  { key: 'straddle', label: 'Straddle', outlook: 'volatility',
    legs: [{ side: 'long', right: 'C', pct: 0 }, { side: 'long', right: 'P', pct: 0 }] },
  { key: 'strangle', label: 'Strangle', outlook: 'volatility',
    legs: [{ side: 'long', right: 'C', pct: 5 }, { side: 'long', right: 'P', pct: -5 }] },
]

export const DEFAULT_DTE = 45

/** Target DTE as a calendar date, rolled off a weekend BACK to Friday — an
 *  expiration convention, not a bars question, so it does not reuse the
 *  chart's trading-day walk. The ±dteTol band catches the listed date. */
export function expirationForDte(today: string, dte: number): string | null {
  const t = Date.parse(today + 'T00:00:00Z')
  if (!Number.isFinite(t)) return null
  let d = new Date(t + Math.max(0, Math.round(dte)) * 86400_000)
  const dow = d.getUTCDay()
  if (dow === 6) d = new Date(d.getTime() - 86400_000) // Sat -> Fri
  else if (dow === 0) d = new Date(d.getTime() - 2 * 86400_000) // Sun -> Fri
  return d.toISOString().slice(0, 10)
}

/** Instantiate a preset at defaults. Pure: (preset, spot, today) → specs.
 *  Tolerances: ±3 calendar days; strike ±max($1, 0.5% of spot) on body legs,
 *  ±max($2, 1% of spot) on wings — wide enough to catch the grid, tight
 *  enough that a condor's four zones read as four zones. */
export function placePreset(preset: Preset, spot: number, today: string): LegSpec[] | null {
  if (!Number.isFinite(spot) || spot <= 0) return null
  const expiration = expirationForDte(today, DEFAULT_DTE)
  if (!expiration) return null
  return preset.legs.map((l) => ({
    side: l.side,
    right: l.right,
    expiration,
    strike: spot * (1 + l.pct / 100),
    dteTol: 3,
    strikeTol: l.wing ? Math.max(2, spot * 0.01) : Math.max(1, spot * 0.005),
  }))
}
