/** The options-chain side panel — a READOUT of what the chart's leg zones
 *  capture, never the primary instrument. The chart stays the centerpiece.
 *
 *  REFRESH CONTRACT: fetches fire when the LEGS CHANGE, and legs only change
 *  on commit — the engine's drag path renders without emitting, so dragging a
 *  zone never fetches; releasing it does. One request per leg window (the
 *  endpoint takes one window), debounced, newest-wins per leg. Nothing here
 *  polls; the server's 45s TTL absorbs repeat commits.
 *
 *  HONESTY RULES, each load-bearing:
 *  - The source string renders VERBATIM from the endpoint ('alpaca
 *    (indicative)' / 'none') — one vocabulary with provider_status.
 *  - No creds / provider failure is a rendered .chain-empty state with the
 *    endpoint's own reason. It is the FIRST state every fresh install sees.
 *  - A missing greek prints an em-dash, never 0. A zero bid prints 'no bid',
 *    never a fabricated mid. Truncation says 'showing N of M', never trims
 *    silently.
 */
import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import type { ResolvedLeg } from './ChartDraw'
import { LEG_PALETTE } from './ChartDraw'
import { OptHeatmap } from './OptHeatmap'
import { midOf, tradingDaysTo, type GridContract } from '../optgrid'
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
  source: string
  reason?: string
}

const fmt = (v: number | null | undefined, dp = 2): string =>
  typeof v === 'number' && Number.isFinite(v) ? v.toFixed(dp) : '—'

function dteOf(expiration: string): number {
  const ms = Date.parse(expiration + 'T00:00:00Z') - Date.parse(new Date().toISOString().slice(0, 10) + 'T00:00:00Z')
  return Math.round(ms / 86400_000)
}

function LegRows({
  leg,
  res,
  view,
  today,
  onToggleHidden,
  onPick,
}: {
  leg: ResolvedLeg
  res: ChainResponse | null
  view: 'list' | 'heat'
  today: string
  onToggleHidden?: (id: string, hidden: boolean) => void
  onPick?: (legId: string, c: GridContract) => void
}) {
  const color = LEG_PALETTE[leg.slot % LEG_PALETTE.length]
  const hidden = !!leg.hidden
  const head = (
    <div className="cp-leg-head">
      {/* The eye stays in the STRIP even while the leg is off the chart. A
          hidden leg has no region and no handles, so this row is the only
          place it can be reached — and 'Clear legs' would still sweep it. */}
      <button
        type="button"
        className={`cp-eye${hidden ? ' off' : ''}`}
        title={hidden ? 'Show leg' : 'Hide leg'}
        aria-pressed={hidden}
        data-leg-hidden={hidden ? '1' : '0'}
        onClick={() => onToggleHidden?.(leg.id, !hidden)}
      >
        {hidden ? '◌' : '●'}
      </button>
      <span className="cp-swatch" style={{ background: hidden ? 'transparent' : color, borderColor: color }} />
      <span className="em">
        {leg.side === 'short' ? 'SELL' : 'BUY'} {leg.right === 'P' ? 'PUT' : 'CALL'}{' '}
        {fmt(leg.resolved.strike, 1)}
      </span>
      <span className="dim">
        {leg.resolved.expiration} ({dteOf(leg.resolved.expiration)}d) ±{Math.round(leg.dteTol)}d ±$
        {fmt(leg.strikeTol, 0)}
        {leg.resolved.hosted ? ` · rides ${leg.resolved.hosted}` : ''}
      </span>
      <span className="cp-count" data-leg-matches={res ? res.total : ''}>
        {res === null ? '…' : res.available ? `${res.total} match${res.total === 1 ? '' : 'es'}` : '—'}
      </span>
    </div>
  )
  if (!res || !res.available || res.contracts.length === 0) {
    return (
      <div className={`cp-leg${hidden ? ' hidden' : ''}`}>
        {head}
        {res && res.available && res.contracts.length === 0 ? (
          <div className="dim subtle">{res.reason ?? 'no contracts in this window'}</div>
        ) : null}
      </div>
    )
  }
  if (view === 'heat') {
    return (
      <div className={`cp-leg${hidden ? ' hidden' : ''}`}>
        {head}
        {res.truncated ? (
          <div className="dim subtle">showing {res.contracts.length} of {res.total} — narrow the window</div>
        ) : null}
        <OptHeatmap
          contracts={res.contracts}
          side={leg.side}
          today={today}
          anchor={{ strike: leg.resolved.strike, expiration: leg.resolved.expiration }}
          selected={leg.pick ?? null}
          onPick={(c) => onPick?.(leg.id, c)}
        />
      </div>
    )
  }

  return (
    <div className={`cp-leg${hidden ? ' hidden' : ''}`}>
      {head}
      {res.truncated ? (
        <div className="dim subtle">showing {res.contracts.length} of {res.total} — narrow the window</div>
      ) : null}
      <table className="cp-table opt-chain">
        <thead>
          <tr><th>strike</th><th>exp</th><th>bid</th><th>ask</th><th>Δ</th><th>iv</th></tr>
        </thead>
        <tbody>
          {/* EVERY row the endpoint returned. This was capped at 14, which
              silently threw away the other 462 of a 476-match window while the
              header truthfully said "476 matches" — the count and the list
              disagreeing about the same answer. The panel already scrolls
              (.side-panel is overflow-y:auto against a bounded height), so the
              cap was buying nothing that scrolling did not already give. The
              honest limit is the endpoint's own MAX_ROWS, which the
              "showing N of M" line above already declares. */}
          {/* Rows are pick targets, exactly like heatmap cells — the list is
              the same selector wearing a different projection, and a row you
              can read but not choose made the two views feel like different
              features. */}
          {res.contracts.map((c) => (
            <tr
              key={c.occ_symbol}
              className={leg.pick === c.occ_symbol ? 'on' : undefined}
              onClick={() => onPick?.(leg.id, c as GridContract)}
            >
              <td className="em">{fmt(c.strike, 1)}</td>
              <td className="dim">{c.expiration.slice(5)}</td>
              {/* A zero bid is not a price — a mid built on it would be a lie. */}
              <td>{c.bid ? fmt(c.bid) : 'no bid'}</td>
              <td>{fmt(c.ask)}</td>
              {/* Absent greeks print an em-dash, never a fabricated 0. */}
              <td>{fmt(c.delta)}</td>
              <td className="dim">{c.iv ? (c.iv * 100).toFixed(0) + '%' : '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function ChainPanel({
  symbol,
  legs,
  onToggleHidden,
  onPick,
}: {
  symbol: string
  legs: ResolvedLeg[]
  onToggleHidden?: (id: string, hidden: boolean) => void
  onPick?: (legId: string, c: GridContract) => void
}) {
  const [byLeg, setByLeg] = useState<Record<string, ChainResponse | null>>({})
  const [meta, setMeta] = useState<{ source: string; reason?: string } | null>(null)
  // List or heatmap. Kept OUT of fetchKey deliberately: both views render the
  // very same rows, so toggling must never cost a request.
  const [view, setView] = useState<'list' | 'heat'>('list')
  // Read once per render rather than per cell, so every DTE in a frame is
  // measured from the same day — a grid built across a midnight rollover would
  // otherwise carry two different todays.
  const today = new Date().toISOString().slice(0, 10)
  const seq = useRef(0)
  // The fetch key is the WINDOWS, not the legs array identity: a re-render
  // that resolved to the same windows (selection change, hover) must not
  // refetch, and a drag only lands here after its commit changed a window.
  const fetchKey = JSON.stringify(
    legs.map((l) => [l.id, l.right, l.window]).sort((a, b) => String(a[0]).localeCompare(String(b[0])))
  )

  useEffect(() => {
    if (legs.length === 0) {
      setByLeg({})
      return
    }
    const mine = ++seq.current
    const t = setTimeout(async () => {
      for (const leg of legs) {
        if (!leg.window) continue
        try {
          const r = await api<ChainResponse>(
            'GET',
            `/api/symbols/${encodeURIComponent(symbol)}/options` +
              `?exp_from=${leg.window.expFrom}&exp_to=${leg.window.expTo}` +
              `&strike_from=${leg.window.strikeLo.toFixed(4)}&strike_to=${leg.window.strikeHi.toFixed(4)}` +
              `&right=${leg.right}`
          )
          if (mine !== seq.current) return // superseded — drop, never paint stale
          setByLeg((cur) => ({ ...cur, [leg.id]: r }))
          setMeta({ source: r.source, reason: r.available ? undefined : r.reason })
        } catch (e) {
          if (mine !== seq.current) return
          setByLeg((cur) => ({
            ...cur,
            [leg.id]: {
              underlying: symbol, available: false, contracts: [], total: 0,
              truncated: false, source: 'none', reason: String(e),
            },
          }))
        }
      }
    }, 400) // the save-debounce idiom: a burst of commits is one fetch round
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fetchKey, symbol])

  if (legs.length === 0) {
    return (
      <div className="chain-idle dim">
        Place a leg or pick a strategy to filter the chain.
        <div className="subtle">
          Legs are zones on the chart — drag them, bind them to your lines, and
          the contracts inside each window list here.
        </div>
      </div>
    )
  }

  // THE STRUCTURE, priced at mid. A leg is a filter window that can match many
  // contracts, so the representative used here is the one NEAREST the leg's own
  // anchor — the endpoint already sorts nearest-first, so that is row 0. Once a
  // contract is explicitly picked (Stage 4) this reads the pick instead.
  const shown = legs.filter((l) => !l.hidden)
  const structure: PayoffLeg[] = shown.map((l) => {
    const rows = byLeg[l.id]?.contracts ?? []
    // THE PICK WINS. Falling back to row 0 (the endpoint sorts nearest-first)
    // only while nothing has been chosen — and a pick that is no longer in the
    // window simply is not found, so the leg degrades to that same fallback
    // rather than pricing a contract the filter no longer covers.
    const rep = (l.pick && rows.find((c) => c.occ_symbol === l.pick)) || rows[0]
    return {
      side: l.side,
      right: l.right,
      strike: rep ? rep.strike : l.resolved.strike,
      premium: rep ? midOf(rep.bid, rep.ask) : null,
    }
  })
  const book = structure.length > 0 ? analyse(structure) : null
  // How much of the book is your choice versus this panel's guess. Stated,
  // because a number built on a default should never read as a decision.
  const picked = shown.filter(
    (l) => l.pick && (byLeg[l.id]?.contracts ?? []).some((c) => c.occ_symbol === l.pick)
  ).length
  // The NEAREST expiration decides the horizon: that is when the structure
  // first resolves, and annualising a condor over its back month would claim a
  // rate it stops earning at the front one.
  const sessions = shown.reduce<number | null>((min, l) => {
    const t = tradingDaysTo(today, l.resolved.expiration)
    return t === null ? min : min === null ? t : Math.min(min, t)
  }, null)
  // The plain ratio leads: it is what the trade pays against what it risks,
  // with no assumption about repeating it. The annualised figure is secondary
  // and linear — see optgrid.annualise for why compounding was removed.
  const roi = book ? returnOnRisk(book) : null

  const unavailable = meta?.reason
  return (
    <div className="chain-panel-body">
      <div className="cp-source dim" data-chain-source={meta?.source ?? ''}>
        {meta?.source ?? '…'}
        {/* Both views render the SAME rows the panel already holds, so this
            toggle costs no request — which is why it is a view switch and not
            a second endpoint. */}
        <span className="cp-views" data-chain-view={view}>
          <button
            type="button"
            className={`seg-btn${view === 'list' ? ' on' : ''}`}
            title="Chain list"
            onClick={() => setView('list')}
          >
            List
          </button>
          <button
            type="button"
            className={`seg-btn${view === 'heat' ? ' on' : ''}`}
            title="Heatmap"
            onClick={() => setView('heat')}
          >
            Heatmap
          </button>
        </span>
      </div>
      {unavailable ? (
        // The FIRST state a fresh install sees: the endpoint's reason, verbatim,
        // findable by the e2e — never a spinner-forever, never fake rows.
        <div className="chain-empty">
          {unavailable}
          <div className="subtle dim">
            Zones still work — they are geometry. Contracts appear when a data
            key is connected.
          </div>
        </div>
      ) : null}
      {book ? (
        <div className="cp-book" data-book-net={book.net === null ? '' : book.net.toFixed(4)}>
          {book.reason ? (
            <span className="dim">{book.reason}</span>
          ) : (
            <>
              <div className="cp-book-top">
                <span className={book.net !== null && book.net > 0 ? 'gain' : 'loss'}>
                  {fmtNet(book.net)}
                </span>
                <span className="cp-book-roi" data-book-roi={roi ?? ''}>
                  {roi === null ? '—' : `${(roi * 100).toFixed(1)}%`}
                  <i>of risk</i>
                </span>
              </div>
              <div className="cp-book-row">
                <span>
                  max <b className="gain">+{fmtExtreme(book.maxProfit)}</b>
                  {' / '}
                  <b className="loss">−{fmtExtreme(book.maxLoss).replace('-', '')}</b>
                </span>
                {book.breakevens.length > 0 ? (
                  <span className="dim">
                    BE {book.breakevens.map((b) => b.toFixed(2)).join(' · ')}
                  </span>
                ) : null}
                {sessions !== null ? (
                  <span className="dim">{sessions} session{sessions === 1 ? '' : 's'}</span>
                ) : null}
                <span className="cp-picked dim" data-book-picked={picked}>
                  {picked === shown.length
                    ? `${picked}/${shown.length} chosen`
                    : `${picked}/${shown.length} chosen · rest use nearest match`}
                </span>
              </div>
            </>
          )}
        </div>
      ) : null}

      {legs.map((l) => (
        <LegRows
          key={l.id}
          leg={l}
          res={byLeg[l.id] ?? null}
          view={view}
          today={today}
          onToggleHidden={onToggleHidden}
          onPick={onPick}
        />
      ))}
    </div>
  )
}
