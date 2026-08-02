import { api, SearchResult } from '../api'
import { Logo } from '../components/Logo'
import { Omnibox } from '../components/Omnibox'
import { AccountsIcon, AiIcon, ApisIcon, DataIcon, PositionsIcon } from '../components/icons'
import type { Route } from '../App'

const FAVORITES: {
  key: string
  label: string
  icon: () => React.JSX.Element
  route?: Route
}[] = [
  { key: 'accounts', label: 'Accounts', icon: AccountsIcon, route: { name: 'accounts' } },
  { key: 'apis', label: 'APIs', icon: ApisIcon },
  { key: 'ai', label: 'AI', icon: AiIcon },
  { key: 'positions', label: 'Positions', icon: PositionsIcon },
  { key: 'data', label: 'Data', icon: DataIcon, route: { name: 'data' } },
]

export function Idle({
  onNavigate,
}: {
  onNavigate: (r: Route) => void
  onLocked: () => void
}) {
  const openResult = (r: SearchResult) => {
    if ((r.type === 'symbol' || r.type === 'action') && r.symbol) {
      onNavigate({ name: 'symbol', symbol: r.symbol })
    } else if (r.type === 'news' && r.url) {
      window.grindstone.openUrl(r.url) // reads in-app, not in the OS browser
    } else if (r.type === 'page') {
      if (r.page === 'accounts') onNavigate({ name: 'accounts' })
      else if (r.page === 'data') onNavigate({ name: 'data' })
    }
  }

  /** Enter with nothing selected: a bare ticker behaves like a URL and goes
   *  straight to its page; anything else lands on the results page. */
  const submit = async (query: string) => {
    const q = query.trim()
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
      <div className="favorites">
        {FAVORITES.map((f) => (
          <div
            key={f.key}
            className="fav-tile"
            style={f.route ? undefined : { opacity: 0.45 }}
            title={f.route ? f.label : `${f.label} — arrives in a later milestone`}
            onClick={() => f.route && onNavigate(f.route)}
          >
            <f.icon />
            <span>{f.label}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
