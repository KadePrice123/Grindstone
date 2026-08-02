import { useEffect, useState } from 'react'
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
  onLocked,
}: {
  onNavigate: (r: Route) => void
  onLocked: () => void
}) {
  const [menuOpen, setMenuOpen] = useState(false)
  const [username, setUsername] = useState('')

  useEffect(() => {
    api<{ username: string }>('GET', '/api/auth/me')
      .then((r) => setUsername(r.username))
      .catch(() => setUsername(''))
  }, [])

  const openResult = (r: SearchResult) => {
    if (r.type === 'symbol' && r.symbol) {
      onNavigate({ name: 'symbol', symbol: r.symbol })
    } else if (r.type === 'action' && r.action === 'symbol-news' && r.symbol) {
      onNavigate({ name: 'symbol', symbol: r.symbol })
    } else if (r.type === 'news' && r.url) {
      window.open(r.url) // routed to the OS browser by the shell
    } else if (r.type === 'page') {
      if (r.page === 'accounts') onNavigate({ name: 'accounts' })
      else if (r.page === 'data') onNavigate({ name: 'data' })
      // apis / ai / positions / settings pages arrive in later milestones
    }
  }

  const lock = async () => {
    try {
      await api('POST', '/api/auth/lock')
    } finally {
      onLocked()
    }
  }
  const signOut = async () => {
    try {
      await api('POST', '/api/auth/logout')
    } finally {
      onLocked()
    }
  }

  return (
    <div className="center-stage" onClick={() => menuOpen && setMenuOpen(false)}>
      <Logo size={84} />
      <Omnibox onOpen={openResult} autoFocus />
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

      <div className="user-chip">
        <button
          title={username || 'Profile'}
          onClick={(e) => {
            e.stopPropagation()
            setMenuOpen((v) => !v)
          }}
        >
          {(username || '?').slice(0, 1).toUpperCase()}
        </button>
      </div>
      {menuOpen ? (
        <div className="user-menu" onClick={(e) => e.stopPropagation()}>
          <div className="subtle" style={{ padding: '6px 12px' }}>
            {username}
          </div>
          <button onClick={() => onNavigate({ name: 'data' })}>Data management</button>
          <button onClick={lock}>Lock</button>
          <button onClick={signOut}>Sign out</button>
        </div>
      ) : null}
    </div>
  )
}
