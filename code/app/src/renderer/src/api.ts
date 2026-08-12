/** Typed wrapper over the preload bridge. */

export interface ApiResponse<T = unknown> {
  status: number
  body: T
}

declare global {
  interface Window {
    grindstone: {
      request: <T = unknown>(method: string, path: string, body?: unknown) => Promise<ApiResponse<T>>
      /** Native file picker; an absolute path, or null if cancelled. The
       *  renderer never handles file bytes — it hands the backend a path. */
      pickFile: () => Promise<string | null>
      onSidecarStatus: (cb: (s: { status: string; detail?: string }) => void) => () => void
      signalUnlocked: () => void
      setTabMeta: (title: string, icon: string, depth: number, address: string) => void
      openTab: (route: string) => void
      openUrl: (url: string) => void
      openBacktestReport: (runId: number, file?: string) => void
      onNav: (cb: (what: 'back' | 'home') => void) => () => void
      onRoute: (cb: (route: string) => void) => () => void
      wheelEvt: (
        kind: 'down' | 'move' | 'up',
        x: number,
        y: number,
        ctx?: {
          context: string
          symbols?: string[]
          indicators?: string[]
          hidden?: string[]
          timeframe?: string
          flags?: string[]
          isolated?: string
        }
      ) => void
      onChartAction: (cb: (a: { tool: string; symbol?: string }) => void) => () => void
      /** Get/Post data actions: tool + intent (left=primary, right=quick) +
       *  the spawn coordinates the page resolves its element from. */
      onDataAction: (
        cb: (a: { tool: string; intent: 'primary' | 'quick'; spawn: { x: number; y: number }; entryId?: string }) => void
      ) => () => void
      onFavoritesChanged: (cb: () => void) => () => void
    }
    grindstoneSplit: {
      drag: (screenX: number) => void
    }
    grindstoneWheel: {
      onSpawn: (cb: (payload: unknown) => void) => () => void
      onUpdate: (cb: (payload: unknown) => void) => () => void
      onMode: (cb: (mode: string) => void) => () => void
      onPointer: (cb: (x: number, y: number) => void) => () => void
      onQuotes: (cb: (quotes: unknown) => void) => () => void
      onDespawn: (cb: () => void) => () => void
      ready: () => void
      act: (index: number, button?: 'left' | 'right') => void
      lockToggle: () => void
      close: () => void
      move: (x: number, y: number) => void
      evt: (kind: 'move' | 'up', x: number, y: number) => void
    }
  }
}

export class ApiError extends Error {
  constructor(
    public status: number,
    detail: string
  ) {
    super(detail)
  }
}

/**
 * A 401 on anything but login means the backend locked or restarted. The main
 * process has already dropped its token; the UI must follow or it sits in a
 * signed-in shell where every call fails.
 */
let onAuthExpired: (() => void) | null = null
export function setAuthExpiredHandler(fn: (() => void) | null): void {
  onAuthExpired = fn
}

export async function api<T = unknown>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await window.grindstone.request<T>(method, path, body)
  if (res.status === 401 && path !== '/api/auth/login' && path !== '/api/auth/setup') {
    onAuthExpired?.()
  }
  if (res.status >= 400) {
    const raw = (res.body as { detail?: unknown } | null)?.detail
    // FastAPI validation errors carry detail as a list of objects; String()
    // would render '[object Object]'.
    const detail =
      typeof raw === 'string'
        ? raw
        : raw != null
          ? JSON.stringify(raw).slice(0, 300)
          : `request failed (${res.status})`
    throw new ApiError(res.status, detail)
  }
  return res.body
}

export interface AccountRow {
  id: number
  broker: string
  kind: 'live' | 'paper' | 'data'
  nickname: string
  enabled: boolean
  created_at: string
  key_hints: Record<string, string>
}

export interface SearchResult {
  type: 'symbol' | 'news' | 'page' | 'action' | 'web' | 'web-news'
  title: string
  subtitle?: string
  symbol?: string
  asset_class?: string
  id?: number | string
  url?: string
  site?: string
  page?: string
  /** Help results: the section id the result deep-links to. */
  section?: string
  /** Pages only: false = announced but not built, so it must not look clickable. */
  ready?: boolean
  action?: string
  created_at?: string
}

export interface Quote {
  symbol: string
  available: boolean
  source?: string
  reason?: string
  price?: number | null
  change?: number | null
  change_pct?: number | null
  bid?: number | null
  ask?: number | null
  day_open?: number | null
  day_high?: number | null
  day_low?: number | null
  day_volume?: number | null
  prev_close?: number | null
  /** Provider-supplied extras; absent from feeds that do not carry them. */
  year_high?: number | null
  year_low?: number | null
  avg_volume?: number | null
  market_cap?: number | null
}

/** GET /api/quotes — the batch snapshot shape shared by the wheel's ticker
 *  segments and the home grid's symbol tiles: fetched once per spawn/load,
 *  never re-polled while shown (spec: numbers must not flash). */
export interface QuoteSnap {
  available: boolean
  price: number | null
  change_pct: number | null
}

export interface NewsItem {
  id: number
  headline: string
  summary: string
  source: string
  url: string
  symbols: string[]
  created_at: string
}

export interface SymbolSummary {
  symbol: string
  name: string
  asset_class: string
  quote: Quote
  news: NewsItem[]
}

export interface RecordJob {
  id: number
  kind: 'bars' | 'chain' | 'news'
  symbol: string
  timeframe: string
  interval_seconds: number
  retention_days: number
  enabled: number
  last_run_at: string
  last_status: string
  last_rows: number
}

export interface DataUsage {
  bars: { symbol: string; timeframe: string; n: number; oldest: string; newest: string }[]
  chain: { underlying: string; n: number; snapshots: number; oldest: string; newest: string }[]
  news: { count: number; newest: string | null; oldest: string | null }
  db_bytes: number
}

export interface BacktestPreset {
  id: number
  name: string
  spec: Record<string, unknown>
  builtin: number
  calibration: number
  created_at: string
  updated_at: string
}

export interface BacktestProgress {
  phase?: string
  i?: number
  n?: number
  pct?: number
}

export interface BacktestSummary {
  cagr_pct?: number
  total_return_pct?: number
  final_net_liq?: number
  max_drawdown_pct?: number
  sharpe?: number
  sortino?: number
  win_rate_pct?: number
  profit_factor?: number
  trades?: number
  avg_dit?: number
  total_fees?: number
  references?: number
  [k: string]: unknown
}

export interface BacktestCalibLayer {
  [metric: string]: number | string
}

export interface BacktestCalibRef {
  reference: string
  preset: string
  summary: BacktestSummary
  layers: Record<string, BacktestCalibLayer>
  report: string
}

export interface BacktestRun {
  id: number
  preset_id: number | null
  name: string
  kind: 'run' | 'calibration'
  status: 'running' | 'done' | 'error' | 'cancelled'
  started_at: string
  finished_at: string
  error: string
  summary: BacktestSummary
  report_files: string[]
  progress?: BacktestProgress
  calib?: BacktestCalibRef[] | null
  spec?: Record<string, unknown> | null
}

export interface BacktestRecordedStats {
  days: number
  first: string | null
  last: string | null
  contracts: number
  bar_days: number
}

export interface BacktestSyncStatus {
  state: 'idle' | 'running' | 'done' | 'error'
  days?: number
  contracts?: number
  bars?: number
  error?: string
}

export interface BacktestStatus {
  options_db: { path: string; present: boolean; size_mb: number }
  bars_db: { path: string; present: boolean }
  vix_csv: { present: boolean }
  calibration: { references: string[]; mapped: string[] }
  /** 'deep' = the uniform data tree; a string starting with 'legacy' names
   *  the migration (tools/consolidate.py) and is shown verbatim. */
  source: 'deep' | 'recorded' | 'custom' | (string & {})
  underlying: string
  recorded: BacktestRecordedStats
  reason: string
  can_run: boolean
  active: { id: number; progress: BacktestProgress } | null
  sync: BacktestSyncStatus
}

export interface TestResult {
  ok: boolean
  error?: string
  status?: string
  equity?: number
  buying_power?: number
  options_level?: number
  account_last4?: string
  kind?: string
  detail?: string
}
