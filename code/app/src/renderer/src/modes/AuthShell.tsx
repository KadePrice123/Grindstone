/**
 * The lock screen — full-window while no one is signed in. On successful
 * login the MAIN process (which sees the token pass through its proxy)
 * swaps this window into tab mode; we just show a beat of progress.
 */
import { useCallback, useEffect, useState } from 'react'
import { api, ApiError } from '../api'
import { Logo } from '../components/Logo'
import { AuthGate } from '../pages/AuthGate'

type Phase =
  | { kind: 'booting' }
  | { kind: 'auth'; initialized: boolean }
  | { kind: 'opening' }
  | { kind: 'backend-down'; detail?: string }

export function AuthShell() {
  const [phase, setPhase] = useState<Phase>({ kind: 'booting' })

  const toAuth = useCallback(async () => {
    try {
      const s = await api<{ initialized: boolean }>('GET', '/api/auth/status')
      setPhase({ kind: 'auth', initialized: s.initialized })
    } catch (e) {
      setPhase({ kind: 'backend-down', detail: e instanceof ApiError ? e.message : String(e) })
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    const probe = async () => {
      for (let tries = 0; tries < 40 && !cancelled; tries += 1) {
        try {
          await api('GET', '/api/health')
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
      if (s.status === 'crashed') setPhase({ kind: 'backend-down', detail: s.detail })
      else if (s.status === 'ready') toAuth()
    })
    return () => {
      cancelled = true
      off()
    }
  }, [toAuth])

  switch (phase.kind) {
    case 'booting':
      return <Splash text="Starting backend…" spin />
    case 'opening':
      return <Splash text="Opening…" spin />
    case 'backend-down':
      return <Splash text="Backend is not running." detail={phase.detail} error />
    case 'auth':
      return (
        <AuthGate
          initialized={phase.initialized}
          onSignedIn={() => setPhase({ kind: 'opening' })}
          onRecheck={toAuth}
        />
      )
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
