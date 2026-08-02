/**
 * Reader view — the actual article text, in the app.
 *
 * Publisher pages are mostly not the article: ads, paywalls, "Create a
 * Watchlist" rails, and in Benzinga's case often a headline-only stub whose
 * body lives elsewhere. The backend gives us the feed's body when it has one
 * and extracts the page when it does not; this renders that text and keeps
 * "Open original" one click away.
 */
import { useEffect, useState } from 'react'
import type { Route } from '../App'
import { api, ApiError } from '../api'
import { NewsMiniIcon } from '../components/icons'

interface Article {
  id: number | null
  headline: string
  source: string
  symbols: string[]
  created_at: string
  url: string
  text: string
  readable: boolean
  reason: string
}

function when(iso: string): string {
  if (!iso) return ''
  const d = new Date(iso)
  const mins = Math.floor((Date.now() - d.getTime()) / 60000)
  const rel = mins < 60 ? `${Math.max(mins, 0)}m ago` : mins < 2880 ? `${Math.floor(mins / 60)}h ago` : ''
  return rel ? `${d.toLocaleString()} · ${rel}` : d.toLocaleString()
}

export function ArticlePage({
  id,
  url,
  onNavigate,
}: {
  id?: number
  url?: string
  onNavigate: (r: Route) => void
}) {
  const [a, setA] = useState<Article | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let stop = false
    setA(null)
    setError(null)
    const q = id != null ? `id=${id}` : `url=${encodeURIComponent(url ?? '')}`
    api<Article>('GET', `/api/article?${q}`)
      .then((d) => !stop && setA(d))
      .catch((e) => !stop && setError(e instanceof ApiError ? e.message : String(e)))
    return () => {
      stop = true
    }
  }, [id, url])

  const paragraphs = (a?.text ?? '').split(/\n{2,}/).filter((p) => p.trim())

  return (
    <div className="page article-page">
      <div className="page-head">
        <NewsMiniIcon />
        <h1 className="article-title">{a?.headline || (a ? 'Article' : 'Loading…')}</h1>
      </div>

      {error ? <div className="test-result bad">{error}</div> : null}

      {a ? (
        <>
          <div className="article-meta">
            {a.source ? <span className="src-tag web">{a.source}</span> : null}
            <span className="subtle">{when(a.created_at)}</span>
            {a.symbols.slice(0, 6).map((s) => (
              <button
                key={s}
                className="sym-chip"
                title={`Open ${s}`}
                onClick={() => onNavigate({ name: 'symbol', symbol: s })}
              >
                {s}
              </button>
            ))}
            {a.url ? (
              <button
                className="btn article-original"
                onClick={() => window.grindstone.openUrl(a.url)}
                title="Open the publisher's page in a tab"
              >
                Open original ↗
              </button>
            ) : null}
          </div>

          {a.readable ? (
            <article className="article-body">
              {paragraphs.map((p, i) => (
                <p key={i}>{p}</p>
              ))}
            </article>
          ) : (
            <div className="card dim">{a.reason}</div>
          )}
        </>
      ) : error ? null : (
        <div className="card dim">Fetching the article…</div>
      )}
    </div>
  )
}
