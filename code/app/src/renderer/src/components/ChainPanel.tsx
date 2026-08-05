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

function LegRows({ leg, res }: { leg: ResolvedLeg; res: ChainResponse | null }) {
  const color = LEG_PALETTE[leg.slot % LEG_PALETTE.length]
  const head = (
    <div className="cp-leg-head">
      <span className="cp-swatch" style={{ background: color }} />
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
      <div className="cp-leg">
        {head}
        {res && res.available && res.contracts.length === 0 ? (
          <div className="dim subtle">{res.reason ?? 'no contracts in this window'}</div>
        ) : null}
      </div>
    )
  }
  return (
    <div className="cp-leg">
      {head}
      {res.truncated ? (
        <div className="dim subtle">showing {res.contracts.length} of {res.total} — narrow the window</div>
      ) : null}
      <table className="cp-table">
        <thead>
          <tr><th>strike</th><th>exp</th><th>bid</th><th>ask</th><th>Δ</th><th>iv</th></tr>
        </thead>
        <tbody>
          {res.contracts.slice(0, 14).map((c) => (
            <tr key={c.occ_symbol}>
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

export function ChainPanel({ symbol, legs }: { symbol: string; legs: ResolvedLeg[] }) {
  const [byLeg, setByLeg] = useState<Record<string, ChainResponse | null>>({})
  const [meta, setMeta] = useState<{ source: string; reason?: string } | null>(null)
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

  const unavailable = meta?.reason
  return (
    <div className="chain-panel-body">
      <div className="cp-source dim" data-chain-source={meta?.source ?? ''}>
        {meta?.source ?? '…'}
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
      {legs.map((l) => (
        <LegRows key={l.id} leg={l} res={byLeg[l.id] ?? null} />
      ))}
    </div>
  )
}
