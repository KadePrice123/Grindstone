import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, setAuthExpiredHandler } from './api'
import { Logo } from './components/Logo'
import { AuthGate } from './pages/AuthGate'
import { Idle } from './pages/Idle'
import { Accounts } from './pages/Accounts'

export type Route = 'idle' | 'accounts'

type Phase =
  | { kind: 'booting' }
  | { kind: 'auth'; initialized: boolean }
  | { kind: 'app'; username: string; route: Route }
  | { kind: 'backend-down'; detail?: string }

/**
 * Minimum time the boot splash stays on screen. The sidecar answers in ~500ms,
 * so without a floor the splash flashes past in tens of milliseconds and the
 * mark never visibly spins. This is a floor, not an added delay: a slow boot
 * waits zero extra.
 */
const MIN_SPLASH_MS = 900

export default function App() {
  const [phase, setPhase] = useState<Phase>({ kind: 'booting' })
  const mountedAt = useRef(performance.now())

  // The sidecar-status subscription is registered once; without a ref it would
  // close over the phase from first render and never see the current one.
  const phaseRef = useRef(phase)
  phaseRef.current = phase

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

  // Any 401 from a user route drops us back to the lock screen.
  useEffect(() => {
    setAuthExpiredHandler(() => {
      if (phaseRef.current.kind === 'app') toAuth()
    })
    return () => setAuthExpiredHandler(null)
  }, [toAuth])

  useEffect(() => {
    let cancelled = false
    const probe = async () => {
      for (let tries = 0; tries < 40 && !cancelled; tries += 1) {
        try {
          await api('GET', '/api/health')
          const shown = performance.now() - mountedAt.current
          if (shown < MIN_SPLASH_MS) {
            await new Promise((r) => setTimeout(r, MIN_SPLASH_MS - shown))
          }
          if (!cancelled) await toAuth()
          return
        } catch {
          await new Promise((r) => setTimeout(r, 500))
        }
      }
      if (!cancelled) setPhase({ kind: 'backend-down', detail: 'backend never became healthy' })
    }
    probe()

    const off = window.grindstone.onSidecarStatus((s) => {
      if (s.status === 'crashed') {
        setPhase({ kind: 'backend-down', detail: s.detail })
      } else if (s.status === 'ready') {
        // Sessions die with the sidecar (main clears its token on crash), so
        // the auth screen is the correct destination after any restart.
        if (phaseRef.current.kind !== 'auth') toAuth()
      }
    })
    return () => {
      cancelled = true
      off()
    }
  }, [toAuth])

  switch (phase.kind) {
    case 'booting':
      return <Splash text="Starting backend…" spin />
    case 'backend-down':
      // Deliberately static: this state can persist, and a permanently
      // animating logo costs GPU time for as long as it is on screen.
      return <Splash text="Backend is not running." detail={phase.detail} error />
    case 'auth':
      return (
        <AuthGate
          initialized={phase.initialized}
          onSignedIn={(username) => setPhase({ kind: 'app', username, route: 'idle' })}
          onRecheck={toAuth}
        />
      )
    case 'app': {
      const nav = (route: Route) => setPhase({ ...phase, route })
      if (phase.route === 'accounts') return <Accounts onBack={() => nav('idle')} />
      return <Idle username={phase.username} onNavigate={nav} onLocked={toAuth} />
    }
  }
}

function Splash({
  text,
  detail,
  error,
  spin,
}: {
  text: string
  detail?: string
  error?: boolean
  spin?: boolean
}) {
  return (
    <div className="center-stage">
      <Logo size={72} spin={spin} />
      <div className={error ? 'error-text' : 'dim'}>{text}</div>
      {detail ? <div className="error-text subtle">{detail}</div> : null}
    </div>
  )
}
