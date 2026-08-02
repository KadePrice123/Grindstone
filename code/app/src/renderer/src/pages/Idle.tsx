import { useState, type JSX } from 'react'
import { api } from '../api'
import { Logo } from '../components/Logo'
import { AccountsIcon, AiIcon, ApisIcon, PositionsIcon } from '../components/icons'
import type { Route } from '../App'

const FAVORITES: { key: string; label: string; icon: () => JSX.Element; route?: Route }[] = [
  { key: 'accounts', label: 'Accounts', icon: AccountsIcon, route: 'accounts' },
  { key: 'apis', label: 'APIs', icon: ApisIcon },
  { key: 'ai', label: 'AI', icon: AiIcon },
  { key: 'positions', label: 'Positions', icon: PositionsIcon },
]

export function Idle({
  username,
  onNavigate,
  onLocked,
}: {
  username: string
  onNavigate: (r: Route) => void
  onLocked: () => void
}) {
  const [menuOpen, setMenuOpen] = useState(false)
  const [query, setQuery] = useState('')

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

  const onSearch = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key !== 'Enter') return
    const q = query.trim().toLowerCase()
    if (!q) return
    // M1: route the four known pages; real omnibox lands in M4.
    const hit = FAVORITES.find((f) => f.label.toLowerCase().startsWith(q))
    if (hit?.route) onNavigate(hit.route)
  }

  return (
    <div className="center-stage" onClick={() => menuOpen && setMenuOpen(false)}>
      <Logo size={84} />
      <input
        className="omnibox"
        placeholder="Search tickers, news, pages…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={onSearch}
        autoFocus
        spellCheck={false}
      />
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
          title={username}
          onClick={(e) => {
            e.stopPropagation()
            setMenuOpen((v) => !v)
          }}
        >
          {username.slice(0, 1).toUpperCase()}
        </button>
      </div>
      {menuOpen ? (
        <div className="user-menu" onClick={(e) => e.stopPropagation()}>
          <div className="subtle" style={{ padding: '6px 12px' }}>
            {username}
          </div>
          <button onClick={lock}>Lock</button>
          <button onClick={signOut}>Sign out</button>
        </div>
      ) : null}
    </div>
  )
}
