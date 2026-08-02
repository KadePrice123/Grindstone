/**
 * The news feed (news.gs) — everything the local store holds, newest first.
 * Existed as an API and a search intent long before it had a page; the
 * gesture wheel's News segment needed somewhere real to land.
 */
import { useCallback, useEffect, useState } from 'react'
import type { Route } from '../App'
import { api, ApiError, NewsItem } from '../api'
import { NewsMiniIcon } from '../components/icons'

function age(iso: string): string {
  const m = Math.floor((Date.now() - new Date(iso).getTime()) / 60000)
  if (m < 60) return `${Math.max(m, 0)}m ago`
  const h = Math.floor(m / 60)
  return h < 48 ? `${h}h ago` : `${Math.floor(h / 24)}d ago`
}

export function NewsPage({ onNavigate }: { onNavigate: (r: Route) => void }) {
  const [items, setItems] = useState<NewsItem[] | null>(null)
  const [filter, setFilter] = useState('')
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      setItems(await api<NewsItem[]>('GET', '/api/news?limit=100'))
      setError(null)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const f = filter.trim().toUpperCase()
  const shown = (items ?? []).filter(
    (n) =>
      !f ||
      n.headline.toUpperCase().includes(f) ||
      n.symbols.some((s) => s.toUpperCase() === f)
  )

  return (
    <div className="page">
      <div className="page-head">
        <NewsMiniIcon />
        <h1>News</h1>
        <input
          className="field news-filter"
          placeholder="Filter by ticker or headline…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          spellCheck={false}
        />
      </div>

      {error ? <div className="test-result bad">{error}</div> : null}

      <div className="card">
        {items == null ? (
          <div className="dim">Loading…</div>
        ) : shown.length === 0 ? (
          <div className="dim">
            {items.length === 0
              ? 'Nothing in the local store yet — news arrives with an Alpaca account, or via Data management jobs.'
              : `Nothing matches “${filter}”.`}
          </div>
        ) : (
          shown.map((n) => (
            <div
              className="news-row"
              key={n.id}
              onClick={() => onNavigate({ name: 'article', id: n.id })}
              title="Read the article"
            >
              <div className="news-head">{n.headline}</div>
              <div className="subtle">
                {n.source} · {age(n.created_at)} · {n.symbols.slice(0, 6).join(' ')}
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  )
}
