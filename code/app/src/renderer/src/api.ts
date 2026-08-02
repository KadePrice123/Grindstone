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

export async function api<T = unknown>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await window.grindstone.request<T>(method, path, body)
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
