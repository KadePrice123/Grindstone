/**
 * The at-a-glance numbers that make a chart mean something: where it is now,
 * what it did today, and the range it is sitting inside.
 *
 * Every field is optional because our sources disagree about what they will
 * give us — Alpaca's snapshot carries the full daily bar and a bid/ask, the
 * keyless Yahoo fallback carries last and previous close and nothing else. A
 * missing number shows as an em dash rather than a zero, because a fabricated
 * zero in a price grid is worse than an obvious blank.
 */
import { Quote } from '../api'

export interface Range {
  label: string
  low: number
  high: number
}

function num(n: number | null | undefined, dp = 2): string {
  return n == null ? '—' : n.toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: dp })
}

/** Volume reads better as 41.2M than as 41,215,903. */
export function compact(n: number | null | undefined): string {
  if (n == null) return '—'
  const abs = Math.abs(n)
  if (abs >= 1e12) return `${(n / 1e12).toFixed(2)}T`
  if (abs >= 1e9) return `${(n / 1e9).toFixed(2)}B`
  if (abs >= 1e6) return `${(n / 1e6).toFixed(2)}M`
  if (abs >= 1e3) return `${(n / 1e3).toFixed(1)}K`
  return n.toLocaleString(undefined, { maximumFractionDigits: 0 })
}

function Metric({ label, value, cls }: { label: string; value: string; cls?: string }) {
  return (
    <div className="metric">
      <span className="metric-label">{label}</span>
      <span className={cls ? `metric-value ${cls}` : 'metric-value'}>{value}</span>
    </div>
  )
}

export function Metrics({ quote, range }: { quote?: Quote | null; range?: Range | null }) {
  if (!quote) return <div className="metrics-empty subtle">Loading quote…</div>
  if (!quote.available) {
    return <div className="metrics-empty subtle">{quote.reason ?? 'No quote available.'}</div>
  }

  const up = (quote.change ?? 0) >= 0
  const sign = up ? '+' : ''
  const change =
    quote.change == null
      ? '—'
      : `${sign}${num(quote.change)} (${sign}${num(quote.change_pct)}%)`

  const yearRange: Range | null =
    quote.year_low != null && quote.year_high != null
      ? { label: '52-week range', low: quote.year_low, high: quote.year_high }
      : (range ?? null)

  return (
    <div className="metrics">
      <div className="metric wide">
        <span className="metric-label">Last</span>
        <span className="metric-price">{num(quote.price)}</span>
      </div>
      <div className="metric wide">
        <span className="metric-label">Today</span>
        <span className={up ? 'metric-price up' : 'metric-price down'}>{change}</span>
      </div>
      <Metric label="Open" value={num(quote.day_open)} />
      <Metric label="High" value={num(quote.day_high)} />
      <Metric label="Low" value={num(quote.day_low)} />
      <Metric label="Prev close" value={num(quote.prev_close)} />
      <Metric label="Volume" value={compact(quote.day_volume)} />
      {quote.avg_volume != null ? (
        <Metric label="Avg vol (3m)" value={compact(quote.avg_volume)} />
      ) : null}
      {quote.bid != null || quote.ask != null ? (
        <Metric label="Bid / Ask" value={`${num(quote.bid)} / ${num(quote.ask)}`} />
      ) : null}
      {/* The provider's own 52-week range beats one derived from the bars we
          happen to hold — it is the whole year, not our slice of it. */}
      {yearRange ? (
        <Metric label={yearRange.label} value={`${num(yearRange.low)} – ${num(yearRange.high)}`} />
      ) : null}
      {quote.market_cap != null ? (
        <Metric label="Market cap" value={compact(quote.market_cap)} />
      ) : null}
      {quote.source ? <Metric label="Source" value={quote.source} cls="dim-value" /> : null}
    </div>
  )
}

/** Low/high across a bar series — the range the chart is actually showing. */
export function barRange(
  bars: { low: number; high: number }[],
  label: string
): Range | null {
  if (bars.length === 0) return null
  let low = bars[0].low
  let high = bars[0].high
  for (const b of bars) {
    if (b.low < low) low = b.low
    if (b.high > high) high = b.high
  }
  return { label, low, high }
}
