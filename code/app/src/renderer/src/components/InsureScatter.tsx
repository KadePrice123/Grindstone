/** The insurance line — the Insure page's one graph.
 *
 *  x = what this risk class has actually COST (measured pure premium,
 *  annualised %/yr on the strike). y = what the market PAYS today (offered
 *  mid ÷ strike, same unit through the same optgrid functions). Both axes are
 *  the same quantity in the same unit, so the fair price for every symbol at
 *  once is the single 45° diagonal y = x — above it the market overpays, and
 *  the vertical gap IS the annualised edge.
 *
 *  A dumb SVG surface in the OptHeatmap mold: every number comes from
 *  optgrid.ts (gate-probed) or the scan payload (gate-probed server-side);
 *  this file only draws. Deliberately NOT lightweight-charts — the house
 *  engine's monopoly covers time-like panels, and its synthetic lattice would
 *  quantize a continuous rate axis (docs/INSURE.md, arbitrated).
 *
 *  The plot area is SQUARE in unit terms: one %/yr spans the same pixels on
 *  both axes, so the diagonal is visually 45° and height above it reads
 *  honestly. Axes auto-scale, capped at 60%/yr with an "n beyond" count —
 *  never a silent clip.
 */
import { useMemo, useState } from 'react'
import { claimRing, confidenceOf } from '../optgrid'

export interface InsureDot {
  symbol: string
  occ: string
  expiration: string
  dte: number
  strike: number
  /** Annualised %/yr, both from optgrid.insurePoint — never recomputed here. */
  x: number
  y: number
  edgeAnnual: number
  nExp: number
  claimFreq: number
  impliedDelta: number | null
  label: string
}

const AXIS_CAP = 60 // %/yr — beyond this the dot is counted, not drawn

/** Slot palette in favorites order — hue says symbol, nothing else. */
export const SYMBOL_INK = [
  '#6BA4E8', '#D98324', '#2EBD85', '#C678DD', '#E5C07B', '#56B6C2', '#E06C75',
]

export function InsureScatter({
  dots, hidden, onPick, height = 380,
}: {
  dots: InsureDot[]
  /** Symbols toggled off via the legend chips — the page owns that state. */
  hidden: Set<string>
  onPick?: (d: InsureDot) => void
  height?: number
}) {
  const [hover, setHover] = useState<InsureDot | null>(null)

  const shown = dots.filter((d) => !hidden.has(d.symbol))
  const beyond = shown.filter((d) => d.x > AXIS_CAP || d.y > AXIS_CAP).length
  const drawn = shown.filter((d) => d.x <= AXIS_CAP && d.y <= AXIS_CAP)

  // One scale for both axes — the 45° promise. Padded a little past the data.
  const max = useMemo(() => {
    const m = Math.max(5, ...drawn.map((d) => Math.max(d.x, d.y)))
    return Math.min(AXIS_CAP, Math.ceil((m * 1.15) / 5) * 5)
  }, [drawn])

  const PAD = { l: 46, r: 14, t: 10, b: 34 }
  const side = height - PAD.t - PAD.b
  const W = side + PAD.l + PAD.r
  const px = (v: number) => PAD.l + (v / max) * side
  const py = (v: number) => PAD.t + side - (v / max) * side

  const symbols = [...new Set(dots.map((d) => d.symbol))]
  const ink = (sym: string) => SYMBOL_INK[symbols.indexOf(sym) % SYMBOL_INK.length]

  // Top three by edge carry standing labels; the rest speak on hover.
  const labeled = [...drawn].sort((a, b) => b.edgeAnnual - a.edgeAnnual).slice(0, 3)

  const ticks = useMemo(() => {
    const step = max <= 15 ? 5 : max <= 30 ? 10 : 20
    const out: number[] = []
    for (let v = 0; v <= max; v += step) out.push(v)
    return out
  }, [max])

  return (
    <div className="insure-plot" data-insure-dots={drawn.length}
      data-insure-beyond={beyond} style={{ position: 'relative' }}>
      <svg viewBox={`0 0 ${W} ${height}`} width="100%" role="img"
        aria-label="offered credit vs measured cost, %/yr">
        {/* frame + gridlines */}
        {ticks.map((v) => (
          <g key={v}>
            <line x1={px(v)} y1={PAD.t} x2={px(v)} y2={PAD.t + side}
              stroke="var(--line)" strokeWidth="1" />
            <line x1={PAD.l} y1={py(v)} x2={PAD.l + side} y2={py(v)}
              stroke="var(--line)" strokeWidth="1" />
            <text x={px(v)} y={height - 14} textAnchor="middle" className="insure-tick">
              {v}%
            </text>
            <text x={PAD.l - 8} y={py(v) + 3} textAnchor="end" className="insure-tick">
              {v}%
            </text>
          </g>
        ))}
        {/* THE FAIR LINE: y = x. Above it the market pays more than the risk
            has cost; the label says so in words. */}
        <line x1={px(0)} y1={py(0)} x2={px(max)} y2={py(max)}
          stroke="var(--text-dim)" strokeWidth="1" strokeDasharray="5 4" />
        <text x={px(max * 0.62)} y={py(max * 0.62) + 14} className="insure-fair"
          transform={`rotate(-45 ${px(max * 0.62)} ${py(max * 0.62) + 14})`}>
          fair — pays what it has cost
        </text>
        {/* axis names */}
        <text x={PAD.l + side / 2} y={height - 2} textAnchor="middle" className="insure-axis">
          required credit — measured cost of claims, %/yr of strike
        </text>
        <text x={12} y={PAD.t + side / 2} textAnchor="middle" className="insure-axis"
          transform={`rotate(-90 12 ${PAD.t + side / 2})`}>
          offered credit today, %/yr
        </text>
        {/* dots: hue = symbol, ring = measured claim frequency, fill = tier */}
        {drawn.map((d) => {
          const tier = confidenceOf(d.nExp)
          const ring = claimRing(d.claimFreq)
          const c = ink(d.symbol)
          return (
            <g key={`${d.symbol}:${d.occ}`}
              style={{ cursor: onPick ? 'pointer' : 'default' }}
              onMouseEnter={() => setHover(d)}
              onMouseLeave={() => setHover((h) => (h === d ? null : h))}
              onClick={() => onPick?.(d)}>
              <circle cx={px(d.x)} cy={py(d.y)} r={4.5 + ring * 1.5}
                fill="none" stroke="#A98EDA" strokeOpacity={0.25 + ring * 0.25}
                strokeWidth={1 + ring} />
              <circle cx={px(d.x)} cy={py(d.y)} r={4}
                fill={tier === 'solid' ? c : 'transparent'}
                stroke={c} strokeWidth="1.6" />
            </g>
          )
        })}
        {labeled.map((d) => (
          <text key={`lbl:${d.symbol}:${d.occ}`} x={px(d.x) + 8} y={py(d.y) - 6}
            className="insure-dot-label" fill={ink(d.symbol)}>
            {d.label}
          </text>
        ))}
      </svg>
      {hover ? (
        <div className="insure-hover float-panel" data-insure-hover={hover.occ}>
          <div className="em">{hover.label}</div>
          <div className="dim subtle">
            offered {hover.y.toFixed(1)}%/yr · needs {hover.x.toFixed(1)}%/yr ·
            edge {hover.edgeAnnual >= 0 ? '+' : ''}{hover.edgeAnnual.toFixed(1)}%/yr
          </div>
          <div className="dim subtle">
            assigned {(hover.claimFreq * 100).toFixed(1)}% of {hover.nExp} expirations
            {hover.impliedDelta != null
              ? ` · Δ said ${(hover.impliedDelta * 100).toFixed(0)}%` : ' · Δ —'}
          </div>
          <div className="dim subtle">click to open on the Opt page</div>
        </div>
      ) : null}
    </div>
  )
}
