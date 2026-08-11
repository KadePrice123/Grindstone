/** The options heatmap: strike x expiration, one cell per contract.
 *
 *  PURE, and deliberately free of any DOM or API import, so the gate can run
 *  every number in here under plain node — the same rule ChartDraw.ts lives by.
 *
 *  WHAT A CELL SAYS. Two different questions, two different channels, because
 *  one number cannot answer both:
 *    the VALUE is the mid — the debit you would pay or the credit you would
 *      take, which is the number you actually transact at;
 *    the COLOUR is the ANNUALISED yield that mid represents, which is what
 *      makes a 5-DTE contract comparable to a 30-DTE one at all. Raw premium
 *      across expirations only ever draws the term structure back at you.
 *
 *  AND THE DELTA RIDES ALONG, uncoloured. Annualised yield alone always
 *  flatters the front week: a 5-DTE credit compounds over ~73 rolls a year and
 *  the arithmetic quietly assumes every one of them expires worthless. This
 *  workspace's own live-ticket calibration (2026-07-28) found short-tenor
 *  selling to be structurally sell-low/mark-high, so the surface must not
 *  re-tell that story in colour with the risk hidden. Yield says what you are
 *  paid; delta says what you are paid FOR, and both are on the cell.
 */

export type LegSide = 'long' | 'short'
export type LegRight = 'P' | 'C'

/** Trading days in a year. Compounding runs on these, not on 365.
 *
 *  A position can only be rolled when the market is open, so the number of
 *  times a 5-day trade repeats in a year is set by sessions, not by dates. For
 *  a span whose weekends fall proportionally the two conventions agree almost
 *  exactly — but they diverge precisely where it matters, at short tenors: an
 *  11-day window covering two weekends holds 7 sessions, one covering one holds
 *  8, and a calendar denominator prices those identically when they are a
 *  seventh apart in reality. */
export const TRADING_DAYS_PER_YEAR = 252

/** Why a cell has no number, when it has none. Never collapsed into "0". */
export type CellState = 'priced' | 'no-bid' | 'no-quote'

export interface GridContract {
  occ_symbol: string
  expiration: string
  strike: number
  right: LegRight
  bid?: number | null
  ask?: number | null
  delta?: number | null
  iv?: number | null
}

export interface GridCell {
  contract: GridContract
  state: CellState
  /** (bid+ask)/2, or null unless state === 'priced'. */
  mid: number | null
  /** Compounded annual rate this mid represents on its capital base. */
  annual: number | null
  /** CALENDAR days, which is how expirations are quoted and read. */
  dte: number
  /** SESSIONS, which is what the compounding above actually divides by. */
  tdte: number
}

export interface OptGrid {
  /** Descending, so the highest strike is the top row — as a chain reads. */
  strikes: number[]
  /** Ascending by date; each carries its own DTE. */
  columns: { expiration: string; dte: number }[]
  /** cells[strike][expiration] — sparse: not every pair is listed. */
  cells: Map<string, GridCell>
  /** Extremes over PRICED cells only, for the colour ramp. */
  annualLo: number | null
  annualHi: number | null
}

export const cellKey = (strike: number, expiration: string): string =>
  `${strike}|${expiration}`

/** Calendar days from `from` to `to`, both 'YYYY-MM-DD'. */
export function dteBetween(from: string, to: string): number | null {
  const a = Date.parse(from + 'T00:00:00Z')
  const b = Date.parse(to + 'T00:00:00Z')
  if (!Number.isFinite(a) || !Number.isFinite(b)) return null
  return Math.round((b - a) / 86400_000)
}

/** The mid, or null — and a zero bid yields NULL, not half the ask.
 *
 *  This is the single most load-bearing rule on the surface. A contract nobody
 *  is bidding on has no mid; averaging a fabricated zero against a real ask
 *  produces a confident half-price that is worst exactly where it is most
 *  tempting — out in the wings, which is also where the eye-catching kinks
 *  live. A cell with no mid renders as no mid and no colour. */
export function midOf(bid: number | null | undefined, ask: number | null | undefined): number | null {
  if (typeof bid !== 'number' || typeof ask !== 'number') return null
  if (!Number.isFinite(bid) || !Number.isFinite(ask)) return null
  if (bid <= 0) return null
  if (ask < bid) return null // crossed book: not a market, not an average
  return (bid + ask) / 2
}

export function cellStateOf(c: GridContract): CellState {
  if (typeof c.ask !== 'number' || !Number.isFinite(c.ask)) return 'no-quote'
  if (typeof c.bid !== 'number' || !Number.isFinite(c.bid) || c.bid <= 0) return 'no-bid'
  return 'priced'
}

/** The capital one share of this contract ties up: THE STRIKE.
 *
 *  Everything here is per share, and that is the whole simplification. A
 *  contract's premium and its strike are both per-share quotes, so the
 *  hundred-multiplier appears on both sides of premium/capital and cancels —
 *  carrying it was arithmetic that did nothing but invite a units mistake.
 *
 *  The strike is also the right denominator for the comparison being made.
 *  Kade's question is whether a higher strike is genuinely better than a lower
 *  one, and a bare credit cannot answer it: a 770 put paying 13.39 and a 766
 *  put paying 12.07 look four cents apart per point of strike until you divide.
 *  1.739% against 1.576% is the real gap, and it is the gap the colour shows.
 *
 *  Deliberately NOT a broker buying-power model. A real Reg-T requirement for
 *  a naked short is a max() of two formulas plus premium plus a floor, it
 *  differs per broker and per account type, and it changes as the underlying
 *  moves — so a surface built on it would compare cells against a moving
 *  target and be wrong the moment the account type differed. The strike is
 *  fixed, known, identical across brokers, and scales exactly with the capital
 *  a cash-secured position actually ties up. */
export function capitalFor(strike: number): number | null {
  if (!Number.isFinite(strike) || strike <= 0) return null
  return strike
}

/** Annual rate from one period's return — SIMPLE scaling, not compounded.
 *
 *  This was compounded, and compounding made the number useless. A defined-risk
 *  spread returning 31.6% over 31 sessions compounds to 831%/yr, and that
 *  figure is not wrong so much as it is unearnable: it assumes the position is
 *  rolled eight times AND wins every one of them. Nobody sells premium at a
 *  100% hit rate, so the surface was quoting a rate that cannot be collected,
 *  which is the exact class of good-looking number this project distrusts.
 *
 *  Linear scaling asks the answerable question instead — "at this rate per
 *  session, what does a year of it come to" — and leaves the win rate where it
 *  belongs: in the delta on the cell, next to the yield it qualifies.
 *
 *  The cap survives the change, smaller but still load-bearing: a 0DTE credit
 *  scaled by 252 sessions still reaches numbers that are noise rather than
 *  information, and those are reported AS capped rather than plotted. */
export const ANNUAL_CAP = 100 // 10,000%/yr

export function annualise(periodReturn: number, tradingDays: number): number | null {
  if (!Number.isFinite(periodReturn) || periodReturn <= -1) return null
  if (!Number.isFinite(tradingDays) || tradingDays <= 0) return null
  const r = periodReturn * (TRADING_DAYS_PER_YEAR / tradingDays)
  if (!Number.isFinite(r)) return ANNUAL_CAP
  return Math.min(r, ANNUAL_CAP)
}

/** Sessions between two dates, floored at one.
 *
 *  Zero would divide by nothing and a same-day expiry is still one session of
 *  risk, so 0DTE annualises as a single trading day rather than refusing.
 *  Holidays are not modelled — see tradingDayOffset, where that trade is
 *  argued — which costs at most one session inside a span. Worth noting the
 *  split: the DTE TOLERANCE stays in calendar days deliberately (an approximate
 *  holiday table must never change which contracts MATCH), while compounding
 *  uses sessions, where a stale holiday moves a yield by a fraction of a
 *  percent and nothing else. */
export function tradingDaysTo(today: string, expiration: string): number | null {
  const a = Date.parse(today + 'T00:00:00Z')
  const b = Date.parse(expiration + 'T00:00:00Z')
  if (!Number.isFinite(a) || !Number.isFinite(b)) return null
  let n = 0
  for (let t = a + 86400_000; t <= b; t += 86400_000) {
    const dow = new Date(t).getUTCDay()
    if (dow !== 0 && dow !== 6) n += 1
  }
  return Math.max(1, n)
}
// NOTE: this repeats ChartDraw's tradingDayOffset rather than importing it, and
// the reason is module resolution, not preference. This file must load under
// plain node for the gate to run its arithmetic, and node cannot resolve the
// extensionless import that Vite rewrites — while ChartDraw itself pulls in the
// charting library. Two walks would normally be two places to drift, so the
// gate asserts they agree across a span containing weekends. Change one, the
// check fails until you change the other.

/** One cell's annualised rate: premium over strike, compounded over SESSIONS.
 *
 *  Per share on both sides, so no multiplier appears — see capitalFor. */
export function annualYield(
  mid: number | null, capital: number | null, tradingDays: number
): number | null {
  if (mid === null || capital === null || capital <= 0) return null
  return annualise(mid / capital, tradingDays)
}

/** 'YYYY-MM-DD' plus n calendar days, or null if the date is unparseable. */
export function addDays(date: string, n: number): string | null {
  const t = Date.parse(date + 'T00:00:00Z')
  if (!Number.isFinite(t)) return null
  return new Date(t + n * 86400_000).toISOString().slice(0, 10)
}

/** The same yield a heatmap cell shows, for a contract quoted on some PAST day.
 *
 *  This is the history chart's entry into this file's arithmetic, and it lives
 *  here rather than on the page for one reason: the two surfaces were read
 *  against each other and disagreed. A heatmap cell said 2.2% while the
 *  history chart said 0.9937% for the same 80P — both correct, one annualised
 *  and one not. Offering the annualised unit on the chart is only honest if it
 *  is THE SAME FUNCTION, so a future change to the convention (this file has
 *  already moved from compounded to linear once) cannot land on one panel and
 *  miss the other.
 *
 *  Takes the quote day and that day's CALENDAR dte, because every point in a
 *  delta-matched series is a different contract with its own tenor — the scale
 *  factor is per-point, not a constant the axis could be relabelled with. */
export function annualYieldOn(
  mid: number | null, strike: number, from: string, calendarDte: number
): number | null {
  const exp = addDays(from, calendarDte)
  if (exp === null) return null
  const sessions = tradingDaysTo(from, exp)
  if (sessions === null) return null
  return annualYield(mid, capitalFor(strike), sessions)
}

/** One day of a picked contract's exit view: what closing the position costs
 *  and what that leaves you, from the entry day forward. */
export interface ExitPoint {
  date: string
  /** Cost to close at mid, per share — null on one-sided days (a gap). */
  mark: number | null
  /** P&L as % of the capital the contract ties up (capitalFor: the strike —
   *  the cash-secured buying-power effect, and the SAME denominator as the
   *  heatmap, so this panel and that one can be read against each other). */
  pnlPct: number | null
  dte: number
}

export interface ExitSeries {
  /** The opening trade, priced at the clicked day's mid. */
  entry: { date: string; premium: number; dte: number }
  points: ExitPoint[]
  /** The most recent day with a two-sided market, or null if none followed. */
  latest: { date: string; mark: number; pnlPct: number } | null
}

/** The exit view of one archived contract, from a clicked entry day.
 *
 *  Everything is per share (see capitalFor). SIGNS: for a short, entry premium
 *  is CREDIT RECEIVED and the mark is the buy-back cost, so P&L per share is
 *  credit − mark; a long flips it. The percentage divides by the strike — the
 *  cash-secured buying-power effect — NOT by max loss or a margin model, for
 *  capitalFor's own documented reasons.
 *
 *  Returns null when the entry day has no two-sided mid: a position that could
 *  not have been opened at a real price has no honest P&L series, and the
 *  caller reports that instead of charting from a made-up entry. Days after
 *  entry with no mid stay as GAPS (null mark/pnlPct), same as every other
 *  chart in this app — a missing quote is not a zero. */
export function exitSeries(
  rows: { date: string; mid: number | null; dte: number }[],
  entryDate: string, side: LegSide, strike: number
): ExitSeries | null {
  const capital = capitalFor(strike)
  if (capital === null) return null
  const entry = rows.find((r) => r.date === entryDate)
  if (!entry || entry.mid === null) return null
  const premium = entry.mid
  const points: ExitPoint[] = rows
    .filter((r) => r.date >= entryDate)
    .map((r) => {
      const pnl = r.mid === null ? null
        : side === 'short' ? premium - r.mid : r.mid - premium
      return {
        date: r.date,
        mark: r.mid,
        pnlPct: pnl === null ? null : (pnl / capital) * 100,
        dte: r.dte,
      }
    })
  let latest: ExitSeries['latest'] = null
  for (const p of points) {
    if (p.mark !== null && p.pnlPct !== null) latest = { date: p.date, mark: p.mark, pnlPct: p.pnlPct }
  }
  return { entry: { date: entryDate, premium, dte: entry.dte }, points, latest }
}

/** Build the grid from whatever contracts the filter returned.
 *
 *  Sparse on purpose: the endpoint returns the contracts inside the leg's
 *  window, and a strike listed for one expiration is often absent from
 *  another. An empty cell means "not listed", which is a true statement and a
 *  different one from "no bid". */
export function buildGrid(
  contracts: GridContract[],
  opts: { today: string; side: LegSide }
): OptGrid {
  const cells = new Map<string, GridCell>()
  const strikeSet = new Set<number>()
  const expSet = new Map<string, number>()
  let lo: number | null = null
  let hi: number | null = null

  for (const c of contracts) {
    const dte = dteBetween(opts.today, c.expiration)
    const tdte = tradingDaysTo(opts.today, c.expiration)
    if (dte === null || tdte === null) continue
    const state = cellStateOf(c)
    const mid = state === 'priced' ? midOf(c.bid, c.ask) : null
    // Sessions, not calendar days — the rate is what you could repeat, and you
    // can only repeat it when the market opens.
    const annual = annualYield(mid, capitalFor(c.strike), tdte)
    strikeSet.add(c.strike)
    expSet.set(c.expiration, dte)
    cells.set(cellKey(c.strike, c.expiration), { contract: c, state, mid, annual, dte, tdte })
    if (annual !== null) {
      lo = lo === null ? annual : Math.min(lo, annual)
      hi = hi === null ? annual : Math.max(hi, annual)
    }
  }

  return {
    strikes: [...strikeSet].sort((a, b) => b - a),
    columns: [...expSet.entries()]
      .map(([expiration, dte]) => ({ expiration, dte }))
      .sort((a, b) => a.dte - b.dte),
    cells,
    annualLo: lo,
    annualHi: hi,
  }
}

/** Where a cell sits on the ramp, 0..1 — MAGNITUDE ONLY.
 *
 *  Two channels, and keeping them separate is what makes the surface readable:
 *  the HUE carries the sign (a credit is green, a debit is red, the same
 *  vocabulary the rest of the app uses for money in and money out), and this
 *  INTENSITY carries how big the annualised rate is. So a bright green cell is
 *  a rich credit and a bright red one is an expensive debit — "more colour"
 *  always means "more of what the hue already told you", and never has to be
 *  reinterpreted per leg.
 *
 *  Colour is never the only channel: every priced cell also prints its mid and
 *  its rate. That is what keeps the surface legible to a red-green colourblind
 *  reader, for whom this palette is otherwise the worst possible choice.
 *
 *  Null when there is nothing to compare against — a single priced cell has no
 *  spread to sit within, and stretching a ramp across one value would paint it
 *  "best" on no evidence. */
export function rampPosition(
  annual: number | null, lo: number | null, hi: number | null
): number | null {
  if (annual === null || lo === null || hi === null) return null
  if (!(hi > lo)) return null
  return (annual - lo) / (hi - lo)
}

/** The theme token a side's money moves through: in is a gain, out is a loss. */
export const sideInk = (side: LegSide): string =>
  side === 'short' ? 'var(--gain)' : 'var(--loss)'

/** Percent, at the precision the number can actually carry. */
export function fmtAnnual(annual: number | null): string {
  if (annual === null) return '—'
  if (annual >= ANNUAL_CAP) return '>10000%'
  const pct = annual * 100
  if (Math.abs(pct) >= 1000) return `${Math.round(pct)}%`
  if (Math.abs(pct) >= 100) return `${pct.toFixed(0)}%`
  return `${pct.toFixed(1)}%`
}
