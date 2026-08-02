import { useEffect, useState } from 'react'
import { api, ApiError, SymbolSummary } from '../api'
import { ChartMiniIcon } from '../components/icons'

function fmt(n: number | null | undefined, dp = 2): string {
  return n == null ? '—' : n.toLocaleString(undefined, { maximumFractionDigits: dp })
}

function age(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime()
  const m = Math.floor(ms / 60000)
  if (m < 60) return `${Math.max(m, 0)}m ago`
  const h = Math.floor(m / 60)
  if (h < 48) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}

export function SymbolPage({ symbol, onBack }: { symbol: string; onBack: () => void }) {
  const [data, setData] = useState<SymbolSummary | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let stop = false
    const load = async () => {
      try {
        const d = await api<SymbolSummary>('GET', `/api/symbols/${encodeURIComponent(symbol)}/summary`)
        if (!stop) setData(d)
      } catch (e) {
        if (!stop) setError(e instanceof ApiError ? e.message : String(e))
      }
    }
    load()
    const t = window.setInterval(load, 30_000) // refresh; real streaming lands in M3
    return () => {
      stop = true
      window.clearInterval(t)
    }
  }, [symbol])

  const q = data?.quote
  const up = (q?.change ?? 0) >= 0

  return (
    <div className="page">
      <div className="page-head">
        <button className="back" onClick={onBack} title="Back">
          ←
        </button>
        <ChartMiniIcon />
        <h1>{symbol}</h1>
        <span className="dim">{data?.name}</span>
      </div>

      {error ? <div className="test-result bad">{error}</div> : null}

      <div className="card">
        {q?.available ? (
          <div className="quote-row">
            <span className="quote-price">{fmt(q.price)}</span>
            <span className={up ? 'quote-change up' : 'quote-change down'}>
              {q.change != null ? `${up ? '+' : ''}${fmt(q.change)} (${fmt(q.change_pct)}%)` : ''}
            </span>
            <span className="subtle">
              {q.bid != null ? `bid ${fmt(q.bid)} · ask ${fmt(q.ask)} · ` : ''}
              O {fmt(q.day_open)} H {fmt(q.day_high)} L {fmt(q.day_low)}
            </span>
            <span className="omni-tag" title="Where this number comes from">
              {q.source}
            </span>
          </div>
        ) : (
          <div className="dim">
            No quote — {q?.reason ?? 'loading…'}
          </div>
        )}
      </div>

      <div className="card">
        <h2>News</h2>
        {data == null ? (
          <div className="dim">Loading…</div>
        ) : data.news.length === 0 ? (
          <div className="dim">Nothing recent for {symbol} in the local store.</div>
        ) : (
          data.news.map((n) => (
            <div
              className="news-row"
              key={n.id}
              onClick={() => n.url && window.open(n.url)}
              title="Open article in your browser"
            >
              <div className="news-head">{n.headline}</div>
              <div className="subtle">
                {n.source} · {age(n.created_at)} · {n.symbols.slice(0, 5).join(' ')}
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  )
}
