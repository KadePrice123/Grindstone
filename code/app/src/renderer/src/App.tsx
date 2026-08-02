/**
 * Mode dispatcher. One renderer bundle serves four view types, chosen by
 * the ?mode= query the main process loads each view with:
 *   auth    — full-window lock screen (owns the whole window while locked)
 *   chrome  — the tab strip (top TABBAR_H px of an unlocked window)
 *   content — one tab's page, starting at ?route=...
 *   wheel   — the transparent gesture-wheel overlay (attached on demand)
 */
import { TabStrip } from './components/TabStrip'
import { AuthShell } from './modes/AuthShell'
import { ContentApp } from './modes/ContentApp'
import { SplitDivider } from './modes/SplitDivider'
import { WheelOverlay } from './modes/WheelOverlay'
import { installWheelEvents } from './wheelEvents'

export type Route =
  | { name: 'idle' }
  | { name: 'accounts' }
  | { name: 'data' }
  | { name: 'symbol'; symbol: string }
  | { name: 'search'; query: string }
  | { name: 'settings' }
  | { name: 'news' }
  | { name: 'charts' }
  | { name: 'help'; section?: string }
  | { name: 'article'; id?: number; url?: string }

export function parseRoute(raw: string | null): Route {
  if (!raw) return { name: 'idle' }
  if (raw.startsWith('symbol:')) return { name: 'symbol', symbol: raw.slice(7).toUpperCase() }
  if (raw.startsWith('search:')) return { name: 'search', query: raw.slice(7) }
  if (raw.startsWith('article:')) {
    const rest = raw.slice(8)
    return /^\d+$/.test(rest) ? { name: 'article', id: Number(rest) } : { name: 'article', url: rest }
  }
  if (raw.startsWith('help')) {
    const section = raw.startsWith('help:') ? raw.slice(5) : undefined
    return { name: 'help', ...(section ? { section } : {}) }
  }
  if (raw === 'accounts' || raw === 'data' || raw === 'settings' || raw === 'news'
      || raw === 'charts') {
    return { name: raw }
  }
  return { name: 'idle' }
}

export function routeKey(r: Route): string {
  if (r.name === 'symbol') return `symbol:${r.symbol}`
  if (r.name === 'search') return `search:${r.query}`
  if (r.name === 'article') return `article:${r.id ?? r.url ?? ''}`
  if (r.name === 'help') return r.section ? `help:${r.section}` : 'help'
  return r.name
}

const params = new URLSearchParams(window.location.search)
const mode = params.get('mode') ?? 'auth'

// The gesture wheel listens in the chrome and every content page (browser
// tabs forward from their own preload; auth has none — no wheel while
// locked; the overlay must not re-forward its own clicks as spawns).
if (mode === 'chrome' || mode === 'content') installWheelEvents()
if (mode === 'wheel') document.documentElement.classList.add('wheel-mode')

export default function App() {
  if (mode === 'chrome') return <TabStrip />
  if (mode === 'content') return <ContentApp initial={parseRoute(params.get('route'))} />
  if (mode === 'wheel') return <WheelOverlay />
  if (mode === 'divider') return <SplitDivider />
  return <AuthShell />
}
