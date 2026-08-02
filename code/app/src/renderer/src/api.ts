/** Typed wrapper over the preload bridge. */

export interface ApiResponse<T = unknown> {
  status: number
  body: T
}

declare global {
  interface Window {
    grindstone: {
      request: <T = unknown>(method: string, path: string, body?: unknown) => Promise<ApiResponse<T>>
      onSidecarStatus: (cb: (s: { status: string; detail?: string }) => void) => () => void
      setTabMeta: (title: string, icon: string) => void
      openTab: (route: string) => void
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
    const detail =
      (res.body as { detail?: string } | null)?.detail ?? `request failed (${res.status})`
    throw new ApiError(res.status, String(detail))
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
  type: 'symbol' | 'news' | 'page' | 'action'
  title: string
  subtitle?: string
  symbol?: string
  asset_class?: string
  id?: number
  url?: string
  page?: string
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
