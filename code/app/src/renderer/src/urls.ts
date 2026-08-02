/**
 * Address handling for the omnibox.
 *
 * The bar is always present, exactly like a browser's, and it addresses two
 * worlds:
 *   the web      google.com, https://…            -> browser tab
 *   the platform accounts.gs, spy.gs, search.gs   -> in-app page
 *
 * The `.gs` suffix is the marker for platform pages: it makes internal
 * destinations first-class addresses you can type, bookmark and share
 * internally, without ever colliding with a real TLD.
 *
 * Detection is deliberately conservative — a wrong search costs a click, a
 * wrong navigation loses the query — so a bare word, a ticker or anything
 * with a space stays a search.
 */

const TLD =
  /\.(com|net|org|io|co|ai|dev|app|gov|edu|news|finance|xyz|me|us|uk|ca|de|fr|jp|au|info|biz|tv|cloud|so|sh|to|ly|[a-z]{2})$/i

/** Platform pages that are addressable by name. */
const PAGES = ['home', 'accounts', 'data', 'settings', 'search', 'article'] as const

export interface GsTarget {
  page: string
  query: string
}

export function asUrl(input: string): string | null {
  const text = input.trim()
  if (!text || /\s/.test(text)) return null

  if (/^https?:\/\//i.test(text)) {
    try {
      const u = new URL(text)
      return u.hostname ? u.toString() : null
    } catch {
      return null
    }
  }
  if (/^(file|javascript|data|about|chrome|blob):/i.test(text)) return null
  if (/\.gs(\/|\?|$)/i.test(text)) return null // ours, not the web's

  if (/^localhost(:\d+)?(\/|$)/i.test(text)) return `http://${text}`

  const host = text.split(/[/?#]/)[0]
  if (!host.includes('.') || host.startsWith('.') || host.endsWith('.')) return null
  if (!TLD.test(host)) return null
  if (/^[\d.]+$/.test(host) && !/^(\d{1,3}\.){3}\d{1,3}$/.test(host)) return null

  return `https://${text}`
}

/** "accounts.gs" / "spy.gs" / "search.gs?q=oil" -> a platform destination. */
export function asGs(input: string): GsTarget | null {
  const text = input.trim()
  if (!/\.gs(\?|$)/i.test(text.split(/\s/)[0]) || /\s/.test(text)) return null
  const [head, qs = ''] = text.split('?')
  const name = head.replace(/\.gs$/i, '').toLowerCase()
  if (!name) return null
  const params = new URLSearchParams(qs)
  return { page: name, query: params.get('q') ?? '' }
}

/** The address shown for a platform page — the inverse of asGs(). */
export function gsAddress(routeName: string, arg?: string): string {
  switch (routeName) {
    case 'idle':
      return 'home.gs'
    case 'symbol':
      return `${(arg ?? '').toLowerCase()}.gs`
    case 'search':
      return `search.gs?q=${encodeURIComponent(arg ?? '')}`
    case 'article':
      return `article.gs?id=${arg ?? ''}`
    default:
      return `${routeName}.gs`
  }
}

export function isKnownPage(name: string): boolean {
  return (PAGES as readonly string[]).includes(name)
}
