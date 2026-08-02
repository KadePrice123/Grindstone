import { useCallback, useEffect, useState } from 'react'
import { api, ApiError } from './api'
import { AuthGate } from './pages/AuthGate'
import { Idle } from './pages/Idle'
import { Accounts } from './pages/Accounts'

export type Route = 'idle' | 'accounts'

type Phase =
  | { kind: 'booting'; detail?: string }
  | { kind: 'auth'; initialized: boolean }
  | { kind: 'app'; username: string; route: Route }
  | { kind: 'backend-down'; detail?: string }

export default function App() {
  const [phase, setPhase] = useState<Phase>({ kind: 'booting' })

  const toAuth = useCallback(async () => {
    try {
      const s = await api<{ initialized: boolean }>('GET', '/api/auth/status')
      setPhase({ kind: 'auth', initialized: s.initialized })
    } catch (e) {
      setPhase({
        kind: 'backend-down',
        detail: e instanceof ApiError ? e.message : String(e),
      })
    }
  }, [])

  useEffect(() => {
    let tries = 0
    let cancelled = false
    const probe = async () => {
      while (!cancelled && tries < 40) {
        tries += 1
        try {
          await api('GET', '/api/health')
          await toAuth()
          return
        } catch {
          await new Promise((r) => setTimeout(r, 500))
        }
      }
      if (!cancelled) setPhase({ kind: 'backend-down', detail: 'backend never became healthy' })
    }
    probe()
    const off = window.grindstone.onSidecarStatus((s) => {
      if (s.status === 'crashed') setPhase({ kind: 'backend-down', detail: s.detail })
      if (s.status === 'ready' && phase.kind === 'backend-down') toAuth()
    })
    return () => {
      cancelled = true
      off()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const onSignedIn = (username: string) => setPhase({ kind: 'app', username, route: 'idle' })
  const onLocked = () => toAuth()

  switch (phase.kind) {
    case 'booting':
      return <Splash text="Starting backend…" />
    case 'backend-down':
      return <Splash text="Backend is not running." detail={phase.detail} error />
    case 'auth':
      return <AuthGate initialized={phase.initialized} onSignedIn={onSignedIn} />
    case 'app': {
      const nav = (route: Route) => setPhase({ ...phase, route })
      if (phase.route === 'accounts') {
        return <Accounts onBack={() => nav('idle')} />
      }
      return <Idle username={phase.username} onNavigate={nav} onLocked={onLocked} />
    }
  }
}

import { Logo } from './components/Logo'

function Splash({ text, detail, error }: { text: string; detail?: string; error?: boolean }) {
  return (
    <div className="center-stage">
      <Logo size={72} />
      <div className={error ? 'error-text' : 'dim'}>{text}</div>
      {detail ? <div className="error-text subtle">{detail}</div> : null}
    </div>
  )
}
