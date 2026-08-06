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
import { api } from '../api'
import {
  legWindow, resolveLegDoc, type Drawing, type OptionLeg,
} from '../components/ChartDraw'
import { OptHeatmap } from '../components/OptHeatmap'
import {
  FanPanel, HistoryPanel, TermPanel,
  type FanData, type TermPoint,
} from '../components/OptCharts'
import { annualise, capitalFor, midOf, tradingDaysTo, dteBetween, type GridContract } from '../optgrid'
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

interface FanResponse {
  available: boolean
  source: string
  reason?: string
  band_reason?: string
  path: { dte: number; spread: number | null; date: string }[]
  band: { dte: number; p10: number; p25: number; p50: number; p75: number; p90: number; n: number }[]
  bucket?: { median_delta: number; lo: number; hi: number }
}

type Sel = { occ: string; strike: number; expiration: string; right: 'P' | 'C'; delta?: number | null }

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
  onNavigate,
}: {
  symbol: string
  onNavigate?: (r: { name: 'symbol'; symbol: string }) => void
}) {
  const [legs, setLegs] = useState<OptionLeg[] | null>(null)
  const [draws, setDraws] = useState<Drawing[]>([])
  const [byLeg, setByLeg] = useState<Record<string, ChainResponse | null>>({})
  const [meta, setMeta] = useState<{ source: string; reason?: string; age?: number } | null>(null)
  const [err, setErr] = useState<string | null>(null)

  // WHAT IS CHARTED, and WHICH question is being asked of it.
  const [sel, setSel] = useState<Sel | null>(null)
  const [tab, setTab] = useState<'future' | 'history'>('future')
  const [view, setView] = useState<'heat' | 'list'>('heat')
  const [showUnder, setShowUnder] = useState(true)

  const [tick, setTick] = useState(0)
  const [term, setTerm] = useState<Contract[] | null>(null)
  const [series, setSeries] = useState<SeriesResponse | null>(null)
  const [fan, setFan] = useState<FanResponse | null>(null)
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

  // A persisted pick selects itself: the trade you built is what the page
  // opens on, not a blank chart waiting for a click you already made.
  useEffect(() => {
    if (sel !== null) return
    const withPick = visible.find(({ leg }) => leg.pick)
    if (!withPick) return
    const parsed = parseOcc(withPick.leg.pick as string)
    if (parsed) setSel(parsed)
  }, [visible, sel])

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
    const end = new Date(Date.parse(today + 'T00:00:00Z') + 365 * 86400_000)
      .toISOString().slice(0, 10)
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
  }, [symbol, sel?.strike, sel?.right, today]) // eslint-disable-line react-hooks/exhaustive-deps

  // ---- HISTORY: the TRADE'S history, not one contract's -------------------
  // The series holds the trade's SHAPE fixed — this strike, ~this DTE — and
  // walks it back a year through the archive. One contract's own life only
  // reaches back six weeks and answered a question nobody asked; "what has
  // the ~21-DTE 765 put cost, against where SPY was" is the comparison Kade
  // actually described. The fan keeps the picked contract's spread life.
  useEffect(() => {
    if (!sel) {
      setSeries(null)
      setFan(null)
      return
    }
    let alive = true
    const dte = Math.max(1, dteBetween(today, sel.expiration) ?? 1)
    // DELTA-FIRST, per Kade: a fixed strike drifts through moneyness as the
    // underlying moves, so its year of history mostly re-plots the underlying.
    // Constant |delta| holds the trade's risk shape fixed. The delta comes
    // from the clicked cell, or from the live rows once they land; strike is
    // the honest fallback when the feed omitted the greek.
    const liveDelta = sel.delta ??
      Object.values(byLeg)
        .flatMap((r) => r?.contracts ?? [])
        .find((c) => c.occ_symbol === sel.occ)?.delta ?? null
    const shape = typeof liveDelta === 'number' && Math.abs(liveDelta) > 0.005
      ? `delta=${liveDelta.toFixed(4)}&delta_tol=0.08`
      : `strike=${sel.strike.toFixed(4)}&strike_tol=1`
    api<SeriesResponse>(
      'GET',
      `/api/symbols/${encodeURIComponent(symbol)}/options/serieshistory` +
        `?right=${sel.right}&dte=${dte}&dte_tol=3&${shape}`
    )
      .then((r) => alive && setSeries(r))
      .catch((e) => alive && setSeries({ available: false, rows: [], source: 'none', reason: String(e) }))
    const qs = `?expiration=${sel.expiration}&strike=${sel.strike.toFixed(4)}&right=${sel.right}`
    api<FanResponse>('GET', `/api/symbols/${encodeURIComponent(symbol)}/options/fanchart${qs}`)
      .then((r) => alive && setFan(r))
      .catch((e) => alive && setFan({
        available: false, path: [], band: [], source: 'none', reason: String(e),
      }))
    return () => {
      alive = false
    }
  }, [symbol, sel?.occ, sel?.strike, sel?.expiration, sel?.right, sel?.delta, // eslint-disable-line react-hooks/exhaustive-deps
      today, Object.keys(byLeg).length])

  // The underlying, for the history view's indicator only: past closes share
  // a timeline with the contract's past prices. (On the term structure the x
  // axis is days-to-expiry — a different axis, so it has no place there.)
  useEffect(() => {
    if (tab !== 'history' || bars.length > 0) return
    let alive = true
    api<{ bars: { ts: string; close: number }[] }>(
      'GET', `/api/symbols/${encodeURIComponent(symbol)}/bars?timeframe=1Day&limit=400`)
      .then((r) => alive && setBars(r.bars ?? []))
      .catch(() => undefined)
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

  const selLabel = sel
    ? `${sel.strike.toFixed(sel.strike % 1 === 0 ? 0 : 1)} ${sel.right === 'P' ? 'put' : 'call'} · ${sel.expiration}`
    : null

  return (
    <div className="page opt-page" data-opt-symbol={symbol}>
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
            <div className="opt-card">
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
                  <label className="opt-toggle">
                    <input type="checkbox" checked={showUnder}
                      onChange={(e) => setShowUnder(e.target.checked)} />
                    underlying
                  </label>
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
                    <HistoryPanel
                      rows={series.rows}
                      under={showUnder ? underPoints(bars, series.rows) : []}
                      underLabel={symbol}
                      refPrice={liveMid}
                      height={240}
                    />
                  ) : (
                    <div className="dim subtle lc-empty">
                      {series === null ? 'loading…' : series.reason ?? 'no archived rows'}
                    </div>
                  )}
                  <div className="dim subtle opt-note">
                    {series?.available && sel ? seriesCaption(series, sel, symbol, today) : null}
                  </div>

                  {fan?.available && (fan.band.length > 0 || fan.path.length > 0) ? (
                    <FanPanel data={fanData(fan)} height={230} />
                  ) : (
                    <div className="dim subtle lc-empty">
                      {fan === null ? 'loading…' : fan.reason ?? 'no archived spread path'}
                    </div>
                  )}
                  <div className="dim subtle opt-note">
                    {fan?.available && sel ? (
                      <>
                        bid-ask spread across this contract's life, expiry at the right
                        edge · gray bands = the 10–90 and 25–75 percentiles of every
                        archived {symbol} {sel.right === 'P' ? 'put' : 'call'} near
                        Δ{fan.bucket ? fan.bucket.median_delta.toFixed(2) : '—'} ·
                        {fan.band.length === 0 ? ` ${fan.band_reason ?? 'no band'} · ` : ' '}
                        inside the band is typical, outside is unusually tight or wide
                      </>
                    ) : null}
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
              const w = legWindow(r.expiration, r.strike, leg.dteTol, leg.strikeTol)
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

/** In words, what the series chart is: the TRADE's shape priced back through
 *  time, with the matching honesty spelled out when the exact DTE was not
 *  listed on some days. */
function seriesCaption(
  series: SeriesResponse, sel: Sel, symbol: string, today: string
): string {
  const dte = dteBetween(today, sel.expiration) ?? 0
  const used = series.rows.map((r) => r.used_dte)
  const lo = Math.min(...used)
  const hi = Math.max(...used)
  const range = lo === hi ? `${lo} DTE exactly` : `${lo}–${hi} DTE (nearest listed each day)`
  const kind = sel.right === 'P' ? 'put' : 'call'
  if (series.mode === 'delta' && series.target?.delta != null) {
    // The strike WALKS with the market in this mode — that is the whole point,
    // and the caption owns it so nobody reads the line as one contract.
    const strikes = series.rows.map((r) => r.used_strike)
    return (
      `what THE Δ${series.target.delta.toFixed(2)} ${kind} at ~${dte} DTE has cost, ` +
      `day by day over the last year — same risk shape, whatever the strike ` +
      `(it walked ${Math.min(...strikes).toFixed(0)}–${Math.max(...strikes).toFixed(0)} ` +
      `as ${symbol} moved) · matched at ${range} · mid solid, bid/ask dashed · ` +
      `${symbol} gray on the left axis`
    )
  }
  return (
    `what a ~${dte}-DTE ${symbol} ${sel.strike.toFixed(0)} ${kind} has cost, ` +
    `day by day over the last year · matched at ${range}, strike ±$1 (no delta ` +
    `known for this contract, so the strike stands in) · mid solid, bid/ask ` +
    `dashed · ${symbol} gray on the left axis`
  )
}

/** The underlying's closes clipped to the series' span. */
function underPoints(
  bars: { ts: string; close: number }[],
  rows: { date: string }[]
): { date: string; close: number }[] {
  if (rows.length === 0) return []
  const from = rows[0].date
  const to = rows[rows.length - 1].date
  return bars
    .map((b) => ({ date: b.ts.slice(0, 10), close: b.close }))
    .filter((b) => b.date >= from && b.date <= to)
}

function fanData(fan: FanResponse): FanData {
  const maxPath = fan.path.length ? Math.max(...fan.path.map((p) => p.dte)) : 0
  const cap = Math.max(90, Math.min(180, maxPath + 15))
  return {
    cap,
    band: fan.band.filter((b) => b.dte <= cap),
    path: fan.path,
  }
}
