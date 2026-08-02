/**
 * The catalog of everything a wheel segment can be — the single source the
 * settings picker searches and filters, and the vocabulary the chart pages
 * act on. Kept in ONE place so "add a chart tool" is a one-entry change:
 * catalog entry here + handler in the chart page + (already-generic)
 * backend validation via CHART_TOOLS in backend/wheels.py.
 *
 * Wheels themselves (go-to-wheel segments) are NOT listed here — the picker
 * generates those from the user's live wheels document.
 */
import type { WheelSegment } from './components/WheelFace'

export interface CatalogEntry {
  /** Stable id, used for search/dedup; not persisted. */
  id: string
  category: string
  label: string
  /** Extra words search should hit. */
  keywords: string
  /** What the segment becomes when picked. */
  segment: WheelSegment
}

export const CATEGORIES = [
  'Navigation',
  'Wheels',
  'Chart · drawing',
  'Chart · measure',
  'Chart · indicators',
  'Chart · view',
  'Tools',
  'Tickers',
  'Other',
] as const

export const CATALOG: CatalogEntry[] = [
  // ---- Navigation ---------------------------------------------------------
  { id: 'nav:idle', category: 'Navigation', label: 'Home',
    keywords: 'home start landing', segment: { type: 'nav', route: 'idle', label: 'Home' } },
  { id: 'nav:charts', category: 'Navigation', label: 'Charts (multi-symbol)',
    keywords: 'chart compare multi', segment: { type: 'nav', route: 'charts', label: 'Charts' } },
  { id: 'nav:accounts', category: 'Navigation', label: 'Accounts',
    keywords: 'brokers keys', segment: { type: 'nav', route: 'accounts', label: 'Accounts' } },
  { id: 'nav:news', category: 'Navigation', label: 'News',
    keywords: 'headlines feed articles', segment: { type: 'nav', route: 'news', label: 'News' } },
  { id: 'nav:data', category: 'Navigation', label: 'Data management',
    keywords: 'recording storage jobs', segment: { type: 'nav', route: 'data', label: 'Data' } },
  { id: 'nav:settings', category: 'Navigation', label: 'Settings',
    keywords: 'preferences config', segment: { type: 'nav', route: 'settings', label: 'Settings' } },

  // ---- Chart · drawing ----------------------------------------------------
  { id: 'chart:pointer', category: 'Chart · drawing', label: 'Pointer',
    keywords: 'select none cursor stop drawing',
    segment: { type: 'chart', tool: 'pointer', label: 'Pointer' } },
  { id: 'chart:trend', category: 'Chart · drawing', label: 'Line (any angle)',
    keywords: 'draw trend free diagonal support resistance',
    segment: { type: 'chart', tool: 'trend', label: 'Line' } },
  { id: 'chart:hline', category: 'Chart · drawing', label: 'Horizontal line',
    keywords: 'draw level price support resistance',
    segment: { type: 'chart', tool: 'hline', label: 'H-line' } },
  { id: 'chart:vline', category: 'Chart · drawing', label: 'Vertical line',
    keywords: 'draw date time event marker',
    segment: { type: 'chart', tool: 'vline', label: 'V-line' } },
  { id: 'chart:circle', category: 'Chart · drawing', label: 'Circle',
    keywords: 'draw ellipse zone area highlight',
    segment: { type: 'chart', tool: 'circle', label: 'Circle' } },
  { id: 'chart:select', category: 'Chart · drawing', label: 'Select',
    keywords: 'pick multi edit adjust move choose',
    segment: { type: 'chart', tool: 'select', label: 'Select' } },
  { id: 'chart:delete', category: 'Chart · drawing', label: 'Delete',
    keywords: 'remove erase selected lines',
    segment: { type: 'chart', tool: 'delete', label: 'Delete' } },
  { id: 'chart:trim', category: 'Chart · drawing', label: 'Trim',
    keywords: 'cut intersection vertex solidworks shorten',
    segment: { type: 'chart', tool: 'trim', label: 'Trim' } },
  { id: 'chart:clear', category: 'Chart · drawing', label: 'Clear drawings',
    keywords: 'erase delete remove all',
    segment: { type: 'chart', tool: 'clear', label: 'Clear' } },

  // ---- Chart · measure ----------------------------------------------------
  { id: 'chart:measure', category: 'Chart · measure', label: 'Measure',
    keywords: 'ruler distance price date bars between candle line',
    segment: { type: 'chart', tool: 'measure', label: 'Measure' } },
  { id: 'chart:inspect', category: 'Chart · measure', label: 'Inspect candle',
    keywords: 'ohlc size body volume detail readout',
    segment: { type: 'chart', tool: 'inspect', label: 'Inspect' } },
  { id: 'chart:clearmeasure', category: 'Chart · measure', label: 'Clear measures',
    keywords: 'erase remove ruler annotations',
    segment: { type: 'chart', tool: 'clearmeasure', label: 'Clear measures' } },

  // ---- Chart · indicators -------------------------------------------------
  { id: 'chart:ind:vol', category: 'Chart · indicators', label: 'Volume',
    keywords: 'indicator toggle', segment: { type: 'chart', tool: 'ind:vol', label: 'Volume' } },
  { id: 'chart:ind:sma20', category: 'Chart · indicators', label: 'SMA 20',
    keywords: 'moving average indicator',
    segment: { type: 'chart', tool: 'ind:sma20', label: 'SMA 20' } },
  { id: 'chart:ind:sma50', category: 'Chart · indicators', label: 'SMA 50',
    keywords: 'moving average indicator',
    segment: { type: 'chart', tool: 'ind:sma50', label: 'SMA 50' } },
  { id: 'chart:ind:ema20', category: 'Chart · indicators', label: 'EMA 20',
    keywords: 'exponential moving average indicator',
    segment: { type: 'chart', tool: 'ind:ema20', label: 'EMA 20' } },
  { id: 'chart:ind:rsi14', category: 'Chart · indicators', label: 'RSI 14',
    keywords: 'relative strength indicator oscillator',
    segment: { type: 'chart', tool: 'ind:rsi14', label: 'RSI 14' } },

  // ---- Chart · view -------------------------------------------------------
  { id: 'chart:normalize', category: 'Chart · view', label: 'Normalize (%)',
    keywords: 'percent compare rebase relative',
    segment: { type: 'chart', tool: 'normalize', label: '% mode' } },
  { id: 'chart:vis:draw', category: 'Chart · view', label: 'Toggle drawings visible',
    keywords: 'hide show visibility lines annotations',
    segment: { type: 'chart', tool: 'vis:draw', label: 'Drawings' } },
  { id: 'chart:vis:ind', category: 'Chart · view', label: 'Toggle indicators visible',
    keywords: 'hide show visibility overlays',
    segment: { type: 'chart', tool: 'vis:ind', label: 'Indicators' } },
  { id: 'chart:isolate', category: 'Chart · view', label: 'Isolate / restore',
    keywords: 'solo single ticker focus multi',
    segment: { type: 'chart', tool: 'isolate', label: 'Isolate' } },
  { id: 'chart:settings', category: 'Chart · indicators', label: 'Indicator settings',
    keywords: 'period edit configure sma ema rsi',
    segment: { type: 'chart', tool: 'settings', label: 'Ind. settings' } },

  // ---- Tools --------------------------------------------------------------
  { id: 'tool:search', category: 'Tools', label: 'Search',
    keywords: 'omnibox address find', segment: { type: 'tool', tool: 'search', label: 'Search' } },

  // ---- Other --------------------------------------------------------------
  { id: 'other:placeholder', category: 'Other', label: 'Placeholder (empty slot)',
    keywords: 'blank none reserved', segment: { type: 'placeholder', label: '—' } },
]

/** Ticker entries are typed, not listed — the picker offers a ticker input
 *  that produces this. */
export function tickerEntry(symbol: string): WheelSegment {
  return { type: 'ticker', ticker: symbol.toUpperCase(), label: '' }
}

export function searchCatalog(query: string, category: string | null): CatalogEntry[] {
  const q = query.trim().toLowerCase()
  return CATALOG.filter((e) => {
    if (category && e.category !== category) return false
    if (!q) return true
    return (
      e.label.toLowerCase().includes(q) ||
      e.keywords.includes(q) ||
      e.category.toLowerCase().includes(q)
    )
  })
}
