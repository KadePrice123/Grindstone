/**
 * One tab's content: in-tab navigation with real history (the strip's Back
 * button drives it through main), plus the persistent user chip that follows
 * you across every page.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import type { Route } from '../App'
import { parseRoute, routeKey } from '../App'
import { setAuthExpiredHandler } from '../api'
import { gsAddress } from '../urls'
import { Logo } from '../components/Logo'
import { UserChip } from '../components/UserChip'
import { Accounts } from '../pages/Accounts'
import { ArticlePage } from '../pages/ArticlePage'
import { DataPage } from '../pages/DataPage'
import { ChartsPage } from '../pages/ChartsPage'
import { HelpPage } from '../pages/HelpPage'
import { Idle } from '../pages/Idle'
import { NewsPage } from '../pages/NewsPage'
import { SearchPage } from '../pages/SearchPage'
import { SettingsPage } from '../pages/SettingsPage'
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
    case 'settings':
      return { title: 'Settings', icon: 'settings' }
    case 'news':
      return { title: 'News', icon: 'news' }
    case 'charts':
      return { title: 'Charts', icon: 'chart' }
    case 'help':
      return { title: 'Help', icon: 'page' }
    case 'article':
      return { title: 'Article', icon: 'news' }
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
    const arg =
      route.name === 'symbol'
        ? route.symbol
        : route.name === 'search'
          ? route.query
          : route.name === 'article'
            ? String(route.id ?? '')
            : route.name === 'help'
              ? route.section
              : undefined
    window.grindstone.setTabMeta(m.title, m.icon, stack.length - 1,
                                 gsAddress(route.name, arg))
  }, [route, stack.length])

  useEffect(() => {
    const off = window.grindstone.onNav((what) => {
      if (what === 'back') setStack((s) => (s.length > 1 ? s.slice(0, -1) : s))
      else setStack([{ name: 'idle' }])
    })
    return off
  }, [])

  // The omnibox can send this tab to any platform route.
  useEffect(() => {
    const off = window.grindstone.onRoute((key) => navigate(parseRoute(key)))
    return off
  }, [navigate])

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
        return <Accounts />
      case 'data':
        return <DataPage />
      case 'symbol':
        return <SymbolPage symbol={route.symbol} onNavigate={navigate} />
      case 'search':
        return <SearchPage query={route.query} onNavigate={navigate} />
      case 'settings':
        return <SettingsPage />
      case 'news':
        return <NewsPage onNavigate={navigate} />
      case 'charts':
        return <ChartsPage />
      case 'help':
        return <HelpPage section={route.section} />
      case 'article':
        return <ArticlePage id={route.id} url={route.url} onNavigate={navigate} />
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
