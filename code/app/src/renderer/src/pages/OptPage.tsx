/** The options workstation — `opt.gs?s=SPY`, addressed as "SPY Opt".
 *
 *  AN EXTENSION OF THE SYMBOL TAB. The legs come from that symbol's daily
 *  chart bucket (polled, so dragging a zone on the chart moves this page), the
 *  selector on the right rail chooses ONE contract, and the charts on the left
 *  answer two questions about it:
 *
 *    FUTURE   the term structure — this strike at every listed expiration,
 *             so "is 15 DTE better than 5" is one line read left to right.
 *    HISTORY  this contract's own archived life: its price day by day with
 *             the underlying as an optional indicator on the right axis, and
 *             the FAN CHART — its bid-ask spread at each DTE it has lived
 *             through, drawn over the archive-wide percentile bands of
 *             similar contracts. Bands from 16 years of daily chains; Alpaca
 *             sells no historical option quotes, so this comes from the
 *             imported archive and says so when the archive is not loaded.
 *
 *  THE RESOLUTION TRAP, learned the hard way: a stored leg's `side`/`strike`
 *  are BIRTH values. The four bounding lines are the real interface — their
 *  vertical order IS the side, their midpoint IS the strike — and syncLegs
 *  deliberately never refreshes a leg that has only its own lines. Everything
 *  here goes through resolveLegDoc over (leg, drawings); reading the stored
 *  fields directly once showed BUY 769.8 for a chart drawing SELL 756.1.
 *
 *  FILTERING AND ANALYTICS ONLY. Nothing here places an order.
 */
import { useEffect, useMemo, useState } from 'react'
import { api, ApiError } from '../api'
import {
  legWindow, resolveLegDoc, type Drawing, type OptionLeg,
} from '../components/ChartDraw'
import { OptHeatmap } from '../components/OptHeatmap'
import { buildChainPayload, buildContractPayload, grab, announce, occAt } from '../datapad'
import { HistoryPanel, TermPanel, type TermPoint } from '../components/OptCharts'
import {
  annualise, annualYieldOn, capitalFor, midOf, tradingDaysTo, dteBetween,
  type GridContract,
} from '../optgrid'
import { analyse, fmtExtreme, fmtNet, returnOnRisk, type PayoffLeg } from '../payoff'

interface Contract {
  occ_symbol: string
  expiration: string
  strike: number
  right: 'P' | 'C'
  bid?: number | null
  ask?: number | null
  last?: number | null
  iv?: number | null
  delta?: number | null
}

interface ChainResponse {
  underlying: string
  available: boolean
  contracts: Contract[]
  total: number
  truncated: boolean
  expirations?: string[]
  age_seconds?: number
  source: string
  reason?: string
}

interface SeriesRow {
  date: string
  mid: number | null
  bid: number | null
  ask: number | null
  spread: number | null
  used_dte: number
  used_strike: number
  used_delta?: number | null
}

interface SeriesResponse {
  available: boolean
  rows: SeriesRow[]
  source: string
  reason?: string
  mode?: 'delta' | 'strike'
  target?: { delta?: number | null; strike?: number | null; dte: number }
}

type Sel = { occ: string; strike: number; expiration: string; right: 'P' | 'C'; delta?: number | null }

/** The history chart's display unit.
 *
 *  'pct'    — premium / strike, raw. What the trade costs.
 *  'annual' — that same ratio scaled to a year over SESSIONS, which is
 *             exactly what a heatmap cell shows. What the trade yields.
 *  'usd'    — the premium itself. What you hand over. */
type HistUnit = 'pct' | 'annual' | 'usd'

/** What the history series holds fixed as it walks back through the archive.
 *
 *  'delta'  — the equivalent trade: constant risk shape, strike walks.
 *  'strike' — the actual strike: constant level, moneyness walks. */
type HistMatch = 'delta' | 'strike'

/** One point's annualised yield as a PERCENT — the heatmap cell's own number,
 *  from the heatmap's own function. The x100 is display only; every decision
 *  about the convention lives in optgrid. */
function annualPct(mid: number, strike: number, from: string, dte: number): number | null {
  const r = annualYieldOn(mid, strike, from, dte)
  return r === null ? null : r * 100
}

/** A FRACTION as a percent — for IV and other 0-1 ratios. Not to be confused
 *  with the history tab's `asPct` unit flag, which was briefly named `pct`
 *  and shadowed this out of existence inside the component. */
const pct = (v: number | null | undefined): string =>
  typeof v === 'number' && Number.isFinite(v) ? `${(v * 100).toFixed(0)}%` : '—'

function ageWords(s: number | undefined): string {
  if (typeof s !== 'number') return ''
  if (s < 45) return 'just now'
  if (s < 5400) return `${Math.round(s / 60)} min old`
  return `${Math.round(s / 3600)} h old`
}

/** An OCC symbol back into (strike, expiration, right) — what lets a leg's
 *  persisted pick select itself when the page opens. */
function parseOcc(occ: string): Sel | null {
  const m = /^[A-Z.]{1,6}(\d{2})(\d{2})(\d{2})([CP])(\d{8})$/.exec(occ)
  if (!m) return null
  return {
    occ,
    expiration: `20${m[1]}-${m[2]}-${m[3]}`,
    right: m[4] as 'P' | 'C',
    strike: Number(m[5]) / 1000,
  }
}

export function OptPage({
  symbol,
  occ,
  onNavigate,
}: {
  symbol: string
  /** A contract to open on, from `opt.gs?s=SPY&occ=…`. This is what makes
   *  "grab a contract, ask for its history" land on THAT strike and
   *  expiration instead of a blank page you then have to click your way
   *  back into. */
  occ?: string
  onNavigate?: (r: { name: 'symbol'; symbol: string }) => void
}) {
  const [legs, setLegs] = useState<OptionLeg[] | null>(null)
  const [draws, setDraws] = useState<Drawing[]>([])
  const [byLeg, setByLeg] = useState<Record<string, ChainResponse | null>>({})
  const [meta, setMeta] = useState<{ source: string; reason?: string; age?: number } | null>(null)
  const [err, setErr] = useState<string | null>(null)
  // SEPARATE from `err`: the 3s document poll clears that one on every
  // success, so a bars failure written there vanished before it could be
  // read — which is exactly how the underlying went missing in silence.
  const [barsErr, setBarsErr] = useState<string | null>(null)

  // WHAT IS CHARTED, and WHICH question is being asked of it.
  const [sel, setSel] = useState<Sel | null>(null)
  const [tab, setTab] = useState<'future' | 'history'>('future')
  const [view, setView] = useState<'heat' | 'list'>('heat')
  const [showUnder, setShowUnder] = useState(true)
  // The averaging window, in ARCHIVED POINTS not calendar days — the series
  // is one point per day the archive priced, so a 20-point average is 20
  // observations however the gaps fall.
  const [avgWin, setAvgWin] = useState(20)
  // PERCENT OF STRIKE, by default. In delta-matched mode the strike WALKS with
  // the market — 617 to 759 over one year on this series — so a flat $35
  // premium at both ends is really two different trades, and a dollar chart
  // shows a drift that is only moneyness. Dividing by the strike each point was
  // matched at removes it, and puts this chart in the same unit as the
  // heatmap's credit/strike. Dollars stay one click away: they are what you
  // actually pay, and over a few weeks the strike barely moves.
  //
  // ANNUALISED is the third, and it exists because the two panels were being
  // read against each other and did not agree: the heatmap cell for an 80P at
  // 157 DTE said 2.2% while this chart said 0.9937%, and both were right —
  // the heatmap annualises (x252/sessions) and this chart did not. Rather
  // than make the reader do the x2.23 in their head, offer the heatmap's own
  // unit here, computed by the heatmap's own function. See annualFor below.
  const [unit, setUnit] = useState<HistUnit>('pct')
  const asPct = unit !== 'usd' // percent-formatted axis: both 'pct' and 'annual'
  // WHAT THE SERIES HOLDS FIXED — see the fetch effect, where the two questions
  // are spelled out. Both the mid line and its rolling average follow this,
  // because they are the same series read two ways.
  const [match, setMatch] = useState<HistMatch>('delta')

  const [tick, setTick] = useState(0)
  const [term, setTerm] = useState<Contract[] | null>(null)
  const [series, setSeries] = useState<SeriesResponse | null>(null)
  const [bars, setBars] = useState<{ ts: string; close: number }[]>([])

  const today = new Date().toISOString().slice(0, 10)
  const key = `${symbol}|1Day`

  // ---- the chart's document, POLLED. The chart saves on a 400ms debounce and
  // this page is its extension, so it tracks rather than snapshots. Drawings
  // ride along because the leg's side/strike live in THEM.
  useEffect(() => {
    let alive = true
    let last = ''
    const pull = async () => {
      try {
        const r = await api<{ doc: { legs?: OptionLeg[]; drawings?: Drawing[] } }>(
          'GET', `/api/chart-objects?key=${encodeURIComponent(key)}`)
        if (!alive) return
        const next = JSON.stringify([r.doc?.legs ?? [], r.doc?.drawings ?? []])
        if (next !== last) {
          last = next
          setLegs(r.doc?.legs ?? [])
          setDraws(r.doc?.drawings ?? [])
        }
        setErr(null)
      } catch (e) {
        if (alive) setErr(String(e))
      }
    }
    void pull()
    const t = window.setInterval(pull, 3000)
    return () => {
      alive = false
      window.clearInterval(t)
    }
  }, [key])

  const visible = useMemo(
    () => (legs ?? []).filter((l) => !l.hidden).map((l) => ({ leg: l, r: resolveLegDoc(l, draws) })),
    [legs, draws]
  )

  // THE ADDRESS WINS. A contract named in the URL is an explicit request for
  // that strike and expiration, so it outranks the doc's persisted pick —
  // otherwise asking for one contract's history would silently show you the
  // last trade you happened to build. Runs before the pick effect below and
  // guards on `occ` alone, so it does not fight the user's later clicks.
  useEffect(() => {
    if (!occ) return
    const parsed = parseOcc(occ)
    if (parsed) setSel(parsed)
  }, [occ])

  // A persisted pick selects itself: the trade you built is what the page
  // opens on, not a blank chart waiting for a click you already made. Skipped
  // when the address named a contract — that is a more specific instruction.
  useEffect(() => {
    if (sel !== null || occ) return
    const withPick = visible.find(({ leg }) => leg.pick)
    if (!withPick) return
    const parsed = parseOcc(withPick.leg.pick as string)
    if (parsed) setSel(parsed)
  }, [visible, sel, occ])

  // ---- one chain fetch per leg window (the selector's rows) ---------------
  useEffect(() => {
    if (visible.length === 0) {
      setByLeg({})
      return
    }
    let alive = true
    ;(async () => {
      for (const { leg, r } of visible) {
        // The lines ARE the filter. The ±tolerance box is only the leg's
        // birth window, and using it here showed 8 matches while the chart
        // panel showed 204 for the same leg — two surfaces disagreeing about
        // one filter. Falls back to the tolerance box only when the leg has
        // no lines at all.
        const w = r.window ?? legWindow(r.expiration, r.strike, leg.dteTol, leg.strikeTol)
        if (!w) continue
        try {
          const res = await api<ChainResponse>(
            'GET',
            `/api/symbols/${encodeURIComponent(symbol)}/options` +
              `?exp_from=${w.expFrom}&exp_to=${w.expTo}` +
              `&strike_from=${w.strikeLo.toFixed(4)}&strike_to=${w.strikeHi.toFixed(4)}` +
              `&right=${leg.right}`
          )
          if (!alive) return
          setByLeg((cur) => ({ ...cur, [leg.id]: res }))
          setMeta({ source: res.source, reason: res.available ? undefined : res.reason, age: res.age_seconds })
        } catch (e) {
          if (!alive) return
          setMeta({ source: 'none', reason: String(e) })
        }
      }
    })()
    return () => {
      alive = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol, tick, JSON.stringify(visible.map(({ leg, r }) =>
      [leg.id, r.strike, r.expiration, JSON.stringify(r.window), leg.dteTol, leg.strikeTol]))])

  // The chain refreshes itself: quotes age whether or not the ticker page is
  // ever touched, and this page is supposed to stand on its own.
  useEffect(() => {
    const t = window.setInterval(() => setTick((n) => n + 1), 60_000)
    return () => window.clearInterval(t)
  }, [])

  // ---- FUTURE: the term structure of the selected strike ------------------
  useEffect(() => {
    if (!sel) {
      setTerm(null)
      return
    }
    let alive = true
    // A year is only the FLOOR. The heatmap charts whatever the leg windows
    // reach — a leg dragged to a 2027 LEAPS put its columns 402 days out —
    // and a term structure that stops at 365 silently drops those same
    // expirations from the Future view. Cover the furthest thing on screen.
    // (ISO date strings compare correctly as strings.)
    let end = new Date(Date.parse(today + 'T00:00:00Z') + 365 * 86400_000)
      .toISOString().slice(0, 10)
    if (sel.expiration > end) end = sel.expiration
    for (const { leg, r } of visible) {
      const far = r.window?.expTo ??
        legWindow(r.expiration, r.strike, leg.dteTol, leg.strikeTol)?.expTo
      if (far && far > end) end = far
    }
    api<ChainResponse>(
      'GET',
      `/api/symbols/${encodeURIComponent(symbol)}/options` +
        `?exp_from=${today}&exp_to=${end}` +
        `&strike_from=${(sel.strike - 0.01).toFixed(4)}&strike_to=${(sel.strike + 0.01).toFixed(4)}` +
        `&right=${sel.right}`
    )
      .then((r) => alive && setTerm(r.available ? r.contracts : []))
      .catch(() => alive && setTerm([]))
    return () => {
      alive = false
    }
    // The window keys on the furthest expiration too: dragging a leg deeper
    // into the future must widen the term structure, not wait for a reload.
  }, [symbol, sel?.strike, sel?.right, sel?.expiration, today, // eslint-disable-line react-hooks/exhaustive-deps
      JSON.stringify(visible.map(({ r }) => r.window?.expTo ?? r.expiration))])

  // The selected contract's delta — from the clicked cell, or from the live
  // rows once they land. Hoisted out of the fetch effect because the MATCH
  // control also needs it: "Δ equivalent trade" has to be able to say when
  // there is no delta to hold fixed, rather than quietly matching on strike
  // and letting the caption be the only place that admits it.
  const selDelta = useMemo(() => {
    if (!sel) return null
    const d = sel.delta ??
      Object.values(byLeg)
        .flatMap((r) => r?.contracts ?? [])
        .find((c) => c.occ_symbol === sel.occ)?.delta ?? null
    return typeof d === 'number' && Math.abs(d) > 0.005 ? d : null
  }, [sel, byLeg])

  // ---- HISTORY: the TRADE'S history, not one contract's -------------------
  // The series holds the trade's SHAPE fixed — this strike, ~this DTE — and
  // walks it back a year through the archive. One contract's own life only
  // reaches back six weeks and answered a question nobody asked; "what has
  // the ~21-DTE 765 put cost, against where SPY was" is the comparison Kade
  // actually described. The fan keeps the picked contract's spread life.
  useEffect(() => {
    if (!sel) {
      setSeries(null)
      return
    }
    let alive = true
    const dte = Math.max(1, dteBetween(today, sel.expiration) ?? 1)
    // WHICH THING IS HELD FIXED — the two questions this chart can answer, and
    // they are genuinely different, not two views of one series:
    //
    //   delta  — the EQUIVALENT TRADE. Constant |delta| holds the risk shape
    //            fixed, so the strike walks with the market (100-130 on this
    //            SPXL series) and the line is comparable to itself over a year.
    //   strike — THE ACTUAL STRIKE. What a ~220-DTE 130 put cost, whatever it
    //            was worth in moneyness terms that day. Drifts with the
    //            underlying by construction, which is the point: it shows the
    //            level you would actually have been trading at.
    //
    // Delta is the default because a fixed strike's year mostly re-plots the
    // underlying. Strike is not a fallback here — it is a question — but it
    // IS still the fallback when the feed omitted the greek, and the caption
    // reports whichever mode the backend actually answered in.
    const shape = match === 'delta' && selDelta !== null
      ? `delta=${selDelta.toFixed(4)}&delta_tol=0.08`
      : `strike=${sel.strike.toFixed(4)}&strike_tol=1`
    api<SeriesResponse>(
      'GET',
      `/api/symbols/${encodeURIComponent(symbol)}/options/serieshistory` +
        `?right=${sel.right}&dte=${dte}&dte_tol=3&${shape}`
    )
      .then((r) => alive && setSeries(r))
      .catch((e) => alive && setSeries({
        available: false, rows: [], source: 'none', reason: staleOr(e),
      }))
    return () => {
      alive = false
    }
  }, [symbol, sel?.occ, sel?.strike, sel?.expiration, sel?.right, // eslint-disable-line react-hooks/exhaustive-deps
      today, match, selDelta])

  // The underlying, for the history view's indicator only: past closes share
  // a timeline with the contract's past prices. (On the term structure the x
  // axis is days-to-expiry — a different axis, so it has no place there.)
  useEffect(() => {
    if (tab !== 'history' || bars.length > 0) return
    let alive = true
    api<{ bars: { ts: string; close: number }[] }>(
      'GET', `/api/symbols/${encodeURIComponent(symbol)}/bars?timeframe=1Day&limit=1000`)
      .then((r) => {
        if (!alive) return
        setBars(r.bars ?? [])
        setBarsErr((r.bars ?? []).length === 0 ? 'the bars endpoint returned nothing' : null)
      })
      .catch((e) => {
        if (alive) setBarsErr(staleOr(e))
      })
    return () => {
      alive = false
    }
  }, [symbol, tab, bars.length])

  // ---- the trade, priced at mid off the DERIVED side/strike ---------------
  const structure: PayoffLeg[] = visible.map(({ leg, r }) => {
    const rows = byLeg[leg.id]?.contracts ?? []
    const rep = (leg.pick && rows.find((c) => c.occ_symbol === leg.pick)) || rows[0]
    return {
      side: r.side,
      right: leg.right,
      strike: rep ? rep.strike : r.strike,
      premium: rep ? midOf(rep.bid, rep.ask) : null,
    }
  })
  const book = structure.length > 0 ? analyse(structure) : null
  const roi = book ? returnOnRisk(book) : null
  const sessions = visible.reduce<number | null>((min, { r }) => {
    const t = tradingDaysTo(today, r.expiration)
    return t === null ? min : min === null ? t : Math.min(min, t)
  }, null)
  const picked = visible.filter(
    ({ leg }) => leg.pick && (byLeg[leg.id]?.contracts ?? []).some((c) => c.occ_symbol === leg.pick)
  ).length

  // Today's live mid for the charted contract — the "compare against now"
  // rule on the history chart. Live rows first (fresh), the doc pick's rep
  // second; null with no chain, which draws no line rather than a wrong one.
  const liveMid = (() => {
    if (!sel) return null
    const row = Object.values(byLeg)
      .flatMap((r) => r?.contracts ?? [])
      .find((c) => c.occ_symbol === sel.occ)
    return row ? midOf(row.bid, row.ask) : null
  })()

  // EVERY PLOTTED POINT in the display unit, computed once. Each row carries
  // `used_strike` — the strike that day's match actually landed on — which is
  // the only honest denominator for that day's premium.
  const plotted = useMemo(
    () => (series?.rows ?? []).map((r) => ({
      date: r.date,
      value: r.mid === null || !r.used_strike ? null
        : unit === 'usd' ? r.mid
        // Annualising uses THAT DAY's own tenor: the series holds the shape
        // fixed at ~157 DTE, but "nearest listed" lands on 154-158, and a
        // day matched at 154 earns its premium over four fewer days.
        : unit === 'annual' ? annualPct(r.mid, r.used_strike, r.date, r.used_dte)
        : (r.mid / r.used_strike) * 100,
    })),
    [series, unit]
  )

  // Today's quote in that SAME unit. Comparing a live percent against a
  // historical dollar would be a category error wearing a percentile's clothes.
  const liveShown = liveMid === null || !sel || !sel.strike
    ? liveMid
    : unit === 'usd' ? liveMid
    : unit === 'annual'
      ? annualPct(liveMid, sel.strike, today, dteBetween(today, sel.expiration) ?? 0)
      : (liveMid / sel.strike) * 100

  // WHAT NORMAL LOOKS LIKE. The percentiles of this shape's own year, and
  // where today's quote falls inside them — the whole "is this a good price"
  // question, answered from the series already on screen rather than a second
  // request. Nulls all the way down when there is nothing to compare.
  const hist = (() => {
    const mids = plotted
      .map((r) => r.value)
      .filter((v): v is number => typeof v === 'number')
      .sort((a, b) => a - b)
    if (mids.length < 10) return { median: null, p25: null, p75: null, pct: null, n: mids.length }
    const q = (f: number): number => mids[Math.min(mids.length - 1, Math.floor(f * mids.length))]
    const rank = liveShown === null ? null
      : Math.round((mids.filter((v) => v <= liveShown).length / mids.length) * 100)
    return { median: q(0.5), p25: q(0.25), p75: q(0.75), pct: rank, n: mids.length }
  })()

  // The rolling average of the option's own mid — "normal" as a line rather
  // than a single flat level, so it tracks how normal itself moved.
  const avgSeries = useMemo(() => {
    const rows = plotted
    if (avgWin <= 1 || rows.length === 0) return []
    const out: { date: string; value: number | null }[] = []
    const win: number[] = []
    for (const r of rows) {
      if (typeof r.value === 'number') {
        win.push(r.value)
        if (win.length > avgWin) win.shift()
      }
      // Nothing until the window is genuinely full: a 3-point "20-day
      // average" is a different statistic wearing the same label.
      out.push({
        date: r.date,
        value: win.length === avgWin ? win.reduce((a, b) => a + b, 0) / avgWin : null,
      })
    }
    return out
  }, [plotted, avgWin])

  // Memoised: this array is a dependency of the chart's rebuild effect, and a
  // fresh identity every render tore down and re-created the chart on every
  // 3s poll tick.
  const underSeries = useMemo(
    () => (showUnder && series?.rows ? underPoints(bars, series.rows) : []),
    [showUnder, bars, series]
  )

  const selLabel = sel
    ? `${sel.strike.toFixed(sel.strike % 1 === 0 ? 0 : 1)} ${sel.right === 'P' ? 'put' : 'call'} · ${sel.expiration}`
    : null

  // Get data (docs/DATA_EXCHANGE.md). The Opt page is enrolled as the 'chain'
  // class: a GET grabs the selected CONTRACT when one is picked, else the
  // whole chain envelope for the first leg. Both serialize from the BACKEND
  // envelope rows, which carry all 13 fields -- the page's own Contract
  // interface declares 9, and building from it would drop four greeks that
  // are already on the wire.
  useEffect(() => {
    const off = window.grindstone.onDataAction(({ tool, spawn }) => {
      if (tool !== 'data:get') return
      const envelope = Object.values(byLeg).find((r) => r && r.available) || null
      const rows = envelope?.contracts ?? []
      // The CELL under the right-click wins over the page's selection: the
      // spawn names what the user pointed at, the selection only what they
      // last touched (DX-8).
      const occHit = occAt(spawn)
      const picked = (occHit ? rows.find((c) => c.occ_symbol === occHit) : undefined)
        ?? (sel ? rows.find((c) => c.occ_symbol === sel.occ) : undefined)
      const address = `opt.gs?s=${symbol}`
      const payload = picked
        ? buildContractPayload({
            contract: picked as unknown as Record<string, unknown>,
            page: 'opt', address, symbol,
          })
        : envelope
          ? buildChainPayload({
              envelope: envelope as unknown as Record<string, unknown>,
              page: 'opt', address, symbol,
            })
          : null
      if (!payload) {
        announce('nothing to grab yet -- the chain has not loaded')
        return
      }
      grab(payload)
        .then((e) => announce(`grabbed: ${e.label}`))
        .catch((err) => announce(`get data failed: ${err instanceof Error ? err.message : err}`))
    })
    return off
  }, [byLeg, sel, symbol])

  return (
    <div className="page opt-page" data-opt-symbol={symbol}
         data-wheel-context="chain" data-chart-symbols={symbol}>
      <div className="page-head">
        <h1>{symbol} Opt</h1>
        <button type="button" className="seg-btn" title="Back to chart"
          onClick={() => onNavigate?.({ name: 'symbol', symbol })}>
          ← chart
        </button>
        <span className="dim" data-opt-source={meta?.source ?? ''}>
          {meta?.source ?? '…'}{meta?.age !== undefined ? ` · ${ageWords(meta.age)}` : ''}
        </span>
        {/* The trade rides in the head strip — always visible, never tall. */}
        {book && !book.reason ? (
          <span className="opt-strip" data-opt-net={book.net === null ? '' : book.net.toFixed(4)}>
            <b className={book.net !== null && book.net > 0 ? 'gain' : 'loss'}>{fmtNet(book.net)}</b>
            <span>{roi === null ? '' : `${(roi * 100).toFixed(1)}% of risk`}</span>
            <span>max <b className="gain">+{fmtExtreme(book.maxProfit)}</b> / <b className="loss">−{fmtExtreme(book.maxLoss).replace('-', '')}</b></span>
            {book.breakevens.length > 0 ? <span>BE {book.breakevens.map((b) => b.toFixed(2)).join(' · ')}</span> : null}
            {sessions !== null ? <span>{sessions}s</span> : null}
            <span data-book-picked={picked}>{picked}/{visible.length} chosen</span>
          </span>
        ) : book?.reason ? (
          <span className="opt-strip dim">{book.reason}</span>
        ) : null}
      </div>

      {err ? <div className="chain-empty">{err}</div> : null}

      {legs === null ? (
        <div className="dim">Loading the chart's legs…</div>
      ) : visible.length === 0 ? (
        <div className="chain-idle dim">
          No legs on {symbol}'s daily chart yet.
          <div className="subtle">
            Legs are drawn on the chart — place a strategy there and this page analyses it.
          </div>
          <button type="button" className="seg-btn" onClick={() => onNavigate?.({ name: 'symbol', symbol })}>
            Open the {symbol} chart
          </button>
        </div>
      ) : (
        <div className="opt-shell">
          <div className="opt-main">
            <div className="opt-card" data-under-points={underSeries.length}
              data-series-points={series?.rows?.length ?? 0}
              data-unit={unit}
              // What was ASKED for, and what the backend actually ANSWERED
              // with. Two attributes because they can legitimately differ.
              data-match={match}
              data-match-got={series?.mode ?? ''}
              data-peak={Math.max(0, ...plotted.map((r) => r.value ?? 0)).toFixed(3)}>
              <div className="opt-card-head">
                <span className="cp-views">
                  <button type="button" className={`seg-btn${tab === 'future' ? ' on' : ''}`}
                    onClick={() => setTab('future')}>
                    Future
                  </button>
                  <button type="button" className={`seg-btn${tab === 'history' ? ' on' : ''}`}
                    onClick={() => setTab('history')}>
                    History
                  </button>
                </span>
                <h2>{selLabel ?? 'pick a contract on the right'}</h2>
                {tab === 'history' ? (
                  <>
                    <label className="opt-toggle">
                      match
                      <select
                        className="seg-select opt-match"
                        value={match}
                        onChange={(e) => setMatch(e.target.value as HistMatch)}
                        title={selDelta === null
                          ? 'no delta known for this contract — the series can only'
                            + ' match on strike'
                          : 'delta: the equivalent trade, strike walks · '
                            + 'strike: the actual strike, moneyness walks'}
                      >
                        {/* Named as the QUESTION, not the mechanism: the point
                            of the toggle is which comparison you get. Delta is
                            offered even with none known, but says so — silently
                            dropping to strike is how the two panels came to
                            disagree in the first place. */}
                        <option value="delta">
                          Δ equivalent trade{selDelta === null ? ' (no Δ — n/a)' : ''}
                        </option>
                        <option value="strike">$ actual strike</option>
                      </select>
                    </label>
                    <label className="opt-toggle">
                      unit
                      <select
                        className="seg-select opt-unit"
                        value={unit}
                        onChange={(e) => setUnit(e.target.value as HistUnit)}
                      >
                        <option value="pct">% of strike</option>
                        <option value="annual">% /yr (annualised)</option>
                        <option value="usd">$</option>
                      </select>
                    </label>
                    <label className="opt-toggle">
                      avg
                      <select
                        className="seg-select opt-avg"
                        value={avgWin}
                        onChange={(e) => setAvgWin(Number(e.target.value))}
                      >
                        <option value={0}>off</option>
                        <option value={10}>10</option>
                        <option value={20}>20</option>
                        <option value={50}>50</option>
                        <option value={100}>100</option>
                      </select>
                    </label>
                    <label className="opt-toggle">
                      <input type="checkbox" checked={showUnder}
                        onChange={(e) => setShowUnder(e.target.checked)} />
                      underlying
                    </label>
                  </>
                ) : null}
              </div>

              {!sel ? (
                <div className="dim subtle lc-empty opt-hint">
                  Click a cell in the heatmap (or a row in the chain) and this
                  space charts it — its term structure under Future, its
                  archived life under History.
                </div>
              ) : tab === 'future' ? (
                <>
                  {term === null ? (
                    <div className="dim subtle lc-empty">loading…</div>
                  ) : term.length === 0 ? (
                    <div className="dim subtle lc-empty">
                      no live chain to draw a term structure from
                    </div>
                  ) : (
                    <TermPanel
                      points={termPoints(term, today)}
                      markDte={dteBetween(today, sel.expiration)}
                      height={300}
                    />
                  )}
                  <div className="dim subtle opt-note">
                    the same strike at every listed expiration · orange = premium at
                    mid (right axis) · green = ANNUALISED rate on strike (left axis) —
                    the line that says whether the far month actually pays better ·
                    the arrow marks the expiration you picked
                  </div>
                </>
              ) : (
                <>
                  {series?.available && series.rows.length > 0 ? (
                    <div
                      // The LAST PLOTTED DATE, as data. The chart is canvas,
                      // so a test cannot read the axis — and "the response
                      // had today in it" is not the same claim as "the chart
                      // drew today": the spine projection ate six weeks off
                      // the tail while every API answer looked complete.
                      data-series-last={plotted.length ? plotted[plotted.length - 1].date : ''}
                      data-series-n={plotted.length}
                      data-series-live={liveShown === null ? '' : liveShown.toFixed(4)}
                    >
                    <HistoryPanel
                      rows={plotted}
                      pct={asPct}
                      under={underSeries}
                      underLabel={symbol}
                      avg={avgSeries}
                      avgLabel={`avg ${avgWin}`}
                      refPrice={liveShown}
                      height={420}
                    />
                    </div>
                  ) : (
                    <div className="dim subtle lc-empty">
                      {series === null ? 'loading…' : series.reason ?? 'no archived rows'}
                    </div>
                  )}
                  <div className="dim subtle opt-note">
                    {showUnder && barsErr ? (
                      <span className="loss">{symbol} price history unavailable: {barsErr} · </span>
                    ) : null}
                    {series?.available && sel ? seriesCaption(series, sel, symbol, today, hist, liveShown, unit, match) : null}
                  </div>
                </>
              )}
            </div>
          </div>

          <aside className="opt-side">
            <div className="opt-selector-head">
              <span className="dim">Selector</span>
              <span className="cp-views">
                <button type="button" className={`seg-btn${view === 'heat' ? ' on' : ''}`}
                  onClick={() => setView('heat')}>
                  Heatmap
                </button>
                <button type="button" className={`seg-btn${view === 'list' ? ' on' : ''}`}
                  onClick={() => setView('list')}>
                  Chain
                </button>
              </span>
            </div>

            {meta?.reason ? <div className="chain-empty">{meta.reason}</div> : null}

            {visible.map(({ leg, r }) => {
              const res = byLeg[leg.id]
              // The SAME rectangle the fetch uses — the tolerance box here once
              // printed 736-744 over a grid whose rows ran to 765.
              const w = r.window ?? legWindow(r.expiration, r.strike, leg.dteTol, leg.strikeTol)
              const rep = res?.contracts?.find((c) => c.occ_symbol === leg.pick)
              // READ-ONLY, deliberately. This page once wrote the pick into
              // the chart's document, and the chart page — which owns that
              // document in memory and rewrites it whole on every commit —
              // silently deleted it on the next drag. Two writers, one doc,
              // chart wins. So: choosing here CHARTS the contract; making it
              // the leg's persisted pick stays on the chart page, the single
              // writer.
              const choose = (c: GridContract | Contract) => {
                setSel({
                  occ: c.occ_symbol, strike: c.strike,
                  expiration: c.expiration, right: c.right,
                  delta: c.delta ?? null,
                })
              }
              return (
                <section className="opt-leg" key={leg.id}>
                  <header className="opt-leg-head">
                    <span className="em">
                      {r.side === 'short' ? 'SELL' : 'BUY'} {leg.right === 'P' ? 'PUT' : 'CALL'}{' '}
                      {r.strike.toFixed(1)}
                    </span>
                    <span className="dim">
                      {r.expiration} ±{Math.round(leg.dteTol)}d ±${leg.strikeTol.toFixed(0)}
                      {w ? ` · ${w.strikeLo.toFixed(0)}–${w.strikeHi.toFixed(0)}` : ''}
                      {r.trendHosted ? ' · rides a trend' : ''}
                    </span>
                    <span className="cp-count" data-opt-matches={res ? res.total : ''}>
                      {res === null || res === undefined ? '…'
                        : res.available ? `${res.total}` : '—'}
                    </span>
                  </header>
                  {rep ? (
                    <div className="opt-chosen">
                      chosen {rep.strike.toFixed(1)} {rep.expiration} ·{' '}
                      {rep.bid ? rep.bid.toFixed(2) : 'no bid'} × {rep.ask?.toFixed(2) ?? '—'} ·
                      Δ{typeof rep.delta === 'number' ? rep.delta.toFixed(2) : '—'} · IV {pct(rep.iv)}
                    </div>
                  ) : null}
                  {res && res.available && res.contracts.length > 0 ? (
                    view === 'heat' ? (
                      <OptHeatmap
                        contracts={res.contracts}
                        side={r.side}
                        today={today}
                        anchor={{ strike: r.strike, expiration: r.expiration }}
                        selected={sel?.occ ?? leg.pick ?? null}
                        onPick={choose}
                      />
                    ) : (
                      <table className="cp-table opt-chain">
                        <thead>
                          <tr><th>strike</th><th>exp</th><th>bid</th><th>ask</th><th>Δ</th><th>iv</th></tr>
                        </thead>
                        <tbody>
                          {res.contracts.map((c) => (
                            <tr key={c.occ_symbol}
                              className={(sel?.occ ?? leg.pick) === c.occ_symbol ? 'on' : undefined}
                              onClick={() => choose(c)}>
                              <td className="em">{c.strike.toFixed(1)}</td>
                              <td className="dim">{c.expiration.slice(5)}</td>
                              <td>{c.bid ? c.bid.toFixed(2) : 'no bid'}</td>
                              <td>{c.ask?.toFixed(2) ?? '—'}</td>
                              <td>{typeof c.delta === 'number' ? c.delta.toFixed(2) : '—'}</td>
                              <td className="dim">{pct(c.iv)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    )
                  ) : res && !res.available ? (
                    <div className="dim subtle">{res.reason ?? 'no contracts'}</div>
                  ) : res && res.contracts.length === 0 ? (
                    <div className="dim subtle">{res.reason ?? 'no contracts in this window'}</div>
                  ) : (
                    <div className="dim subtle">loading…</div>
                  )}
                </section>
              )
            })}
          </aside>
        </div>
      )}
    </div>
  )
}

/** Term-structure points, carrying the ANNUALISED rate beside the premium —
 *  the same credit/strike-over-sessions arithmetic as the heatmap cells, so
 *  the curve and the grid can never disagree about which DTE pays best. */
function termPoints(rows: Contract[], today: string): TermPoint[] {
  return rows
    .map((c) => {
      const dte = dteBetween(today, c.expiration)
      const tdte = tradingDaysTo(today, c.expiration)
      if (dte === null || tdte === null) return null
      const mid = midOf(c.bid, c.ask)
      return {
        dte,
        mid,
        bid: typeof c.bid === 'number' && c.bid > 0 ? c.bid : null,
        ask: typeof c.ask === 'number' ? c.ask : null,
        rate: mid === null ? null : annualise(mid / (capitalFor(c.strike) ?? c.strike), tdte),
      }
    })
    .filter((p): p is TermPoint => p !== null)
}

/** A 404 from our own sidecar means one thing: the running backend predates
 *  the endpoint. Naming that beats a bare "Not Found", because the sidecar
 *  never hot-reloads and this WILL happen again after backend work. */
function staleOr(e: unknown): string {
  if (e instanceof ApiError && e.status === 404) {
    return 'the running backend predates this feature — close and relaunch the app'
  }
  return String(e)
}

/** In words, what the series chart is: the TRADE's shape priced back through
 *  time, with the matching honesty spelled out when the exact DTE was not
 *  listed on some days. */
function seriesCaption(
  series: SeriesResponse, sel: Sel, symbol: string, today: string,
  hist: { median: number | null; p25: number | null; p75: number | null; pct: number | null; n: number },
  liveMid: number | null,
  unit: HistUnit,
  /** What the user ASKED to hold fixed — which is not always what the backend
   *  could answer with. When they diverge the caption must say so. */
  match: HistMatch
): string {
  // The caption quotes the same numbers the axis does, in the same unit — and
  // NAMES it, because "0.9937%" and "2.2%" are the same trade and the only
  // thing telling them apart is the suffix.
  const u = (v: number): string => {
    const d = v.toFixed(Math.abs(v) >= 10 ? 2 : Math.abs(v) >= 1 ? 3 : 4)
    if (unit === 'usd') return v.toFixed(2)
    return unit === 'annual' ? `${d}%/yr` : `${d}% of strike`
  }
  const dte = dteBetween(today, sel.expiration) ?? 0
  const used = series.rows.map((r) => r.used_dte)
  const lo = Math.min(...used)
  const hi = Math.max(...used)
  const range = lo === hi ? `${lo} DTE exactly` : `${lo}–${hi} DTE (nearest listed each day)`
  const kind = sel.right === 'P' ? 'put' : 'call'
  // The verdict first, in words: a percentile is the answer to "is this a
  // good price", and it is worth more than any amount of axis-reading.
  const verdict = (hist.pct === null || liveMid === null || hist.median === null)
    ? ''
    : `TODAY ${u(liveMid)} sits at the ${hist.pct}th percentile of the last ` +
      `year (normal ${u(hist.median)}, typical range ` +
      `${u(hist.p25 ?? 0)}–${u(hist.p75 ?? 0)}) · `
  // Say where the annualised number comes from, once, ON the chart. This unit
  // exists to be compared against a heatmap cell, and a reader who cannot see
  // why 0.99% became 2.2% will distrust one panel or the other.
  const unitNote = unit === 'annual'
    ? 'premium ÷ strike, scaled to a year over SESSIONS — the heatmap cell’s ' +
      'own unit, so the two panels can be read against each other · '
    : ''
  // ASKED FOR DELTA, GOT STRIKE. The control offers the delta match whether or
  // not a delta is known, because hiding the option would leave the reader
  // wondering where it went — but then this is the only place that can admit
  // the answer is a different question from the one asked.
  const fellBack = match === 'delta' && series.mode !== 'delta'
    ? 'asked for the Δ-matched trade, but this contract carries no delta in ' +
      'the feed, so this is the STRIKE match instead · '
    : ''
  if (series.mode === 'delta' && series.target?.delta != null) {
    // The strike WALKS with the market in this mode — that is the whole point,
    // and the caption owns it so nobody reads the line as one contract.
    const strikes = series.rows.map((r) => r.used_strike)
    return (
      verdict + unitNote +
      `what THE Δ${series.target.delta.toFixed(2)} ${kind} at ~${dte} DTE has cost, ` +
      `day by day over the last year — same risk shape, whatever the strike ` +
      `(it walked ${Math.min(...strikes).toFixed(0)}–${Math.max(...strikes).toFixed(0)} ` +
      `as ${symbol} moved) · matched at ${range} · each node is one archived ` +
      `contract · ${symbol} BLUE on the left axis`
    )
  }
  // THE ACTUAL STRIKE. The "(no delta known, so the strike stands in)" note
  // that used to live here was true when this was only a fallback; it is a
  // chosen question now, and `fellBack` says the other thing when it applies.
  const strikes = series.rows.map((r) => r.used_strike)
  const drift = Math.max(...strikes) - Math.min(...strikes)
  return (
    verdict + unitNote + fellBack +
    `what a ~${dte}-DTE ${symbol} ${sel.strike.toFixed(0)} ${kind} has cost, ` +
    `day by day over the last year — THE STRIKE held fixed` +
    (drift > 0.01 ? ` (±$1, so it ranged ${Math.min(...strikes).toFixed(0)}–` +
      `${Math.max(...strikes).toFixed(0)})` : '') +
    `, so its moneyness walks as ${symbol} moves · matched at ${range} · ` +
    `each node is one archived contract · ${symbol} BLUE on the left axis`
  )
}

/** The underlying's closes over the series' span.
 *
 *  Clipped to the option's window when the two overlap, and UNCLIPPED when
 *  they do not. An empty overlay is the one outcome worth engineering
 *  against: it removes the left axis entirely, so the failure looks like a
 *  feature that was never built rather than data that did not line up. */
function underPoints(
  bars: { ts: string; close: number }[],
  rows: { date: string }[]
): { date: string; close: number }[] {
  const all = bars
    .map((b) => ({ date: b.ts.slice(0, 10), close: b.close }))
    .filter((b) => Number.isFinite(b.close))
    .sort((a, b) => a.date.localeCompare(b.date))
  if (all.length === 0 || rows.length === 0) return all
  const from = rows[0].date
  const to = rows[rows.length - 1].date
  const clipped = all.filter((b) => b.date >= from && b.date <= to)
  return clipped.length > 0 ? clipped : all
}
