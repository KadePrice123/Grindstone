/**
 * One tab's content. Navigation happens in-tab (Chrome parity); the tab
 * reports its title/icon to main so the strip stays truthful. Auth is
 * window-level: on 401 the main process flips the window to the lock
 * screen — we just render a quiet splash for the moment that takes.
 */
import { useEffect, useState } from 'react'
import type { Route } from '../App'
import { routeKey } from '../App'
import { api, setAuthExpiredHandler } from '../api'
import { Logo } from '../components/Logo'
import { Accounts } from '../pages/Accounts'
import { DataPage } from '../pages/DataPage'
import { Idle } from '../pages/Idle'
import { SymbolPage } from '../pages/SymbolPage'

function meta(route: Route): { title: string; icon: string } {
  switch (route.name) {
    case 'accounts':
      return { title: 'Accounts', icon: 'accounts' }
    case 'data':
      return { title: 'Data management', icon: 'data' }
    case 'symbol':
      return { title: route.symbol, icon: 'chart' }
    default:
      return { title: 'New tab', icon: 'home' }
  }
}

export function ContentApp({ initial }: { initial: Route }) {
  const [route, setRoute] = useState<Route>(initial)
  const [locked, setLocked] = useState(false)

  useEffect(() => {
    const m = meta(route)
    window.grindstone.setTabMeta(m.title, m.icon)
  }, [route])

  useEffect(() => {
    setAuthExpiredHandler(() => setLocked(true))
    return () => setAuthExpiredHandler(null)
  }, [])

  if (locked) {
    return (
      <div className="center-stage">
        <Logo size={64} />
        <div className="dim">Locked</div>
      </div>
    )
  }

  const nav = (r: Route) => setRoute(r)
  const back = () => nav({ name: 'idle' })

  switch (route.name) {
    case 'accounts':
      return <Accounts onBack={back} />
    case 'data':
      return <DataPage onBack={back} />
    case 'symbol':
      return <SymbolPage symbol={route.symbol} onBack={back} />
    default:
      return <Idle onNavigate={nav} onLocked={() => setLocked(true)} />
  }
}

/** Content pages can ask for a route in a new tab (used by ctrl/middle-click). */
export function openInNewTab(r: Route): void {
  window.grindstone.openTab(routeKey(r))
}

// api import kept referenced for the auth handler side effect module load
void api
