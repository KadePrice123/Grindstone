import { useCallback, useEffect, useState } from 'react'
import { api, ApiError, SymbolSummary } from '../api'
import { Bar, Chart, IndicatorKey } from '../components/Chart'
import { ChartMiniIcon } from '../components/icons'

const TIMEFRAMES: { key: string; label: string }[] = [
  { key: '1Min', label: '1m' },
  { key: '5Min', label: '5m' },
  { key: '15Min', label: '15m' },
  { key: '1Hour', label: '1H' },
  { key: '1Day', label: '1D' },
]

const INDICATORS: { key: IndicatorKey; label: string }[] = [
  { key: 'vol', label: 'Volume' },
  { key: 'sma20', label: 'SMA 20' },
  { key: 'sma50', label: 'SMA 50' },
  { key: 'ema20', label: 'EMA 20' },
  { key: 'rsi14', label: 'RSI 14' },
]

function fmt(n: number | null | undefined, dp = 2): string {
  return n == null ? '—' : n.toLocaleString(undefined, { maximumFractionDigits: dp })
}

function age(iso: string): string {
  const m = Math.floor((Date.now() - new Date(iso).getTime()) / 60000)
  if (m < 60) return `${Math.max(m, 0)}m ago`
  const h = Math.floor(m / 60)
  return h < 48 ? `${h}h ago` : `${Math.floor(h / 24)}d ago`
}

export function SymbolPage({
  symbol,
  onNavigate,
}: {
  symbol: string
  onNavigate: (r: { name: 'article'; id?: number; url?: string }) => void
}) {
  const [data, setData] = useState<SymbolSummary | null>(null)
  const [bars, setBars] = useState<Bar[]>([])
  const [barSource, setBarSource] = useState<string>('')
  const [barNote, setBarNote] = useState<string>('')
  const [timeframe, setTimeframe] = useState('1Day')
  const [indicators, setIndicators] = useState<IndicatorKey[]>(['vol'])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let stop = false
    setData(null)
    setError(null)
    const load = async () => {
      try {
        const d = await api<SymbolSummary>(
          'GET',
          `/api/symbols/${encodeURIComponent(symbol)}/summary`
        )
        if (!stop) {
          setData(d)
          setError(null)
        }
      } catch (e) {
        if (!stop) setError(e instanceof ApiError ? e.message : String(e))
      }
    }
    load()
    const t = window.setInterval(load, 30_000)
    return () => {
      stop = true
      window.clearInterval(t)
    }
  }, [symbol])

  const loadBars = useCallback(async () => {
    try {
      const b = await api<{ bars: Bar[]; source: string; reason?: string }>(
        'GET',
        `/api/symbols/${encodeURIComponent(symbol)}/bars?timeframe=${timeframe}&limit=600`
      )
      setBars(b.bars)
      setBarSource(b.source)
      setBarNote(b.reason ?? '')
    } catch (e) {
      setBars([])
      setBarNote(e instanceof ApiError ? e.message : String(e))
    }
  }, [symbol, timeframe])

  useEffect(() => {
    setBars([])
    loadBars()
  }, [loadBars])

  const toggle = (k: IndicatorKey) =>
    setIndicators((cur) => (cur.includes(k) ? cur.filter((x) => x !== k) : [...cur, k]))

  const q = data?.quote
  const up = (q?.change ?? 0) >= 0

  return (
    <div className="page wide">
      <div className="page-head">
        <ChartMiniIcon />
        <h1>{symbol}</h1>
        <span className="dim">{data?.name}</span>
        {q?.available ? (
          <span className="quote-inline">
            <span className="quote-price sm">{fmt(q.price)}</span>
            <span className={up ? 'quote-change up' : 'quote-change down'}>
              {q.change != null ? `${up ? '+' : ''}${fmt(q.change)} (${fmt(q.change_pct)}%)` : ''}
            </span>
            <span className="omni-tag">{q.source}</span>
          </span>
        ) : (
          <span className="subtle">{q?.reason ?? ''}</span>
        )}
      </div>

      {error ? <div className="test-result bad">{error}</div> : null}

      <div className="card chart-card">
        <div className="chart-toolbar">
          <div className="seg">
            {TIMEFRAMES.map((t) => (
              <button
                key={t.key}
                className={`seg-btn${timeframe === t.key ? ' on' : ''}`}
                onClick={() => setTimeframe(t.key)}
              >
                {t.label}
              </button>
            ))}
          </div>
          <div className="seg">
            {INDICATORS.map((i) => (
              <button
                key={i.key}
                className={`seg-btn${indicators.includes(i.key) ? ' on' : ''}`}
                onClick={() => toggle(i.key)}
              >
                {i.label}
              </button>
            ))}
          </div>
          <span className="subtle chart-source">
            {bars.length > 0 ? `${bars.length} bars · ${barSource}` : barNote || 'loading…'}
          </span>
        </div>
        {bars.length > 0 ? (
          <Chart bars={bars} indicators={indicators} height={indicators.includes('rsi14') ? 480 : 400} />
        ) : (
          <div className="chart-empty dim">{barNote || 'No bars for this timeframe.'}</div>
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
              onClick={() => onNavigate({ name: 'article', id: n.id })}
              title="Read the article"
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
