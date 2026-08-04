/**
 * Favorites: the shared contract between the omnibox star (TabStrip), the
 * home grid (Idle), the wheel picker (GesturesPanel), and the launcher.
 *
 * One classifier decides what starring the current location MEANS, from the
 * same two facts the strip already carries (activeKind + activeUrl) — so the
 * star, the grid tile, and the wheel segment can never disagree about
 * identity. Mirrors backend/favorites.py's vocabulary exactly.
 */
import { asGs, isKnownPage } from './urls'

export interface Favorite {
  id: number
  kind: 'symbol' | 'page' | 'web'
  key: string // SPY | backtest.gs / help.gs?s=drawing | https://…
  label: string
  icon: string // web: data URI of the captured tab image; else ''
  pos: number
}

export interface FavoriteIdentity {
  kind: Favorite['kind']
  key: string
}

/**
 * What the current tab IS, in favorites vocabulary — or null when it has no
 * starrable identity (a tab still loading, an empty address).
 *
 * App tabs address the platform (`spy.gs`, `backtest.gs`, `help.gs?s=…`):
 * an unknown page name is a ticker by the same rule gsRoute() uses. Browser
 * tabs are the web, keyed by their canonical URL.
 */
export function favoriteIdentity(
  kind: 'app' | 'browser' | null,
  url: string
): FavoriteIdentity | null {
  const text = (url ?? '').trim()
  if (!kind || !text) return null
  if (kind === 'browser') {
    if (!/^https?:\/\//i.test(text)) return null
    return { kind: 'web', key: text }
  }
  const gs = asGs(text)
  if (!gs) return null
  if (!isKnownPage(gs.page)) return { kind: 'symbol', key: gs.page.toUpperCase() }
  return { kind: 'page', key: text.toLowerCase() }
}

/**
 * The short name under the tile ("based on whatever the page actually is"):
 * a symbol IS its label; everything else takes the live tab title, cut to
 * the backend's 24-char cap, with an honest fallback when the title is
 * still empty (page name / hostname).
 */
export function favoriteLabel(id: FavoriteIdentity, tabTitle: string): string {
  if (id.kind === 'symbol') return id.key
  const title = (tabTitle ?? '').trim()
  if (title) return title.slice(0, 24)
  if (id.kind === 'page') {
    const name = id.key.split('?')[0].replace(/\.gs$/, '')
    return name.charAt(0).toUpperCase() + name.slice(1)
  }
  try {
    return new URL(id.key).hostname.replace(/^www\./, '').slice(0, 24)
  } catch {
    return id.key.slice(0, 24)
  }
}

/** Two favorites (or a favorite and a live identity) are the same thing. */
export function sameFavorite(a: FavoriteIdentity, b: FavoriteIdentity): boolean {
  if (a.kind !== b.kind) return false
  if (a.kind === 'symbol') return a.key.toUpperCase() === b.key.toUpperCase()
  return a.key === b.key
}
