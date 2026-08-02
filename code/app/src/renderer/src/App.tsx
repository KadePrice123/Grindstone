/**
 * Mode dispatcher. One renderer bundle serves three view types, chosen by
 * the ?mode= query the main process loads each view with:
 *   auth    — full-window lock screen (owns the whole window while locked)
 *   chrome  — the tab strip (top TABBAR_H px of an unlocked window)
 *   content — one tab's page, starting at ?route=...
 */
import { TabStrip } from './components/TabStrip'
import { AuthShell } from './modes/AuthShell'
import { ContentApp } from './modes/ContentApp'

export type Route =
  | { name: 'idle' }
  | { name: 'accounts' }
  | { name: 'data' }
  | { name: 'symbol'; symbol: string }

export function parseRoute(raw: string | null): Route {
  if (!raw) return { name: 'idle' }
  if (raw.startsWith('symbol:')) return { name: 'symbol', symbol: raw.slice(7).toUpperCase() }
  if (raw === 'accounts' || raw === 'data') return { name: raw }
  return { name: 'idle' }
}

export function routeKey(r: Route): string {
  return r.name === 'symbol' ? `symbol:${r.symbol}` : r.name
}

export default function App() {
  const params = new URLSearchParams(window.location.search)
  const mode = params.get('mode') ?? 'auth'
  if (mode === 'chrome') return <TabStrip />
  if (mode === 'content') return <ContentApp initial={parseRoute(params.get('route'))} />
  return <AuthShell />
}
