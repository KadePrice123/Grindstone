/**
 * The search results page — where Enter lands when you don't pick a
 * suggestion. Google-shaped: paginated results, and when the query is about
 * a ticker, a live mini chart sits above the first result and links to the
 * ticker's page.
 */
import { useCallback, useEffect, useState } from 'react'
import { parseRoute, type Route } from '../App'
import { api, ApiError, Quote, SearchResult, SymbolSummary } from '../api'
import { pageRoute } from '../urls'
import { Bar, Chart } from '../components/Chart'
import { Metrics, barRange } from '../components/Metrics'
import { BrowserMiniIcon, ChartMiniIcon, NewsMiniIcon, PageMiniIcon } from '../components/icons'

interface PageResult {
  query: string
  page: number
  pages: number
  total: number
  inhouse: SearchResult[]
  results: SearchResult[]
  featured: { symbol: string; name: string; asset_class: string } | null
  web?: { used: boolean; installed: boolean; available: boolean }
}

function RowIcon({ r }: { r: SearchResult }) {
  if (r.type === 'symbol') return <ChartMiniIcon />
  if (r.type === 'news' || r.type === 'web-news') return <NewsMiniIcon />
  if (r.type === 'web') return <BrowserMiniIcon />
  return <PageMiniIcon />
}

const IN_HOUSE = new Set(['symbol', 'news', 'page', 'action'])

function age(iso?: string): string {
  if (!iso) return ''
  const m = Math.floor((Date.now() - new Date(iso).getTime()) / 60000)
  if (m < 60) return `${Math.max(m, 0)}m ago`
  const h = Math.floor(m / 60)
  return h < 48 ? `${h}h ago` : `${Math.floor(h / 24)}d ago`
}

export function SearchPage({
  query,
  onNavigate,
}: {
  query: string
  onNavigate: (r: Route) => void
}) {
  const [data, setData] = useState<PageResult | null>(null)
  const [page, setPage] = useState(1)
  const [bars, setBars] = useState<Bar[]>([])
  const [quote, setQuote] = useState<Quote | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setPage(1)
    setBars([]) // never show the previous query's ticker under this one's name
    setQuote(null)
  }, [query])

  const load = useCallback(async () => {
    try {
      const d = await api<PageResult>(
        'GET',
        `/api/search/page?q=${encodeURIComponent(query)}&page=${page}&per_page=10`
      )
      setData(d)
      setError(null)
      if (d.featured) {
        // A year of bars for the 52-week range, the last quarter for the
        // chart — a small card reads as noise at full-year density.
        const b = await api<{ bars: Bar[] }>(
          'GET',
          `/api/symbols/${encodeURIComponent(d.featured.symbol)}/bars?timeframe=1Day&limit=260`
        )
        setBars(b.bars)
        // The quote is a separate call on purpose: bars come from the local
        // store or a cached fetch, the quote is live, and a slow quote must
        // not hold the chart hostage.
        const s = await api<SymbolSummary>(
          'GET',
          `/api/symbols/${encodeURIComponent(d.featured.symbol)}/summary`
        )
        setQuote(s.quote)
      } else {
        setBars([])
        setQuote(null)
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    }
  }, [query, page])

  useEffect(() => {
    load()
  }, [load])

  const open = (r: SearchResult) => {
    if (r.type === 'symbol' && r.symbol) onNavigate({ name: 'symbol', symbol: r.symbol })
    else if (r.type === 'news' && typeof r.id === 'number') {
      // Our own news has clean article text — read it in-app.
      onNavigate({ name: 'article', id: r.id })
    } else if (r.type === 'page') {
      const key = pageRoute(r.page ?? '')
      if (key) onNavigate(parseRoute(key))
    } else if (r.url) {
      // A web result is a WEBSITE. Extracting it to plain text stripped the
      // layout that makes it make sense (a Wikipedia infobox became a wall
      // of pipes), so open the real page in a browser tab.
      window.grindstone.openUrl(r.url)
    }
  }

  const pages = data?.pages ?? 1
  const window_ = Array.from({ length: Math.min(pages, 10) }, (_, i) => {
    const start = Math.max(1, Math.min(page - 4, pages - 9))
    return start + i
  }).filter((p) => p >= 1 && p <= pages)

  return (
    <div className="page search-page">
      <div className="search-head">
        <span className="dim">
          {data ? `${data.total} result${data.total === 1 ? '' : 's'} for` : 'Searching for'}
        </span>
        <strong>{query}</strong>
      </div>

      {error ? <div className="test-result bad">{error}</div> : null}

      {data?.featured ? (
        <div
          className="card featured"
          onClick={() => onNavigate({ name: 'symbol', symbol: data.featured!.symbol })}
          title={`Open ${data.featured.symbol}`}
        >
          <div className="featured-head">
            <strong>{data.featured.symbol}</strong>
            <span className="dim">{data.featured.name}</span>
            <span className="featured-link">Open ticker page →</span>
          </div>
          {bars.length > 0 ? (
            <Chart bars={bars.slice(-90)} height={120} compact />
          ) : (
            <div className="dim subtle">No chart data for this symbol yet.</div>
          )}
          {/* A line with no numbers on it is decoration. */}
          <Metrics
            quote={quote}
            range={barRange(bars, bars.length >= 200 ? '52-week range' : `${bars.length}-day range`)}
          />
        </div>
      ) : null}

      {data && data.inhouse.length > 0 ? (
        <div className="card">
          <h2 className="section-head">
            Tickers &amp; news <span className="src-tag in">Grindstone</span>
          </h2>
          {data.inhouse.map((r, i) => (
            <div
              className={r.ready === false ? 'result-row dead' : 'result-row'}
              key={`in:${r.type}:${r.symbol ?? r.id ?? r.page}:${i}`}
              onClick={() => r.ready !== false && open(r)}
            >
              <span className="result-ico">
                <RowIcon r={r} />
              </span>
              <div className="result-body">
                <div className="result-title">{r.title}</div>
                <div className="subtle">
                  {r.subtitle}
                  {r.created_at ? ` · ${age(r.created_at)}` : ''}
                </div>
              </div>
            </div>
          ))}
        </div>
      ) : null}

      <div className="card">
        {data && data.inhouse.length > 0 && data.results.length > 0 ? (
          <h2 className="section-head">
            From the web
            {data.web?.available === false ? (
              <span className="subtle"> · search unavailable right now</span>
            ) : null}
          </h2>
        ) : null}
        {data == null ? (
          <div className="dim">Loading…</div>
        ) : data.results.length === 0 && data.inhouse.length === 0 ? (
          <div className="dim">Nothing matched “{query}”.</div>
        ) : data.results.length === 0 ? (
          <div className="dim">
            No web results
            {data.web?.installed === false ? ' — web search is not installed.' : '.'}
          </div>
        ) : (
          data.results.map((r, i) => (
            <div
              className="result-row"
              key={`${r.type}:${r.symbol ?? r.id ?? r.page}:${i}`}
              onClick={() => open(r)}
            >
              <span className="result-ico">
                <RowIcon r={r} />
              </span>
              <div className="result-body">
                <div className="result-title">
                  {r.title}
                  {IN_HOUSE.has(r.type) ? (
                    <span className="src-tag in">Grindstone</span>
                  ) : (
                    <span className="src-tag web">{r.site ?? 'web'}</span>
                  )}
                </div>
                <div className="subtle">
                  {r.subtitle}
                  {r.created_at ? ` · ${age(r.created_at)}` : ''}
                </div>
              </div>
            </div>
          ))
        )}
      </div>

      {pages > 1 ? (
        <div className="pager">
          <button className="btn" disabled={page <= 1} onClick={() => setPage(page - 1)}>
            ‹ Prev
          </button>
          {window_.map((p) => (
            <button
              key={p}
              className={`btn page-btn${p === page ? ' primary' : ''}`}
              onClick={() => setPage(p)}
            >
              {p}
            </button>
          ))}
          <button className="btn" disabled={page >= pages} onClick={() => setPage(page + 1)}>
            Next ›
          </button>
        </div>
      ) : null}
    </div>
  )
}
