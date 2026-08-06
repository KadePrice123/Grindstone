/** The heatmap view of a leg's filtered contracts.
 *
 *  Rows are strikes, highest at the top, the way a chain reads. Columns are
 *  expirations, nearest first, labelled by DTE because DTE is the thing being
 *  compared. Every number in here comes from optgrid.ts, which is pure and
 *  gate-probed; this file is the surface only.
 *
 *  It is a READOUT AND A DOOR: hovering explains a cell, clicking picks that
 *  contract. Nothing here places an order — the whole options surface is a
 *  filter, and that scope rule is older than this component.
 */
import { useCallback, useMemo, useRef } from 'react'
import {
  buildGrid, cellKey, fmtAnnual, rampPosition, sideInk,
  type GridCell, type GridContract, type LegSide,
} from '../optgrid'

export interface HeatmapProps {
  contracts: GridContract[]
  side: LegSide
  today: string
  /** The leg's own anchor, ringed so you can see what you are comparing to. */
  anchor?: { strike: number; expiration: string } | null
  selected?: string | null
  onPick?: (c: GridContract) => void
}

const money = (v: number | null): string => (v === null ? '—' : v.toFixed(2))

export function OptHeatmap({
  contracts, side, today, anchor, selected, onPick,
}: HeatmapProps) {
  const grid = useMemo(
    () => buildGrid(contracts, { today, side }),
    [contracts, today, side]
  )

  const scroller = useRef<HTMLDivElement | null>(null)
  /** Sideways scrolling, by every route a mouse actually offers.
   *
   *  A tilt wheel reports `deltaX`, which Chromium normally applies on its
   *  own — but only once something has claimed the horizontal axis, and a
   *  vertically-scrolling ancestor often claims the gesture first, so the grid
   *  sits still while the panel behind it moves. Claiming deltaX here settles
   *  that in the grid's favour whenever the grid can actually use it.
   *
   *  Shift+wheel is handled too, and not merely for convenience: plenty of
   *  mice emit a tilt as extra BUTTONS rather than as wheel deltas, and those
   *  never reach the page at all. Shift is the route that always exists.
   *
   *  preventDefault only when the scroll was consumed — at either end of the
   *  range the event is left alone so the panel keeps scrolling normally
   *  instead of the gesture dying against a wall. */
  const onWheel = useCallback((e: React.WheelEvent<HTMLDivElement>) => {
    const el = scroller.current
    if (!el) return
    const max = el.scrollWidth - el.clientWidth
    if (max <= 0) return // nothing to scroll sideways; let it bubble
    const dx = e.deltaX !== 0 ? e.deltaX : e.shiftKey ? e.deltaY : 0
    if (dx === 0) return
    const next = Math.max(0, Math.min(max, el.scrollLeft + dx))
    if (next === el.scrollLeft) return // already at the end — do not swallow it
    el.scrollLeft = next
    e.preventDefault()
    e.stopPropagation()
  }, [])

  if (grid.strikes.length === 0) {
    return <div className="dim subtle">No contracts in this window to map.</div>
  }

  const priced = [...grid.cells.values()].filter((c) => c.state === 'priced').length
  const flat = grid.annualLo !== null && grid.annualHi !== null && !(grid.annualHi > grid.annualLo)
  // Money in is a gain, money out is a loss — the same two tokens the rest of
  // the app already spends on that distinction.
  const ink = sideInk(side)

  return (
    <div className="oh" data-oh-strikes={grid.strikes.length} data-oh-cols={grid.columns.length}>
      <div className="oh-legend">
        <span className="dim">
          {side === 'short' ? 'CREDIT at mid' : 'DEBIT at mid'} · deeper = larger annualised
          {side === 'short' ? ' credit' : ' cost'}
        </span>
        <span className="oh-ramp" aria-hidden="true">
          {[0.1, 0.3, 0.5, 0.72].map((t) => (
            <i
              key={t}
              style={{ background: `color-mix(in srgb, ${ink} ${Math.round(t * 100)}%, transparent)` }}
            />
          ))}
        </span>
      </div>

      <div className="oh-scroll" ref={scroller} onWheel={onWheel}>
        <table className="oh-table">
          <thead>
            <tr>
              <th className="oh-corner" scope="col">strike</th>
              {grid.columns.map((c) => (
                <th key={c.expiration} scope="col" title={c.expiration}>
                  <span className="oh-dte">{c.dte}d</span>
                  <span className="dim oh-exp">{c.expiration.slice(5)}</span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {grid.strikes.map((k) => (
              <tr key={k}>
                <th className="oh-strike" scope="row">{k.toFixed(k % 1 === 0 ? 0 : 1)}</th>
                {grid.columns.map((col) => {
                  const cell = grid.cells.get(cellKey(k, col.expiration))
                  if (!cell) {
                    // Not listed at this pair. A true statement, and a
                    // different one from "quoted but nobody is bidding".
                    return <td key={col.expiration} className="oh-cell oh-absent" title="not listed" />
                  }
                  const t = rampPosition(cell.annual, grid.annualLo, grid.annualHi)
                  const isAnchor =
                    !!anchor && anchor.strike === k && anchor.expiration === col.expiration
                  const isSel = selected === cell.contract.occ_symbol
                  const cls = [
                    'oh-cell',
                    cell.state !== 'priced' ? `oh-${cell.state}` : '',
                    isAnchor ? 'oh-anchor' : '',
                    isSel ? 'oh-sel' : '',
                  ].filter(Boolean).join(' ')
                  return (
                    <td
                      key={col.expiration}
                      className={cls}
                      // Colour ONLY on a real mid. An unpriced cell that took a
                      // ramp colour would advertise a yield computed from a
                      // number that does not exist.
                      // A floor of 10%, so a priced cell at the bottom of the
                      // ramp still reads as green-or-red rather than as blank.
                      // Blank already means "not listed" one column over.
                      style={t === null ? undefined : {
                        background: `color-mix(in srgb, ${ink} ${Math.round(10 + t * 62)}%, transparent)`,
                      }}
                      title={titleFor(cell, col.dte, side)}
                      data-occ={cell.contract.occ_symbol}
                      data-oh-state={cell.state}
                      onClick={() => onPick?.(cell.contract)}
                    >
                      <span className="oh-mid">
                        {cell.state === 'priced' ? money(cell.mid)
                          : cell.state === 'no-bid' ? 'no bid' : '—'}
                      </span>
                      <span className="oh-sub">
                        {cell.state === 'priced' ? fmtAnnual(cell.annual) : ''}
                        {typeof cell.contract.delta === 'number'
                          ? ` · Δ${cell.contract.delta.toFixed(2)}`
                          : ' · Δ—'}
                      </span>
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="oh-foot dim subtle">
        {priced} of {grid.cells.size} cells priced · rate is
        {side === 'short' ? ' credit' : ' debit'} ÷ strike, compounded to a year
        {priced < grid.cells.size ? ' · unpriced cells carry no mid and no colour' : ''}
        {flat ? ' · every priced cell has the same yield, so nothing is shaded' : ''}
      </div>
    </div>
  )
}

function titleFor(cell: GridCell, dte: number, side: LegSide): string {
  const c = cell.contract
  const head = `${c.strike} ${c.right === 'P' ? 'put' : 'call'} · ${c.expiration} (${dte}d)`
  if (cell.state === 'no-quote') return `${head}\nno quote`
  if (cell.state === 'no-bid') {
    return `${head}\nno bid — nobody is bidding, so there is no mid to annualise`
  }
  const bid = typeof c.bid === 'number' ? c.bid.toFixed(2) : '—'
  const ask = typeof c.ask === 'number' ? c.ask.toFixed(2) : '—'
  const iv = typeof c.iv === 'number' ? `${(c.iv * 100).toFixed(0)}%` : '—'
  const d = typeof c.delta === 'number' ? c.delta.toFixed(3) : '—'
  // The period return is spelled out, because it is the step that makes a high
  // strike comparable to a low one and it is invisible in the annualised
  // figure alone.
  const period = cell.mid === null ? '—' : `${((cell.mid / c.strike) * 100).toFixed(3)}%`
  return (
    `${head}\n` +
    `${bid} × ${ask}   mid ${cell.mid === null ? '—' : cell.mid.toFixed(2)}\n` +
    `${period} of strike over ${cell.tdte} session${cell.tdte === 1 ? '' : 's'} ` +
    `(${dte} cal days)  →  ${fmtAnnual(cell.annual)}/yr ` +
    `${side === 'short' ? 'credit' : 'cost'}\n` +
    `Δ ${d}   IV ${iv}`
  )
}
