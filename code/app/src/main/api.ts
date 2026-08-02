/**
 * IPC → HTTP proxy. The renderer NEVER talks to the sidecar directly and
 * NEVER sees a token: the boot token and the user's session token live only
 * in this process (REQUIREMENTS.md 6.7).
 *
 * Two rules make that hold rather than merely intend it:
 *  1. The renderer-supplied path is canonicalized once and rejected unless it
 *     is already canonical — so '/api/auth/login?x=1' and '/api/auth/login/'
 *     cannot reach the same backend route while dodging our route matching.
 *  2. A `token` key is stripped from EVERY response body by default; the
 *     auth routes are special-cased only to *capture* it. A future
 *     token-returning route therefore cannot leak by omission.
 */
import { ipcMain, WebFrameMain } from 'electron'
import type { Sidecar } from './sidecar'

const AUTH_CAPTURE_PATHS = new Set(['/api/auth/login', '/api/auth/setup'])
const AUTH_CLEAR_PATHS = new Set(['/api/auth/logout', '/api/auth/lock'])
const ALLOWED_METHODS = new Set(['GET', 'POST', 'DELETE', 'PUT', 'PATCH'])

let sessionToken: string | null = null
let notifyAuth: ((signedIn: boolean) => void) | null = null

function setToken(t: string | null): void {
  const was = sessionToken !== null
  sessionToken = t
  const is = sessionToken !== null
  if (was !== is) notifyAuth?.(is)
}

function frameIsOurs(frame: WebFrameMain | null): boolean {
  if (!frame) return false
  const devUrl = process.env['ELECTRON_RENDERER_URL']
  if (devUrl) return frame.url.startsWith(devUrl)
  // Packaged: only our own bundled renderer file, not any file:// URL.
  return frame.url.startsWith('file://') && frame.url.includes('/out/renderer/')
}

/** Remove any token-ish field before a body can reach the renderer. */
function scrub(body: unknown): unknown {
  if (body === null || typeof body !== 'object') return body
  if (Array.isArray(body)) return body.map(scrub)
  const out: Record<string, unknown> = {}
  for (const [k, v] of Object.entries(body as Record<string, unknown>)) {
    if (k === 'token' || k === 'access_token' || k === 'refresh_token') continue
    out[k] = scrub(v)
  }
  return out
}

export function registerApiBridge(
  sidecar: Sidecar,
  onAuthChange?: (signedIn: boolean) => void
): void {
  notifyAuth = onAuthChange ?? null
  ipcMain.handle('api:request', async (event, req: unknown) => {
    if (!frameIsOurs(event.senderFrame)) {
      return { status: 403, body: { detail: 'unknown sender' } }
    }
    const { method, path, body } = (req ?? {}) as {
      method?: string
      path?: string
      body?: unknown
    }
    const m = (method ?? 'GET').toUpperCase()
    if (!ALLOWED_METHODS.has(m)) {
      return { status: 400, body: { detail: 'bad method' } }
    }
    if (typeof path !== 'string' || !path.startsWith('/api/')) {
      return { status: 400, body: { detail: 'bad path' } }
    }
    // Canonical or rejected — never silently rewritten. Query strings ARE
    // allowed (the omnibox lives on /api/search?q=...; rejecting them
    // silently killed search in production while every test bypassed this
    // proxy). The invariant that actually protects tokens is below: route
    // matching uses the PARSED pathname, so '/api/auth/login?x=1' can never
    // reach the login route while dodging token capture/stripping.
    let url: URL
    try {
      url = new URL(path, 'http://127.0.0.1')
    } catch {
      return { status: 400, body: { detail: 'bad path' } }
    }
    if (url.hash || url.pathname + url.search !== path) {
      return { status: 400, body: { detail: 'path must be canonical' } }
    }
    const route = url.pathname.replace(/\/+$/, '')
    if (!route.startsWith('/api/')) {
      return { status: 400, body: { detail: 'bad path' } }
    }

    const state = sidecar.current
    if (!state) {
      return { status: 503, body: { detail: 'backend not running' } }
    }

    const headers: Record<string, string> = { 'X-App-Token': state.bootToken }
    if (sessionToken) headers['Authorization'] = `Bearer ${sessionToken}`
    if (body !== undefined) headers['Content-Type'] = 'application/json'

    let res: Response
    try {
      res = await fetch(`http://127.0.0.1:${state.port}${url.pathname}${url.search}`, {
        method: m,
        headers,
        body: body !== undefined ? JSON.stringify(body) : undefined,
        // A server-side redirect must never silently move us to another route.
        redirect: 'error',
      })
    } catch (e) {
      return { status: 502, body: { detail: `backend unreachable: ${String(e)}` } }
    }

    let payload: unknown = null
    try {
      payload = await res.json()
    } catch {
      payload = null
    }

    if (res.ok && AUTH_CAPTURE_PATHS.has(route)) {
      const t = (payload as { token?: string } | null)?.token
      if (typeof t === 'string' && t) setToken(t)
    }
    if (AUTH_CLEAR_PATHS.has(route)) {
      setToken(null)
    }
    // A 401 on any non-login route means the backend dropped our session.
    if (res.status === 401 && route !== '/api/auth/login') {
      setToken(null)
    }

    return { status: res.status, body: scrub(payload) }
  })
}

export function clearSession(): void {
  setToken(null)
}
