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
const PAGES = ['home', 'accounts', 'data', 'settings', 'search', 'article', 'news',
  'charts', 'help', 'backtest', 'opt', 'notepad', 'insure'] as const

/**
 * Bare words that are addresses in their own right. A browser bar navigates
 * on a known keyword without making you type the suffix, so "settings" lands
 * on Settings just like "settings.gs" does.
 *
 * ONLY pages that actually exist: routing "ai" here would dead-end on a page
 * we have not built, and AI is also a real ticker — the ambiguity resolves
 * itself once there is somewhere to go.
 */
const PAGE_ROUTES: Record<string, string> = {
  home: 'idle',
  accounts: 'accounts',
  data: 'data',
  settings: 'settings',
  news: 'news',
  charts: 'charts',
  help: 'help',
  backtest: 'backtest',
  notepad: 'notepad',
  insure: 'insure',
}

const BARE: Record<string, string> = {
  ...PAGE_ROUTES,
  start: 'idle',
  account: 'accounts',
  brokers: 'accounts',
  setting: 'settings',
  preferences: 'settings',
  headlines: 'news',
  guide: 'help',
  manual: 'help',
  docs: 'help',
  backtests: 'backtest',
  backtesting: 'backtest',
  notes: 'notepad',
  pad: 'notepad',
  clipboard: 'notepad',
  // The Insure scanner's own vocabulary. "puts" is a word a put-seller
  // actually types; it is not a ticker (no US listing), so the ambiguity
  // that keeps "ai" out of this table does not apply.
  insurance: 'insure',
  puts: 'insure',
}

/** The route key for a page from the backend's registry, or null if that
 *  page is announced but not built yet. */
export function pageRoute(page: string): string | null {
  return PAGE_ROUTES[page.toLowerCase()] ?? null
}

export interface GsTarget {
  page: string
  query: string
  /** A contract (OCC symbol), when the address names one: opt.gs?s=SPY&occ=…
   *  Separate from `query` because the Opt page is a SYMBOL plus optionally
   *  one contract, and one field cannot carry both. */
  occ?: string
}

export type Destination =
  | { kind: 'url'; url: string }
  | { kind: 'route'; route: string }
  | { kind: 'search'; query: string }

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
  return {
    page: name,
    query: params.get('q') ?? params.get('id') ?? params.get('s') ?? '',
    // The CONTRACT, carried separately from the page's argument. The Opt
    // page is "SPY Opt" plus optionally one contract whose history it opens
    // on; folding that into `query` would make the symbol and the contract
    // the same field and lose one of them.
    occ: params.get('occ') ?? undefined,
  }
}

/** A .gs target as a route key (App.tsx's parseRoute vocabulary). */
export function gsRoute(gs: GsTarget): string {
  if (!isKnownPage(gs.page)) return `symbol:${gs.page.toUpperCase()}` // anything else .gs is a ticker
  switch (gs.page) {
    case 'home':
      return 'idle'
    case 'search':
      return `search:${gs.query}`
    case 'article':
      return `article:${gs.query}`
    // opt.gs?s=SPY. A bare `opt.gs` has no symbol to analyse, so it falls back
    // to the multi-chart page rather than opening an empty workstation.
    case 'opt':
      // opt:SPY, or opt:SPY:<occ> when a specific contract should be loaded
      // — grabbing a contract and asking for its history is the whole point
      // of predicting this page, and a route with no slot for the contract
      // silently dropped it at the boundary.
      return gs.query
        ? `opt:${gs.query.toUpperCase()}${gs.occ ? `:${gs.occ.toUpperCase()}` : ''}`
        : 'charts'
    case 'help':
      return gs.query ? `help:${gs.query}` : 'help'
    default:
      return gs.page
  }
}

/**
 * What typing this into an address bar means. One decision procedure, shared
 * by the chrome omnibox and the home-page search box, so the same text can
 * never mean two different things depending on where you typed it.
 */
export function classify(input: string): Destination {
  const text = input.trim()
  if (!text) return { kind: 'search', query: '' }

  const url = asUrl(text)
  if (url) return { kind: 'url', url }

  const gs = asGs(text)
  if (gs) return { kind: 'route', route: gsRoute(gs) }

  const bare = BARE[text.toLowerCase()]
  if (bare) return { kind: 'route', route: bare }

  return { kind: 'search', query: text }
}

/** The address shown for a platform page — the inverse of asGs(). */
export function gsAddress(routeName: string, arg?: string, arg2?: string): string {
  switch (routeName) {
    case 'idle':
      return 'home.gs'
    // Every Opt tab used to report its address as a bare 'opt.gs', so the
    // address bar said the same thing for SPY Opt and QQQ Opt, and nothing
    // could tell whether a given Opt page was ALREADY OPEN.
    case 'opt':
      return `opt.gs?s=${(arg ?? '').toUpperCase()}` +
             (arg2 ? `&occ=${arg2.toUpperCase()}` : '')
    case 'symbol':
      return `${(arg ?? '').toLowerCase()}.gs`
    case 'search':
      return `search.gs?q=${encodeURIComponent(arg ?? '')}`
    case 'article':
      return `article.gs?id=${arg ?? ''}`
    case 'help':
      return arg ? `help.gs?s=${encodeURIComponent(arg)}` : 'help.gs'
    default:
      return `${routeName}.gs`
  }
}

export function isKnownPage(name: string): boolean {
  return (PAGES as readonly string[]).includes(name)
}
