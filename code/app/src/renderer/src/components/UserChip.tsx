/**
 * The user avatar, bottom-left of every page (FR-SHELL: "on the very bottom
 * left you have your user logo and clicking it will open a small nav list").
 * It lives in the content view rather than the chrome strip so it sits at
 * the bottom of the window, where Kade asked for it.
 */
import { useEffect, useRef, useState } from 'react'
import type { Route } from '../App'
import { api } from '../api'

export function UserChip({
  onNavigate,
  onLocked,
}: {
  onNavigate: (r: Route) => void
  onLocked: () => void
}) {
  const [open, setOpen] = useState(false)
  const [username, setUsername] = useState('')
  const box = useRef<HTMLDivElement>(null)

  useEffect(() => {
    api<{ username: string }>('GET', '/api/auth/me')
      .then((r) => setUsername(r.username))
      .catch(() => setUsername(''))
  }, [])

  useEffect(() => {
    const away = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', away)
    return () => document.removeEventListener('mousedown', away)
  }, [])

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
    <div className="user-chip" ref={box}>
      {open ? (
        <div className="user-menu">
          <div className="subtle" style={{ padding: '6px 12px' }}>
            {username || 'Signed in'}
          </div>
          <button onClick={() => (setOpen(false), onNavigate({ name: 'accounts' }))}>
            Accounts
          </button>
          <button onClick={() => (setOpen(false), onNavigate({ name: 'data' }))}>
            Data management
          </button>
          <button onClick={() => (setOpen(false), onNavigate({ name: 'settings' }))}>
            Settings
          </button>
          <button onClick={lock}>Lock</button>
          <button onClick={signOut}>Sign out</button>
        </div>
      ) : null}
      <button
        className="user-avatar"
        title={username || 'Profile'}
        onClick={() => setOpen((v) => !v)}
      >
        {(username || '?').slice(0, 1).toUpperCase()}
      </button>
    </div>
  )
}
