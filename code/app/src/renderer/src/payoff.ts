/** Expiration payoff for a set of option legs: net debit/credit, max profit,
 *  max loss, breakevens.
 *
 *  ANALYTICS ON A HYPOTHETICAL STRUCTURE. Nothing here places, prices or routes
 *  an order — the whole options surface in this app is a filter, and this is
 *  the arithmetic that says what the filtered structure would be worth at
 *  expiry. No broker call, no order ticket, no position.
 *
 *  PURE, and it lives in the renderer rather than the sidecar for two reasons
 *  the gate enforces: the sidecar must never import numpy, and a module with no
 *  DOM and no API import can be run whole under plain node, which is how every
 *  number below is pinned.
 *
 *  PER SHARE throughout. A contract is 100 shares, but the multiplier appears
 *  on every term and cancels out of every comparison, so it is applied once at
 *  the display edge rather than threaded through the maths.
 *
 *  MID FILLS, per this workspace's standing rule. A leg whose bid is zero has
 *  no mid — see optgrid.midOf — and a structure containing one has no net,
 *  which is reported as such rather than computed from a fabricated price.
 */

export type LegSide = 'long' | 'short'
export type LegRight = 'P' | 'C'

export interface PayoffLeg {
  side: LegSide
  right: LegRight
  strike: number
  /** Mid premium per share. Null when the leg has no usable quote. */
  premium: number | null
}

/** An extreme that does not exist as a number.
 *
 *  A naked short call loses without limit as the underlying rises; a long call
 *  gains without limit. Rendering either as a large number would be a lie with
 *  a decimal point on it, so the bound is absent and says why. */
export interface Extreme {
  bounded: boolean
  /** Per share. Null exactly when bounded is false. */
  value: number | null
  /** Where it occurs, when that is a single underlying price. */
  at?: number
}

export interface Payoff {
  /** Positive is a CREDIT you receive, negative a DEBIT you pay. Per share. */
  net: number | null
  maxProfit: Extreme
  maxLoss: Extreme
  /** Underlying prices where the structure breaks even, ascending. */
  breakevens: number[]
  /** Present exactly when the structure could not be evaluated. */
  reason?: string
}

/** One leg's contribution to intrinsic value at expiry, per share. */
function intrinsic(leg: PayoffLeg, s: number): number {
  const iv = leg.right === 'C' ? Math.max(s - leg.strike, 0) : Math.max(leg.strike - s, 0)
  return (leg.side === 'long' ? 1 : -1) * iv
}

/** Cash at entry: a short leg brings premium in, a long leg pays it out. */
function cashflow(legs: PayoffLeg[]): number {
  let net = 0
  for (const l of legs) {
    if (l.premium === null) continue
    net += (l.side === 'short' ? 1 : -1) * l.premium
  }
  return net
}

/** Profit per share if the underlying settles at `s`. */
export function payoffAt(legs: PayoffLeg[], s: number): number {
  let total = cashflow(legs)
  for (const l of legs) total += intrinsic(l, s)
  return total
}

/** Slope of the payoff above the highest strike.
 *
 *  Only calls have any: every put is worthless up there, so the far-right
 *  behaviour is decided entirely by the net long-call count. Positive means
 *  profit grows without limit; negative means loss does. */
export function upperSlope(legs: PayoffLeg[]): number {
  let k = 0
  for (const l of legs) if (l.right === 'C') k += l.side === 'long' ? 1 : -1
  return k
}

/** Everything about the structure at expiry.
 *
 *  The payoff is piecewise linear with kinks only at strikes, so evaluating at
 *  0, at every strike, and just past the highest one finds every extreme
 *  exactly — no sampling, no grid, nothing to miss between samples.
 *
 *  ZERO is a real evaluation point, not a convenience: an underlying cannot go
 *  below it, which is precisely why the DOWNSIDE of any structure is always
 *  bounded and only the upside can run away. */
export function analyse(legs: PayoffLeg[]): Payoff {
  const none: Extreme = { bounded: false, value: null }
  if (legs.length === 0) {
    return { net: null, maxProfit: none, maxLoss: none, breakevens: [],
             reason: 'no legs' }
  }
  const unpriced = legs.filter((l) => l.premium === null).length
  if (unpriced > 0) {
    // Reported, never estimated. A structure priced from a leg nobody is
    // bidding on produces a max profit that cannot be collected.
    return {
      net: null, maxProfit: none, maxLoss: none, breakevens: [],
      reason: `no mid on ${unpriced} leg${unpriced > 1 ? 's' : ''} — nothing is `
        + 'bidding, so this structure has no price',
    }
  }
  if (legs.some((l) => !Number.isFinite(l.strike) || l.strike <= 0)) {
    return { net: null, maxProfit: none, maxLoss: none, breakevens: [],
             reason: 'a leg has no usable strike' }
  }

  const strikes = [...new Set(legs.map((l) => l.strike))].sort((a, b) => a - b)
  const slope = upperSlope(legs)
  // One step past the last kink. Any positive step gives the same VERDICT
  // (bounded or not) because the function is linear from there on; the size
  // only affects a number that is discarded when unbounded.
  const beyond = strikes[strikes.length - 1] + Math.max(1, strikes[0] * 0.5)
  const points = [0, ...strikes, beyond]
  const values = points.map((s) => payoffAt(legs, s))

  let hiAt = 0
  let loAt = 0
  for (let i = 1; i < values.length; i += 1) {
    if (values[i] > values[hiAt]) hiAt = i
    if (values[i] < values[loAt]) loAt = i
  }

  const maxProfit: Extreme = slope > 0
    ? { bounded: false, value: null }
    : { bounded: true, value: values[hiAt], at: points[hiAt] }
  const maxLoss: Extreme = slope < 0
    ? { bounded: false, value: null }
    : { bounded: true, value: values[loAt], at: points[loAt] }

  return { net: cashflow(legs), maxProfit, maxLoss, breakevens: breakevens(legs, points, values) }
}

/** Underlying prices where profit crosses zero.
 *
 *  Linear interpolation between adjacent breakpoints is EXACT here, not an
 *  approximation: the payoff has no curvature between strikes, so the straight
 *  line between two evaluations is the function itself. */
function breakevens(legs: PayoffLeg[], points: number[], values: number[]): number[] {
  const out: number[] = []
  for (let i = 0; i < points.length - 1; i += 1) {
    const a = values[i]
    const b = values[i + 1]
    if (a === 0) out.push(points[i])
    if ((a < 0 && b > 0) || (a > 0 && b < 0)) {
      out.push(points[i] + ((points[i + 1] - points[i]) * -a) / (b - a))
    }
  }
  const last = values[values.length - 1]
  if (last === 0) out.push(points[points.length - 1])
  // Past the final kink the line runs forever, so a crossing can live beyond
  // every evaluated point — a covered call whose breakeven sits above the
  // highest strike would otherwise simply not be reported.
  const slope = upperSlope(legs)
  if (slope !== 0 && ((last < 0 && slope > 0) || (last > 0 && slope < 0))) {
    out.push(points[points.length - 1] + -last / slope)
  }
  return [...new Set(out.map((v) => Math.round(v * 1e6) / 1e6))].sort((a, b) => a - b)
}

/** The structure's annualised return on the capital it actually risks.
 *
 *  ONE ratio for every structure — max gain over max risk — which is what
 *  makes a spread comparable to a naked short and to the heatmap's cells:
 *    credit spread  1.50 credit against 3.50 of width  ->  42.9% for the period
 *    debit spread   3.50 max gain against a 1.50 debit -> 233%   for the period
 *    cash-secured put  6.24 against 763.76 at risk     ->   0.82% for the period
 *  That last one is deliberately the same answer the heatmap's cell gives
 *  (credit ÷ strike), because strike and strike-minus-credit differ by less
 *  than a rounding on a real quote — the two surfaces agree by construction
 *  rather than by coincidence.
 *
 *  Compounded to a year through the SAME annualise() the grid uses, cap and
 *  all, so the panel and the cells can never disagree about what a year means.
 *
 *  Null when either end is unbounded, and that is the honest answer rather
 *  than an inconvenience: a naked short call risks without limit, so there is
 *  no denominator, and a long call gains without limit, so there is no
 *  numerator. A percentage either way would be invented. */
export function returnOnRisk(p: Payoff): number | null {
  if (!p.maxProfit.bounded || p.maxProfit.value === null) return null
  if (!p.maxLoss.bounded || p.maxLoss.value === null) return null
  const risk = Math.abs(p.maxLoss.value)
  const gain = p.maxProfit.value
  if (!(risk > 0) || gain <= 0) return null
  return gain / risk
}

// There is deliberately no annualised twin of returnOnRisk. A per-year figure
// on ONE position answered a question nobody asks of a single trade, and the
// compounded version of it quoted 831% on a real spread — a rate assuming eight
// consecutive winning rolls. Cross-tenor comparison lives in the heatmap, where
// every cell carries the delta that qualifies it.

/** Dollars for one contract of a per-share figure. Applied at the display
 *  edge only, so nothing upstream has to carry the multiplier. */
export const CONTRACT_MULTIPLIER = 100

export function fmtExtreme(e: Extreme, perContract = true): string {
  if (!e.bounded || e.value === null) return 'unlimited'
  const v = e.value * (perContract ? CONTRACT_MULTIPLIER : 1)
  return `${v < 0 ? '-' : ''}$${Math.abs(v).toFixed(2)}`
}

export function fmtNet(net: number | null, perContract = true): string {
  if (net === null) return '—'
  const v = net * (perContract ? CONTRACT_MULTIPLIER : 1)
  if (v === 0) return 'even'
  return `$${Math.abs(v).toFixed(2)} ${v > 0 ? 'credit' : 'debit'}`
}
