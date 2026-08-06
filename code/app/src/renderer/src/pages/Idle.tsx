import { useEffect, useState } from 'react'
import { api, QuoteSnap, SearchResult } from '../api'
import { Logo } from '../components/Logo'
import { Omnibox } from '../components/Omnibox'
import { pageIcon } from '../components/icons'
import type { Favorite } from '../favorites'
import { classify, pageRoute } from '../urls'
import { parseRoute, type Route } from '../App'
import '../favorites.css'

export function Idle({
  onNavigate,
}: {
  onNavigate: (r: Route) => void
  onLocked: () => void
}) {
  // null = not loaded yet (render nothing rather than a lying empty state)
  const [favs, setFavs] = useState<Favorite[] | null>(null)
  const [quotes, setQuotes] = useState<Record<string, QuoteSnap>>({})

  // The grid's truth: fetched on mount and after ANY view mutates favorites
  // (main broadcasts). Symbol quotes ride one batch call per load — same
  // fetch-once-per-show rule as the wheel's ticker segments.
  useEffect(() => {
    let live = true
    // Star two pages quickly and two loads overlap; whichever RESPONDS last
    // would otherwise win, installing the older list. Newest load wins.
    let seq = 0
    const load = async () => {
      const mine = ++seq
      let favorites: Favorite[]
      try {
        favorites = (await api<{ favorites: Favorite[] }>('GET', '/api/favorites')).favorites
      } catch {
        return // offline blip: keep what is shown; the next broadcast retries
      }
      if (!live || mine !== seq) return
      setFavs(favorites)
      const symbols = favorites.filter((f) => f.kind === 'symbol').map((f) => f.key)
      if (symbols.length === 0) {
        setQuotes({})
        return
      }
      try {
        // /api/quotes takes 12 symbols per call; past that the tail came
        // back permanently blank, which reads as "provider down".
        const batches: string[][] = []
        for (let i = 0; i < symbols.length; i += 12) batches.push(symbols.slice(i, i + 12))
        const results = await Promise.all(
          batches.map((b) =>
            api<{ quotes: Record<string, QuoteSnap> }>(
              'GET',
              `/api/quotes?symbols=${encodeURIComponent(b.join(','))}`
            )
          )
        )
        if (live && mine === seq) setQuotes(Object.assign({}, ...results.map((r) => r.quotes)))
      } catch {
        if (live && mine === seq) setQuotes({}) // honest — beats stale numbers
      }
    }
    load()
    const off = window.grindstone.onFavoritesChanged(load)
    return () => {
      live = false
      off()
    }
  }, [])

  const openFavorite = (f: Favorite) => {
    if (f.kind === 'symbol') {
      onNavigate({ name: 'symbol', symbol: f.key })
    } else if (f.kind === 'web') {
      window.grindstone.openUrl(f.key)
    } else {
      // A page favorite's key IS a .gs address — the same classifier the
      // omnibox uses turns it into a route.
      const dest = classify(f.key)
      if (dest.kind === 'route') onNavigate(parseRoute(dest.route))
    }
  }

  /** The tile's identity mark: ticker text, page glyph, or captured image. */
  const glyph = (f: Favorite) => {
    if (f.kind === 'symbol') return <span className="fav-symbol">{f.key}</span>
    if (f.kind === 'page') {
      const PageIcon = pageIcon(f.key.split('?')[0].replace(/\.gs$/, ''))
      return <PageIcon />
    }
    return f.icon ? (
      <img className="fav-favicon" src={f.icon} alt="" />
    ) : (
      <span className="fav-letter">{f.label.charAt(0).toUpperCase()}</span>
    )
  }

  const quoteLine = (sym: string) => {
    const q = quotes[sym]
    const pct = q?.available ? q.change_pct : null
    if (pct == null) return <span className="fav-quote">—</span>
    return (
      <span className={`fav-quote ${pct >= 0 ? 'up' : 'down'}`}>
        {pct >= 0 ? '+' : ''}
        {pct.toFixed(2)}%
      </span>
    )
  }

  const openResult = (r: SearchResult) => {
    if (r.type === 'action' && r.action === 'symbol-opt' && r.symbol) {
      // "SPY Opt" lands on the workstation, not the chart.
      onNavigate({ name: 'opt', symbol: r.symbol })
    } else if ((r.type === 'symbol' || r.type === 'action') && r.symbol) {
      onNavigate({ name: 'symbol', symbol: r.symbol })
    } else if (r.type === 'news' && typeof r.id === 'number') {
      onNavigate({ name: 'article', id: r.id }) // our news: reader view
    } else if ((r.type === 'web' || r.type === 'web-news') && r.url) {
      window.grindstone.openUrl(r.url) // the web: the actual site
    } else if (r.type === 'page') {
      if (r.page === 'help' && r.section) {
        onNavigate({ name: 'help', section: r.section })
        return
      }
      const key = pageRoute(r.page ?? '')
      if (key) onNavigate(parseRoute(key))
    }
  }

  /** Enter with nothing selected, in browser-bar order: a real address
   *  navigates, a platform address (settings.gs, or just "settings")
   *  navigates, a bare ticker opens its page, else search.
   *
   *  This box and the chrome omnibox share classify() so that the same text
   *  cannot mean two different things depending on where it was typed. */
  const submit = async (query: string) => {
    const q = query.trim()
    const dest = classify(q)
    if (dest.kind === 'url') {
      window.grindstone.openUrl(dest.url)
      return
    }
    if (dest.kind === 'route') {
      onNavigate(parseRoute(dest.route))
      return
    }
    try {
      const res = await api<{ results: SearchResult[] }>(
        'GET',
        `/api/search?q=${encodeURIComponent(q)}`
      )
      const first = res.results[0]
      if (
        first?.type === 'symbol' &&
        first.symbol &&
        first.symbol.toUpperCase() === q.toUpperCase()
      ) {
        onNavigate({ name: 'symbol', symbol: first.symbol })
        return
      }
    } catch {
      /* fall through to the results page */
    }
    onNavigate({ name: 'search', query: q })
  }

  return (
    <div className="center-stage">
      <Logo size={84} />
      <Omnibox onOpen={openResult} onSubmit={submit} autoFocus />
      {favs === null ? null : favs.length === 0 ? (
        <div className="fav-empty">
          No favorites yet — open any page and click the ☆ at the end of the
          address bar.
        </div>
      ) : (
        <div className="favorites">
          {favs.map((f) => (
            <div
              key={f.id}
              className="fav-tile"
              title={f.kind === 'web' ? f.key : f.label}
              onClick={() => openFavorite(f)}
            >
              {glyph(f)}
              {f.kind === 'symbol' ? quoteLine(f.key) : null}
              <span>{f.label}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
