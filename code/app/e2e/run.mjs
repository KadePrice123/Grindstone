/**
 * Live end-to-end diagnostic — NOT part of the offline gate.
 *
 * It exists because every offline test talks to the backend directly, and a
 * bug in the Electron proxy (which only the real app crosses) shipped and
 * broke search three times before a human found it. This drives the REAL
 * renderer bridges over the Chrome DevTools Protocol.
 *
 *   npm run e2e
 *
 * Launches the built app on a throwaway data dir, so it never touches your
 * real profile, then asserts:
 *   proxy — query-string requests reach the backend; no token reaches the
 *           renderer even on path variants
 *   tabs  — new tab, open-in-tab, tear-off to a new window, adopt back, and
 *           the live-view marker survives (re-parented, never reloaded)
 */
import { spawn } from 'node:child_process'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const APP = path.resolve(HERE, '..')
const PORT = 9310 + (process.pid % 300)
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const dataDir = mkdtempSync(path.join(tmpdir(), 'grindstone-e2e-'))
const electron = path.join(APP, 'node_modules', 'electron', 'dist', 'electron.exe')
const child = spawn(electron, [`--remote-debugging-port=${PORT}`, '.'], {
  cwd: APP,
  env: { ...process.env, GRINDSTONE_DATA_DIR: dataDir },
  stdio: 'ignore',
  windowsHide: false,
})

const cleanup = () => {
  try {
    child.kill()
  } catch {
    /* already gone */
  }
  try {
    rmSync(dataDir, { recursive: true, force: true })
  } catch {
    /* windows file locks; the temp dir is disposable anyway */
  }
}

let failures = 0
const check = (ok, label, detail = '') => {
  console.log(`${ok ? 'ok  ' : 'FAIL'}  ${label}${detail ? ' — ' + detail : ''}`)
  if (!ok) failures += 1
}

async function targets() {
  const res = await fetch(`http://127.0.0.1:${PORT}/json`)
  return (await res.json()).filter((t) => t.type === 'page')
}

async function connect(target) {
  const ws = new WebSocket(target.webSocketDebuggerUrl)
  let id = 0
  const pending = new Map()
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data)
    if (m.id && pending.has(m.id)) {
      pending.get(m.id)(m)
      pending.delete(m.id)
    }
  }
  await new Promise((resolve, reject) => {
    ws.onopen = resolve
    ws.onerror = reject
  })
  return {
    eval: async (expression) => {
      const mid = ++id
      const res = await new Promise((resolve) => {
        pending.set(mid, resolve)
        ws.send(
          JSON.stringify({
            id: mid,
            method: 'Runtime.evaluate',
            params: { expression, awaitPromise: true, returnByValue: true },
          })
        )
      })
      return res.result?.result?.value
    },
  }
}

async function waitFor(pred, what, ms = 20000) {
  const deadline = Date.now() + ms
  while (Date.now() < deadline) {
    try {
      const hit = await pred()
      if (hit) return hit
    } catch {
      /* app still starting */
    }
    await sleep(400)
  }
  throw new Error(`timed out waiting for ${what}`)
}

try {
  const authTarget = await waitFor(
    async () => (await targets()).find((t) => t.url.includes('mode=auth')),
    'the lock screen'
  )
  const auth = await connect(authTarget)
  await sleep(1500)

  // ---------------------------------------------------------------- proxy
  const status = await auth.eval(
    `window.grindstone.request('GET','/api/auth/status').then(r=>JSON.stringify(r))`
  )
  check(status.includes('"status":200'), 'proxy: plain GET reaches the backend', status.slice(0, 60))

  const login = await auth.eval(
    `window.grindstone.request('POST','/api/auth/login?x=1',{username:'nope',password:'nopenopenope'}).then(r=>JSON.stringify(r))`
  )
  check(!login.includes('"token"'), 'proxy: no token reaches the renderer on a path variant')
  check(login.includes('401'), 'proxy: query-string path still routes to the backend', login.slice(0, 60))

  // ----------------------------------------------------------------- tabs
  // The login REPLY must reach the renderer. Unlocking reloads the very view
  // that is awaiting it, and doing that inline killed the promise — the
  // button span "Working…" forever with no error anywhere (reported live).
  const setup = await Promise.race([
    auth.eval(
      `window.grindstone.request('POST','/api/auth/setup',{username:'e2e',password:'e2e-password-1'}).then(r=>JSON.stringify(r))`
    ),
    sleep(15000).then(() => null),
  ])
  check(setup !== null, 'auth: the sign-in reply reaches the renderer (never hangs)')
  check(!!setup && setup.includes('"status":200'), 'auth: first-run setup succeeds')
  await sleep(2500)

  const chromeTarget = await waitFor(
    async () => (await targets()).find((t) => t.url.includes('mode=chrome')),
    'the tab strip'
  )
  let chromeA = await connect(chromeTarget)

  // Now the returning-user path: lock, then sign in again. First-run setup
  // and login take different branches, and only login was broken in the
  // field, so both must be exercised.
  await chromeA.eval(`window.grindstone.request('POST','/api/auth/lock')`)
  const relock = await waitFor(
    async () => (await targets()).find((t) => t.url.includes('mode=auth')),
    'the lock screen after locking'
  )
  const auth2 = await connect(relock)
  await sleep(800)
  const relogin = await Promise.race([
    auth2.eval(
      `window.grindstone.request('POST','/api/auth/login',{username:'e2e',password:'e2e-password-1'}).then(r=>JSON.stringify(r))`
    ),
    sleep(15000).then(() => null),
  ])
  check(relogin !== null, 'auth: a returning user’s login reply also reaches the renderer')
  check(
    !!relogin && relogin.includes('"status":200'),
    'auth: login succeeds',
    String(relogin).slice(0, 60)
  )
  const backToTabs = await waitFor(
    async () => (await targets()).find((t) => t.url.includes('mode=chrome')),
    'the tab strip after login'
  ).catch(() => null)
  check(!!backToTabs, 'auth: signing in returns you to the tab UI')
  // Re-point at the post-login strip: unlocking reloads the chrome view, so
  // the old connection refers to a page that no longer exists.
  if (backToTabs) chromeA = await connect(backToTabs)
  let state = await chromeA.eval('window.grindstoneTabs.getState()')
  check(state?.tabs?.length === 1, 'tabs: unlocking opens one tab', `got ${state?.tabs?.length}`)

  await chromeA.eval(`(window.grindstoneTabs.newTab(), 'ok')`)
  await sleep(1200)
  const contents = (await targets()).filter((t) => t.url.includes('mode=content'))
  check(contents.length === 2, 'tabs: new tab creates a live view', `views=${contents.length}`)

  const second = await connect(contents[1])
  await second.eval(`(window.grindstone.openTab('symbol:SPY'), 'ok')`)
  await sleep(1600)
  state = await chromeA.eval('window.grindstoneTabs.getState()')
  const spyTab = state.tabs.find((t) => t.title === 'SPY')
  check(!!spyTab, 'tabs: a page opens in a new tab and reports its title')

  const spyTarget = (await targets()).find((t) => decodeURIComponent(t.url).includes('symbol:SPY'))
  const spy = await connect(spyTarget)
  await spy.eval(`(window.__e2e_mark = 42, 'marked')`)

  await chromeA.eval(
    `(window.grindstoneTabs.dragStart(${spyTab.id}), window.grindstoneTabs.dragMove(700,600), window.grindstoneTabs.dragEnd(700,600), 'torn')`
  )
  await sleep(1600)
  let chromes = (await targets()).filter((t) => t.url.includes('mode=chrome'))
  check(chromes.length === 2, 'tabs: dragging a tab out creates a new window', `windows=${chromes.length}`)

  let chromeB = null
  for (const t of chromes) {
    const c = await connect(t)
    const s = await c.eval('window.grindstoneTabs.getState()')
    if (s && s.tabs.some((x) => x.id === spyTab.id)) chromeB = c
  }
  check(!!chromeB, 'tabs: the torn tab lives in the new window')

  const boundsA = (await chromeA.eval('window.grindstoneTabs.getState()')).bounds
  const dropX = boundsA.x + 80
  const dropY = boundsA.y + 10
  await chromeB.eval(
    `(window.grindstoneTabs.dragStart(${spyTab.id}), window.grindstoneTabs.dragMove(${dropX},${dropY}), window.grindstoneTabs.dragEnd(${dropX},${dropY}), 'adopted')`
  )
  await sleep(1600)
  chromes = (await targets()).filter((t) => t.url.includes('mode=chrome'))
  check(chromes.length === 1, 'tabs: emptied window closes after its last tab leaves', `windows=${chromes.length}`)
  const finalState = await chromeA.eval('window.grindstoneTabs.getState()')
  check(finalState.tabs.length === 3, 'tabs: the tab was adopted back', `tabs=${finalState.tabs.length}`)

  const mark = await spy.eval('window.__e2e_mark')
  check(mark === 42, 'tabs: LIVE view survived tear-off + adopt (no reload)', `marker=${mark}`)

  // ---------------------------------------------------------------- chart
  // The chart is canvas-based; a CSP or v5-API mistake shows up as zero
  // canvases plus console errors, never as a visible exception.
  // Charts wait on a network round-trip (and a keyless-fallback provider on a
  // cold profile), so give rendering real time before calling it a failure.
  const canvases = await waitFor(
    async () => {
      const n = await spy.eval('document.querySelectorAll("canvas").length')
      return n > 0 ? n : null
    },
    'the chart canvas',
    45000
  ).catch(() => 0)
  // Honest assertion: a cold e2e profile has no broker account, so the
  // chart legitimately has no data. What must ALWAYS hold is that the chart
  // area resolves to one of two correct states — a canvas, or a labelled
  // empty state — and never to a blank void or a hang.
  const chartState = await spy.eval(
    `JSON.stringify({
       canvas: document.querySelectorAll('canvas').length,
       empty: document.querySelector('.chart-empty')?.textContent?.slice(0,80) ?? null
     })`
  )
  const cs = JSON.parse(chartState)
  check(
    cs.canvas > 0 || !!cs.empty,
    'chart: renders a canvas, or says why it cannot',
    cs.canvas > 0 ? `${cs.canvas} canvas` : `empty state: "${cs.empty}"`
  )

  if (canvases === 0) {
    // Do not guess why: ask the page what it actually has.
    const diag = await spy.eval(
      `JSON.stringify({
         boxes: document.querySelectorAll('.chart-box').length,
         empty: document.querySelector('.chart-empty')?.textContent?.slice(0,120) ?? null,
         w: document.querySelector('.chart-box')?.clientWidth ?? null,
         h: document.querySelector('.chart-box')?.clientHeight ?? null,
         bodyW: document.body.clientWidth,
         errs: (window.__e2e_errs||[]).slice(-3)
       })`
    )
    console.log('      chart diagnostic:', diag)
    // Never await an unbounded page promise here: a hanging backend call
    // would leave the harness stuck instead of reporting a failure.
    const bars = await Promise.race([
      spy.eval(
        `window.grindstone.request('GET','/api/symbols/SPY/bars?timeframe=1Day&limit=20')
          .then(r => JSON.stringify({status:r.status, source:r.body?.source, n:r.body?.bars?.length, reason:r.body?.reason}))`
      ),
      sleep(20000).then(() => '(no answer in 20s — backend call is hanging)'),
    ])
    console.log('      bars endpoint:', bars)
  }
  const chartErr = await spy.eval(
    `(window.__e2e_errs || []).filter(e => /lightweight|chart|canvas/i.test(e)).length`
  )
  check(!chartErr, 'chart: no chart-related console errors')

  // The bars endpoint must ANSWER, quickly, even when it has nothing to
  // serve — a hanging data path was a real bug (unbounded Yahoo fallback).
  const t0 = Date.now()
  const barsAnswer = await Promise.race([
    spy.eval(
      `window.grindstone.request('GET','/api/symbols/SPY/bars?timeframe=1Day&limit=20')
        .then(r => JSON.stringify({status:r.status, source:r.body?.source, n:r.body?.bars?.length}))`
    ),
    sleep(20000).then(() => null),
  ])
  check(
    barsAnswer !== null,
    'data: the bars endpoint always answers (never hangs)',
    barsAnswer ? `${Date.now() - t0}ms ${barsAnswer}` : 'no answer in 20s'
  )

  // -------------------------------------------------------------- browsing
  // News must open IN the app. iframes cannot show news sites (they send
  // X-Frame-Options/frame-ancestors), so this must be a real browser view.
  const beforeTabs = (await chromeA.eval('window.grindstoneTabs.getState()')).tabs.length
  await spy.eval(`(window.grindstone.openUrl('https://example.com/'), 'ok')`)
  await sleep(2500)
  const afterState = await chromeA.eval('window.grindstoneTabs.getState()')
  check(
    afterState.tabs.length === beforeTabs + 1,
    'browsing: a URL opens as an in-app tab, not in the OS browser',
    `tabs ${beforeTabs} -> ${afterState.tabs.length}`
  )
  const browserTab = afterState.tabs.find((t) => t.kind === 'browser')
  check(!!browserTab, 'browsing: the new tab is a browser tab with its own kind')
  check(
    !!browserTab && /example\.com/.test(browserTab.url ?? ''),
    'browsing: the tab reports the page URL to the strip',
    browserTab?.url
  )
} catch (err) {
  console.log('FAIL  harness error —', err.message)
  failures += 1
} finally {
  cleanup()
}

console.log(failures === 0 ? 'E2E OK' : `E2E FAILED (${failures})`)
process.exit(failures === 0 ? 0 : 1)
