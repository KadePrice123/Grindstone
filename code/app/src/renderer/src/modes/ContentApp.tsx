/**
 * One tab's content: in-tab navigation with real history (the strip's Back
 * button drives it through main), plus the persistent user chip that follows
 * you across every page.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import type { Route } from '../App'
import { routeKey } from '../App'
import { setAuthExpiredHandler } from '../api'
import { Logo } from '../components/Logo'
import { UserChip } from '../components/UserChip'
import { Accounts } from '../pages/Accounts'
import { DataPage } from '../pages/DataPage'
import { Idle } from '../pages/Idle'
import { SearchPage } from '../pages/SearchPage'
import { SymbolPage } from '../pages/SymbolPage'

function meta(route: Route): { title: string; icon: string } {
  switch (route.name) {
    case 'accounts':
      return { title: 'Accounts', icon: 'accounts' }
    case 'data':
      return { title: 'Data management', icon: 'data' }
    case 'symbol':
      return { title: route.symbol, icon: 'chart' }
    case 'search':
      return { title: route.query, icon: 'search' }
    default:
      return { title: 'New tab', icon: 'home' }
  }
}

export function ContentApp({ initial }: { initial: Route }) {
  const [stack, setStack] = useState<Route[]>([initial])
  const [locked, setLocked] = useState(false)
  const route = stack[stack.length - 1]
  const stackRef = useRef(stack)
  stackRef.current = stack

  const navigate = useCallback((r: Route) => setStack((s) => [...s, r]), [])

  useEffect(() => {
    const m = meta(route)
    window.grindstone.setTabMeta(m.title, m.icon, stack.length - 1)
  }, [route, stack.length])

  useEffect(() => {
    const off = window.grindstone.onNav((what) => {
      if (what === 'back') setStack((s) => (s.length > 1 ? s.slice(0, -1) : s))
      else setStack([{ name: 'idle' }])
    })
    return off
  }, [])

  useEffect(() => {
    setAuthExpiredHandler(() => setLocked(true))
    return () => setAuthExpiredHandler(null)
  }, [])

  if (locked) {
    return (
      <div className="center-stage">
        <Logo size={64} />
        <div className="dim">Locked — unlock in the main window</div>
      </div>
    )
  }

  const body = (() => {
    switch (route.name) {
      case 'accounts':
        return <Accounts onBack={() => setStack((s) => s.slice(0, -1))} />
      case 'data':
        return <DataPage onBack={() => setStack((s) => s.slice(0, -1))} />
      case 'symbol':
        return <SymbolPage symbol={route.symbol} />
      case 'search':
        return <SearchPage query={route.query} onNavigate={navigate} />
      default:
        return <Idle onNavigate={navigate} onLocked={() => setLocked(true)} />
    }
  })()

  return (
    <>
      {body}
      <UserChip onNavigate={navigate} onLocked={() => setLocked(true)} />
    </>
  )
}

/** Content pages can ask for a route in a new tab (ctrl/middle-click). */
export function openInNewTab(r: Route): void {
  window.grindstone.openTab(routeKey(r))
}
