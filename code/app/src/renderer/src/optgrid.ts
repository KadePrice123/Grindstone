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

/** Shares per option contract. This file is per-share EVERYWHERE else on
 *  purpose (see capitalFor: the multiplier cancels out of every ratio, and
 *  carrying it was arithmetic that did nothing but invite a units mistake).
 *
 *  The exit view is the ONE place it must appear, because a closed trade's
 *  P&L is the number a trader reads in dollars — "26.10" is a quote, "$2,610"
 *  is the money. Kade read a 13.25% against a right-hand dollar axis and got
 *  ~55; the fix is not a better axis, it is showing the actual dollars. */
export const CONTRACT_MULTIPLIER = 100

/** One day of a picked contract's exit view: what closing the position costs
 *  and what that leaves you, from the entry day forward. */
export interface ExitPoint {
  date: string
  /** Cost to close at mid, per share — null on one-sided days (a gap). */
  mark: number | null
  /** The part of `mark` that is REAL MONEYNESS: max(0, strike − spot) for a
   *  put, max(0, spot − strike) for a call. It does not decay. null when the
   *  underlying's close for this day is unknown — an unknown split is not a
   *  zero split, and drawing 0 would claim the position was safely OTM. */
  intrinsic: number | null
  /** The rest: TIME VALUE, which decays to zero by expiry. This is what a
   *  premium seller actually sold. Clamped at 0 — a mark below intrinsic is
   *  a stale/one-sided quote, not negative time value. */
  extrinsic: number | null
  /** ASSIGNMENT ODDS for this day, as a percentage: |delta| × 100.
   *
   *  Delta is the market's own estimate of the chance this contract finishes
   *  in the money, which is the number that says how close assignment is —
   *  and unlike intrinsic it MOVES for the whole life of an out-of-the-money
   *  trade (Kade's 150P ran 25% at entry to 2% at the end while intrinsic sat
   *  flat at zero the entire time). Recorded per day by the feed, not modelled
   *  here: null when the feed omitted the greek, never a guess. */
  assignPct: number | null
  /** P&L in dollars for ONE CONTRACT — credit received minus buy-back cost,
   *  times the multiplier. The headline number. */
  pnl: number | null
  /** The same P&L as % of the capital the contract ties up (capitalFor: the
   *  strike — the cash-secured buying-power effect, and the SAME denominator
   *  as the heatmap, so this panel and that one can be read against each
   *  other). Identical per share or per contract: the x100 cancels. */
  pnlPct: number | null
  dte: number
}

export interface ExitSeries {
  /** The opening trade, priced at the clicked day's mid. `credit` is the same
   *  premium in ONE CONTRACT'S dollars — what actually hit the account. */
  entry: { date: string; premium: number; credit: number; dte: number }
  points: ExitPoint[]
  /** The most recent day with a two-sided market, or null if none followed. */
  latest: {
    date: string; mark: number; cost: number; pnl: number; pnlPct: number
    intrinsic: number | null; extrinsic: number | null
  } | null
  /** The deepest the position ever went in the money, over the days whose
   *  spot is known — the danger P&L cannot show, since a trade can end green
   *  after a deep excursion. `intrinsic: 0` means it never crossed; null
   *  means no day's underlying close was available to judge. */
  worstIntrinsic: { date: string; intrinsic: number } | null
  /** Assignment odds at entry and at the latest print, plus the scariest day
   *  the trade ever saw. The peak is the point: a trade that ended at 2% may
   *  have been at 40% in the middle, and only the peak says so. */
  odds: {
    entry: number | null
    latest: { date: string; pct: number } | null
    peak: { date: string; pct: number } | null
  }
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
export function intrinsicOf(
  spot: number | null | undefined, strike: number, right: 'P' | 'C'
): number | null {
  if (typeof spot !== 'number' || !Number.isFinite(spot)) return null
  return Math.max(0, right === 'P' ? strike - spot : spot - strike)
}

export function exitSeries(
  rows: { date: string; mid: number | null; dte: number; delta?: number | null }[],
  entryDate: string, side: LegSide, strike: number,
  /** The underlying's close per date, for the intrinsic/extrinsic split.
   *  Optional: without it both come back null and the panel draws the mark
   *  undecomposed, which is what it did before the split existed. */
  spots?: Map<string, number>, right: 'P' | 'C' = 'P'
): ExitSeries | null {
  const capital = capitalFor(strike)
  if (capital === null) return null
  const entry = rows.find((r) => r.date === entryDate)
  if (!entry || entry.mid === null) return null
  const premium = entry.mid
  const points: ExitPoint[] = rows
    .filter((r) => r.date >= entryDate)
    .map((r) => {
      // Per share first, because that is the unit both quotes are in; the
      // multiplier lands once, on the way out.
      const per = r.mid === null ? null
        : side === 'short' ? premium - r.mid : r.mid - premium
      // THE SPLIT. Intrinsic is what the contract is worth on moneyness
      // alone and does NOT decay; extrinsic is the time value that goes to
      // zero by expiry. A premium seller sold the extrinsic — so "is the
      // cost to close going to evaporate?" is exactly this decomposition.
      // Both null without a spot: an unknown split must not read as 0
      // intrinsic, which would claim the position was safely out of the money.
      const intr = intrinsicOf(spots?.get(r.date), strike, right)
      return {
        date: r.date,
        mark: r.mid,
        intrinsic: r.mid === null ? null : intr,
        // Clamped: a mark below parity is a stale or one-sided quote, not
        // negative time value, and a dip below zero here would draw the
        // extrinsic band inverted.
        extrinsic: r.mid === null || intr === null ? null : Math.max(0, r.mid - intr),
        // The greek stands on its own: an assignment reading is valid on a
        // day whose market was one-sided (no mid), because the feed's delta
        // does not depend on our being able to price a close.
        assignPct: typeof r.delta === 'number' && Number.isFinite(r.delta)
          ? Math.abs(r.delta) * 100 : null,
        pnl: per === null ? null : per * CONTRACT_MULTIPLIER,
        pnlPct: per === null ? null : (per / capital) * 100,
        dte: r.dte,
      }
    })
  let latest: ExitSeries['latest'] = null
  for (const p of points) {
    if (p.mark !== null && p.pnl !== null && p.pnlPct !== null) {
      latest = {
        date: p.date, mark: p.mark,
        cost: p.mark * CONTRACT_MULTIPLIER,
        pnl: p.pnl, pnlPct: p.pnlPct,
        intrinsic: p.intrinsic, extrinsic: p.extrinsic,
      }
    }
  }
  // DID IT EVER GO IN THE MONEY? The excursion P&L hides: a position can
  // finish green having been deep ITM and come back, and that is a different
  // trade from one that never got close. Measured over days we actually know
  // the spot for; null when we know none of them.
  let worst: ExitSeries['worstIntrinsic'] = null
  for (const p of points) {
    if (p.intrinsic === null) continue
    if (worst === null || p.intrinsic > worst.intrinsic) {
      worst = { date: p.date, intrinsic: p.intrinsic }
    }
  }
  // ASSIGNMENT ODDS: at entry, now, and at the worst moment. The peak is what
  // the other two cannot show — a trade that opened at 25% and closed at 2%
  // may have touched 60% in between, and that excursion is the risk actually
  // taken rather than the risk it happened to end on.
  let oddsLatest: { date: string; pct: number } | null = null
  let oddsPeak: { date: string; pct: number } | null = null
  for (const p of points) {
    if (p.assignPct === null) continue
    oddsLatest = { date: p.date, pct: p.assignPct }
    if (oddsPeak === null || p.assignPct > oddsPeak.pct) {
      oddsPeak = { date: p.date, pct: p.assignPct }
    }
  }
  return {
    entry: {
      date: entryDate, premium,
      credit: premium * CONTRACT_MULTIPLIER,
      dte: entry.dte,
    },
    points,
    latest,
    worstIntrinsic: worst,
    odds: {
      entry: points[0]?.assignPct ?? null,
      latest: oddsLatest,
      peak: oddsPeak,
    },
  }
}

// ---------------------------------------------------------------------------
// THE INSURE PAGE'S ARITHMETIC (docs/INSURE.md). Appended HERE, not in a twin
// file, so the annualisation convention cannot fork: the scanner's axes and
// the heatmap's cells go through the same annualise/tradingDaysTo or they do
// not compile.

/** One risk class's measured history, as the scan endpoint returns it —
 *  RAW FRACTIONS. Every %/yr on screen derives here, client-side. */
export interface MeasuredClass {
  n_exp: number
  n_days: number
  episodes?: number
  claim_freq?: number
  implied?: number | null
  expected_loss_pct?: number | null
  win_rate?: number | null
  wl_ratio?: number | null
  win_at_offer?: number | null
  severity?: { mean: number; p95: number; worst: number; worst_date: string } | null
  window?: { first: string; last: string }
  zero_claims_reason?: string
  rule_of_three?: number
}

/** The measured pure premium — the credit at which this insurance has
 *  historically broken even. Null when the class saw zero claims: an
 *  unobserved tail is unmeasurable, never free (the zero-claims rule). */
export function requiredCreditPct(m: MeasuredClass | null | undefined): number | null {
  if (!m || typeof m.expected_loss_pct !== 'number') return null
  return m.expected_loss_pct
}

/** offered − required, same units. Null propagates: an edge that cannot be
 *  computed is absent, never zero. */
export function edgePct(offered: number | null, required: number | null): number | null {
  if (offered === null || required === null) return null
  return offered - required
}

/** How much a dot's measurement is worth: solid ≥20 expirations, thin 8–19,
 *  below that nothing — a dot IS a claim of measurement. */
export function confidenceOf(nExp: number): 'solid' | 'thin' | 'none' {
  return nExp >= 20 ? 'solid' : nExp >= 8 ? 'thin' : 'none'
}

/** One dot on the insurance line: x = required, y = offered, both annualised
 *  through the SAME scale factor — which is what makes the 45° diagonal the
 *  fair price by construction. Refuses (null) when either side is missing or
 *  the tenor cannot be counted. */
export function insurePoint(
  offeredPct: number | null, requiredPct: number | null,
  today: string, expiration: string
): { x: number; y: number; edgeAnnual: number } | null {
  if (offeredPct === null || requiredPct === null) return null
  const sessions = tradingDaysTo(today, expiration)
  if (sessions === null) return null
  const x = annualise(requiredPct, sessions)
  const y = annualise(offeredPct, sessions)
  if (x === null || y === null) return null
  return { x: x * 100, y: y * 100, edgeAnnual: (y - x) * 100 }
}

/** The dot's ring: measured claim frequency in three discrete steps —
 *  assignment risk visible on the plane without a second axis. */
export function claimRing(claimFreq: number | null | undefined): 0 | 1 | 2 {
  if (typeof claimFreq !== 'number' || claimFreq < 0.10) return 0
  return claimFreq <= 0.25 ? 1 : 2
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
