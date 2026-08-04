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
import { ipcMain, WebFrameMain, webContents } from 'electron'
import http from 'node:http'
import { log } from './log'
import type { Sidecar } from './sidecar'

/**
 * Talk to the sidecar over node:http with pooling DISABLED (agent: false).
 *
 * We used fetch() and it hung — reproducibly for a user, invisibly in tests.
 * Root cause: undici keeps connections alive, uvicorn closes idle ones after
 * ~5s, and a request sent down a socket the server is simultaneously closing
 * is simply lost. undici does not retry non-idempotent methods, so the POST
 * never settled. GETs hid it because the boot poller kept a connection warm;
 * the failure appeared exactly when a human paused to type a password.
 *
 * A fresh connection per request costs microseconds on loopback and removes
 * the entire class of bug. Every request also carries a hard deadline, so
 * "hangs forever" is not a reachable state.
 */
const REQUEST_TIMEOUT_MS = 30_000

interface RawResponse {
  status: number
  text: string
}

function request(
  port: number,
  method: string,
  path: string,
  headers: Record<string, string>,
  payload: string | null
): Promise<RawResponse> {
  return new Promise((resolve, reject) => {
    const req = http.request(
      {
        host: '127.0.0.1',
        port,
        path,
        method,
        agent: false, // no keep-alive pool: see the note above
        headers: payload
          ? { ...headers, 'Content-Length': Buffer.byteLength(payload) }
          : headers,
        timeout: REQUEST_TIMEOUT_MS,
      },
      (res) => {
        let data = ''
        res.setEncoding('utf-8')
        res.on('data', (c) => (data += c))
        res.on('end', () => resolve({ status: res.statusCode ?? 0, text: data }))
      }
    )
    req.on('timeout', () => req.destroy(new Error(`timed out after ${REQUEST_TIMEOUT_MS}ms`)))
    req.on('error', reject)
    if (payload) req.write(payload)
    req.end()
  })
}

const AUTH_CAPTURE_PATHS = new Set(['/api/auth/login', '/api/auth/setup'])
const AUTH_CLEAR_PATHS = new Set(['/api/auth/logout', '/api/auth/lock'])
const ALLOWED_METHODS = new Set(['GET', 'POST', 'DELETE', 'PUT', 'PATCH'])

let sessionToken: string | null = null
let notifyAuth: ((signedIn: boolean) => void) | null = null
let sidecarRef: Sidecar | null = null

function setToken(t: string | null): void {
  const was = sessionToken !== null
  sessionToken = t
  const is = sessionToken !== null
  if (was === is) return
  if (is) {
    // UNLOCK IS NOT ANNOUNCED HERE. The lock screen lives in the window's
    // chrome view, and unlocking reloads that very view — tearing down the
    // frame still waiting for this request's reply, so the login promise
    // never settled ("Working…" forever). Deferring by a tick was NOT
    // enough: the reply crosses a process boundary and may still be in
    // flight. The renderer now signals 'auth:unlocked' once it has the
    // answer in hand, which removes the race instead of narrowing it.
    return
  }
  // Locking is safe to announce immediately: nothing is awaiting a reply
  // that the lock screen needs.
  setImmediate(() => notifyAuth?.(false))
}

/** Called from the renderer via 'auth:unlocked' once sign-in has resolved. */
export function announceUnlockedIfSignedIn(): void {
  if (sessionToken !== null) notifyAuth?.(true)
}

export function isSignedIn(): boolean {
  return sessionToken !== null
}

/**
 * Main's OWN door to the backend (the gesture wheel fetches its config and
 * ticker quotes from here). Same transport, same tokens, no renderer in the
 * loop — and the result never leaves this process unscrubbed because the
 * caller IS this process.
 */
export async function mainRequest<T = unknown>(
  method: string,
  path: string,
  body?: unknown
): Promise<{ status: number; body: T | null }> {
  const state = sidecarRef?.current
  if (!state) return { status: 503, body: null }
  const headers: Record<string, string> = { 'X-App-Token': state.bootToken }
  if (sessionToken) headers['Authorization'] = `Bearer ${sessionToken}`
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  try {
    const res = await request(
      state.port,
      method,
      path,
      headers,
      body !== undefined ? JSON.stringify(body) : null
    )
    let payload: T | null = null
    try {
      payload = res.text ? (JSON.parse(res.text) as T) : null
    } catch {
      payload = null
    }
    return { status: res.status, body: payload }
  } catch (e) {
    log('mainRequest FAILED', method, path, String(e).slice(0, 120))
    return { status: 502, body: null }
  }
}

/**
 * Build the one-shot URL for a backtest report tab. The report route is the
 * single header-exempt door in the backend (browser tabs cannot send
 * X-App-Token); its guard is a 60s single-use key minted here, over the
 * authed API, by the only process that holds the tokens and the port.
 */
export async function backtestReportUrl(
  runId: number,
  file?: string
): Promise<string | null> {
  const state = sidecarRef?.current
  if (!state) return null
  const res = await mainRequest<{ report_key?: string }>(
    'POST',
    `/api/backtests/runs/${runId}/report-key`,
    file ? { file } : {}
  )
  const key = res.body?.report_key
  if (res.status !== 200 || !key) return null
  return `http://127.0.0.1:${state.port}/api/backtests/report/${runId}?k=${encodeURIComponent(key)}`
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
  sidecarRef = sidecar
  ipcMain.handle('api:request', async (event, req: unknown) => {
    const { method, path, body } = (req ?? {}) as {
      method?: string
      path?: string
      body?: unknown
    }
    log('proxy IN', method, path)
    if (!frameIsOurs(event.senderFrame)) {
      log('proxy REJECT unknown sender', method, path)
      return { status: 403, body: { detail: 'unknown sender' } }
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
    // Report keys are minted by MAIN only (backtestReportUrl): the field name
    // survives scrub() by design, so the route itself must be walled off from
    // renderers — otherwise any content view could mint (or burn) keys.
    if (route.endsWith('/report-key')) {
      return { status: 403, body: { detail: 'main-process only' } }
    }

    const state = sidecar.current
    if (!state) {
      return { status: 503, body: { detail: 'backend not running' } }
    }

    const headers: Record<string, string> = { 'X-App-Token': state.bootToken }
    if (sessionToken) headers['Authorization'] = `Bearer ${sessionToken}`
    if (body !== undefined) headers['Content-Type'] = 'application/json'

    const started = Date.now()
    let res: RawResponse
    try {
      const target = url.pathname + url.search
      res = await request(
        state.port,
        m,
        target,
        headers,
        body !== undefined ? JSON.stringify(body) : null
      )
    } catch (e) {
      log('proxy: request FAILED', m, route, String(e).slice(0, 200))
      return { status: 502, body: { detail: `backend unreachable: ${String(e)}` } }
    }

    let payload: unknown = null
    try {
      payload = res.text ? JSON.parse(res.text) : null
    } catch (e) {
      log('proxy: response body was not JSON', route, String(e).slice(0, 120))
      payload = null
    }
    log('proxy', m, route, '->', res.status, `${Date.now() - started}ms`)

    if (res.status >= 200 && res.status < 300 && AUTH_CAPTURE_PATHS.has(route)) {
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

    // Favorites render in four places at once (home grid, omnibox star,
    // wheel, picker) across SEPARATE views; every mutation flows through
    // this proxy, so this is the one chokepoint that can tell them all to
    // refetch. Views without a listener (hardened browser tabs) ignore it.
    if (res.status >= 200 && res.status < 300 && m !== 'GET'
        && route.startsWith('/api/favorites')) {
      for (const wc of webContents.getAllWebContents()) wc.send('favorites:changed')
    }

    return { status: res.status, body: scrub(payload) }
  })
}

export function clearSession(): void {
  setToken(null)
}
