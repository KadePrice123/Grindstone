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
import { appendFileSync, mkdirSync, mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const APP = path.resolve(HERE, '..')
const PORT = 9310 + (process.pid % 300)
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

// ---------------------------------------------------------------------------
// The run log
// ---------------------------------------------------------------------------
// Every line also goes to a file, APPENDED and flushed on the spot. Three
// failures this fixes, all of them met in practice:
//   - a run watched through a pipe (`npm run e2e | tail`) prints NOTHING until
//     it exits, and the pipe hands back TAIL's exit code, so a red run reads
//     as green. The log is readable while the run is still going: `tail -f` it.
//   - a run that is killed, wedges, or dies on a CDP timeout loses every result
//     it had already proved. appendFileSync — not a write stream — because a
//     hard kill cannot drop what was never buffered.
//   - the previous run is gone the moment the next one starts, so "it passed
//     before my change" is unanswerable. Appending keeps the history; each run
//     is delimited by its own header.
// Redirect elsewhere with GRINDSTONE_E2E_LOG; the default is gitignored.
const LOG = process.env.GRINDSTONE_E2E_LOG ?? path.join(HERE, 'e2e.log')
try {
  mkdirSync(path.dirname(LOG), { recursive: true })
} catch {
  /* the append below reports it far more usefully than a mkdir race would */
}
let logBroken = false
const fmt = (a) => {
  if (typeof a === 'string') return a
  try {
    return JSON.stringify(a)
  } catch {
    return String(a) // circular, or a DOM handle that came back over CDP
  }
}
const rawLog = console.log.bind(console)
console.log = (...args) => {
  rawLog(...args)
  if (logBroken) return
  try {
    appendFileSync(LOG, `${new Date().toISOString().slice(11, 23)}  ${args.map(fmt).join(' ')}\n`)
  } catch (err) {
    logBroken = true // say it ONCE, on the terminal, and never wedge the run
    rawLog(`      (run log unavailable at ${LOG}: ${err.message})`)
  }
}
console.log(`\n=== e2e ${new Date().toISOString()} · pid ${process.pid} · cdp ${PORT} ===`)

// A throwaway profile by default, but overridable: when a run fails for a
// reason only the backend log explains, point this somewhere that survives.
const dataDir = process.env.GRINDSTONE_E2E_DATA_DIR ?? mkdtempSync(path.join(tmpdir(), 'grindstone-e2e-'))
const keepData = !!process.env.GRINDSTONE_E2E_DATA_DIR
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
    if (!keepData) rmSync(dataDir, { recursive: true, force: true })
    else console.log(`      profile kept at ${dataDir}`)
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

/** One live connection per target — polling loops that reconnect every tick
 *  leak sockets until CDP wedges (this hung a full run). */
const connCache = new Map()

async function connect(target) {
  const cached = connCache.get(target.id)
  if (cached && cached.alive()) return cached
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
  // A dying socket must SETTLE every in-flight await — an unresolved promise
  // inside a waitFor poll is an invisible permanent hang, not a failure.
  const settleAll = () => {
    for (const resolve of pending.values()) resolve({ result: undefined })
    pending.clear()
  }
  ws.onclose = settleAll
  await new Promise((resolve, reject) => {
    ws.onopen = resolve
    ws.onerror = (e) => {
      settleAll()
      reject(e)
    }
  })
  const raw = async (method, params) => {
    const mid = ++id
    const res = await new Promise((resolve) => {
      pending.set(mid, resolve)
      ws.send(JSON.stringify({ id: mid, method, params }))
    })
    return res.result
  }
  const conn = {
    alive: () => ws.readyState === WebSocket.OPEN,
    /** Raw CDP — Input.dispatchMouseEvent sends TRUSTED input, the only way
     *  to exercise the chart tools the way a hand does. */
    send: raw,
    /** A real left click at view coordinates, arriving the way a HAND does:
     *  a short streamed approach, a beat, then press+release. Two measured
     *  reasons — lightweight-charts derives click coordinates from its
     *  crosshair (a press with no prior move is point-less and dropped), and
     *  on dense charts the crosshair updates on rAF, so a lone move
     *  immediately followed by press loses the race and the click never
     *  fires (found when 'all'-depth charts landed: 8,433 bars). */
    click: async (x, y) => {
      const base = { x: Math.round(x), y: Math.round(y), button: 'left', clickCount: 1 }
      for (const dx of [-24, -9, 0]) {
        await raw('Input.dispatchMouseEvent', { type: 'mouseMoved', x: base.x + dx, y: base.y })
        await sleep(30)
      }
      await raw('Input.dispatchMouseEvent', { type: 'mousePressed', ...base })
      await raw('Input.dispatchMouseEvent', { type: 'mouseReleased', ...base })
    },
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
  connCache.set(target.id, conn)
  return conn
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
  // Sign in THROUGH THE FORM, as a user does. Calling the API directly is
  // what let two sign-in bugs ship: the failure was never in the request, it
  // was in the handshake between the reply and the shell swapping this view.
  const fillAndSubmit = (password) => `(() => {
    const set = (el, v) => {
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value').set
      setter.call(el, v)
      el.dispatchEvent(new Event('input', { bubbles: true }))
    }
    const inputs = document.querySelectorAll('input.field')
    if (inputs.length < 2) return 'no form'
    set(inputs[0], 'e2e')
    for (let i = 1; i < inputs.length; i++) set(inputs[i], ${JSON.stringify(password)})
    const btn = [...document.querySelectorAll('button')]
      .find(b => /create profile|unlock/i.test(b.textContent || ''))
    if (!btn) return 'no button'
    btn.click()
    return 'submitted'
  })()`

  // REGRESSION: sign-in hung for a real user because the proxy pooled
  // connections and uvicorn closed idle ones after ~5s — the exact gap a
  // human leaves while typing a password. Idle here on purpose before
  // submitting, so a pooled-connection bug cannot hide behind fast tests.
  await sleep(9000)

  const submitted = await auth.eval(fillAndSubmit('e2e-password-1'))
  check(submitted === 'submitted', 'auth: the sign-in form submits', String(submitted))

  // The button must not be left spinning: either we advance, or an error is
  // shown. "Working…" forever is the exact failure users hit.
  const settled = await waitFor(
    async () => {
      const s = await auth.eval(
        `JSON.stringify({
           busy: !!document.querySelector('button.primary')?.textContent?.match(/Working/),
           err: document.querySelector('.error-text')?.textContent ?? null,
           gone: !document.querySelector('input.field')
         })`
      )
      const st = JSON.parse(s)
      return st.gone || st.err || !st.busy ? st : null
    },
    'the sign-in button to stop spinning',
    25000
  ).catch(() => null)
  check(!!settled, 'auth: the sign-in button never spins forever')
  check(settled && !settled.err, 'auth: sign-in reports no error', settled?.err ?? '')

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
  const resubmitted = await auth2.eval(fillAndSubmit('e2e-password-1'))
  check(resubmitted === 'submitted', 'auth: the returning-user form submits', String(resubmitted))
  const settled2 = await waitFor(
    async () => {
      const s = await auth2.eval(
        `JSON.stringify({
           busy: !!document.querySelector('button.primary')?.textContent?.match(/Working/),
           err: document.querySelector('.error-text')?.textContent ?? null,
           gone: !document.querySelector('input.field')
         })`
      )
      const st = JSON.parse(s)
      return st.gone || st.err || !st.busy ? st : null
    },
    'the login button to stop spinning',
    25000
  ).catch(() => null)
  check(!!settled2, 'auth: a returning user’s login never spins forever')
  check(settled2 && !settled2.err, 'auth: login reports no error', settled2?.err ?? '')
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

  // The numbers beside the chart resolve the same way: a real grid, or a
  // stated reason. Same rule — never a blank void.
  const metricState = await spy.eval(
    `JSON.stringify({
       grid: document.querySelectorAll('.metrics .metric').length,
       why: document.querySelector('.metrics-empty')?.textContent?.slice(0,80) ?? null
     })`
  )
  const ms = JSON.parse(metricState)
  check(
    ms.grid > 0 || !!ms.why,
    'metrics: the chart is accompanied by numbers, or by a reason',
    ms.grid > 0 ? `${ms.grid} metrics` : `no quote: "${ms.why}"`
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

  // This profile has NO broker account — which is precisely the case the
  // keyless fallback exists for. It silently stopped working twice (a heavy
  // import blew the deadline and tripped the breaker), and both times the
  // symptom was an empty chart that looked like an honest empty state.
  const src = barsAnswer ? JSON.parse(barsAnswer).source : 'none'
  check(
    src !== 'none',
    'fallback: a profile with no broker account still gets market data',
    `source=${src}`
  )

  // -------------------------------------------------------------- browsing
  // News must open IN the app. iframes cannot show news sites (they send
  // X-Frame-Options/frame-ancestors), so this must be a real browser view.
  const beforeTabs = (await chromeA.eval('window.grindstoneTabs.getState()')).tabs.length
  await spy.eval(`(window.grindstone.openUrl('https://example.com/'), 'ok')`)
  // WAIT for the tab to report its URL rather than sleeping a fixed 2.5s and
  // hoping: the page is fetched over the real network, and a slow load made
  // this check fail intermittently on an unrelated change.
  const afterState = await waitFor(
    async () => {
      const s = await chromeA.eval('window.grindstoneTabs.getState()')
      const t = s.tabs.find((x) => x.kind === 'browser')
      return t && /example\.com/.test(t.url ?? '') ? s : null
    },
    'the browser tab to report its URL',
    20000
  ).catch(async () => chromeA.eval('window.grindstoneTabs.getState()'))
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

  // ------------------------------------------------------------ addressing
  // Platform pages are addresses. This types into the REAL address bar and
  // presses a REAL Enter, because the failure was in the classify/route path
  // between the two — a unit test on either end would have passed.
  const typeAddress = (text) => `(() => {
    const el = document.querySelector('input.addr')
    if (!el) return 'no address bar'
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype, 'value').set
    setter.call(el, ${JSON.stringify('')})
    el.dispatchEvent(new Event('input', { bubbles: true }))
    setter.call(el, ${JSON.stringify(text)})
    el.dispatchEvent(new Event('input', { bubbles: true }))
    el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    return 'typed'
  })()`

  const appTab = afterState.tabs.find((t) => t.kind === 'app')
  for (const [typed, wantTitle] of [
    ['settings.gs', 'Settings'],
    ['accounts', 'Accounts'], // a bare page name is an address too
  ]) {
    await chromeA.eval(`(window.grindstoneTabs.activate(${appTab.id}), 'ok')`)
    await sleep(400)
    const typedOk = await chromeA.eval(typeAddress(typed))
    const landed = await waitFor(
      async () => {
        const s = await chromeA.eval('window.grindstoneTabs.getState()')
        const t = s.tabs.find((x) => x.id === appTab.id)
        return t && t.title === wantTitle ? s : null
      },
      `${typed} to open ${wantTitle}`,
      8000
    ).catch(() => null)
    check(
      typedOk === 'typed' && !!landed,
      `addressing: "${typed}" opens ${wantTitle}`,
      landed ? landed.activeUrl : `still on ${typed === 'settings.gs' ? '?' : '?'}`
    )
  }

  // ...and the bar shows the page's own address back, not a blank.
  const finalUrl = (await chromeA.eval('window.grindstoneTabs.getState()')).activeUrl
  check(/\.gs/.test(finalUrl ?? ''), 'addressing: the bar reports a .gs address', finalUrl)

  // ------------------------------------------------------------- favorites
  // The star at the end of the address bar is the only way favorites are
  // born, so this drives it — for all three kinds (ticker page, platform
  // page, live website) — and then checks every surface that consumes the
  // store: the home grid, the wheel (below), and the launcher.
  const starState = async () =>
    JSON.parse(
      await chromeA.eval(`JSON.stringify({
        present: !!document.querySelector('.addr-star'),
        on: !!document.querySelector('.addr-star.on')
      })`)
    )
  const clickStar = () =>
    chromeA.eval(`(document.querySelector('.addr-star')?.click(), 'ok')`)
  const waitStar = (on, why) =>
    waitFor(async () => {
      const s = await starState()
      return s.present && s.on === on ? s : null
    }, why, 8000).catch(() => null)

  const starPage = async (addr, wantTitle) => {
    await chromeA.eval(`(window.grindstoneTabs.activate(${appTab.id}), 'ok')`)
    await sleep(300)
    await chromeA.eval(typeAddress(addr))
    await waitFor(async () => {
      const s = await chromeA.eval('window.grindstoneTabs.getState()')
      const t = s.tabs.find((x) => x.id === appTab.id)
      return t && t.title === wantTitle ? s : null
    }, `${addr} to load before starring`, 8000)
    await waitStar(false, `${addr}: an unstarred page shows the hollow star`)
    await clickStar()
    return waitStar(true, `${addr}: the star to fill after clicking`)
  }

  check(!!(await starPage('spy.gs', 'SPY')), 'star: a ticker page stars from the address bar')
  check(!!(await starPage('backtest.gs', 'Backtest')), 'star: a platform page stars too')

  // The web kind: the example.com tab the browsing block opened. Its icon
  // may honestly be empty (example.com serves no favicon) — the tile falls
  // back to a letter, which is exactly the degradation under test.
  await chromeA.eval(`(window.grindstoneTabs.activate(${browserTab.id}), 'ok')`)
  await sleep(400)
  const webStar = await waitStar(false, 'the browser tab to offer the star')
  await clickStar()
  const webStarred = await waitStar(true, 'the website star to fill')
  check(!!webStar && !!webStarred, 'star: a live website stars from the same control')

  const favList = JSON.parse(
    await spy.eval(
      `window.grindstone.request('GET','/api/favorites')
        .then(r => JSON.stringify(r.body?.favorites ?? []))`
    )
  )
  check(
    favList.length === 3 &&
      new Set(favList.map((f) => f.kind)).size === 3 &&
      favList.some((f) => f.key === 'SPY'),
    'favorites: the store holds all three kinds',
    JSON.stringify(favList.map((f) => `${f.kind}:${f.key}`))
  )

  // The home grid IS the favorites now — the hardcoded app tiles moved to
  // the launcher. Symbol tiles carry a live day-change line.
  await chromeA.eval(`(window.grindstoneTabs.activate(${appTab.id}), 'ok')`)
  await sleep(300)
  await chromeA.eval(typeAddress('home.gs'))
  const grid = await waitFor(
    async () => {
      const s = await spy.eval(`JSON.stringify({
        tiles: document.querySelectorAll('.fav-tile').length,
        text: [...document.querySelectorAll('.fav-tile')].map(t => t.textContent).join('|')
      })`)
      const g = JSON.parse(s)
      return g.tiles === 3 ? g : null
    },
    'the home grid to show the three favorites',
    10000
  ).catch(() => null)
  check(
    !!grid && /SPY/.test(grid.text) && /Backtest/i.test(grid.text),
    'home: the grid shows the starred pages, not hardcoded apps',
    grid ? grid.text.slice(0, 120) : 'no grid'
  )

  // Un-star round trip: the star giveth and the star taketh away.
  await chromeA.eval(typeAddress('spy.gs'))
  await waitStar(true, 'spy.gs to come back starred')
  await clickStar()
  const unstarred = await waitStar(false, 'the star to hollow after removal')
  const nAfterRemove = JSON.parse(
    await spy.eval(
      `window.grindstone.request('GET','/api/favorites')
        .then(r => JSON.stringify((r.body?.favorites ?? []).length))`
    )
  )
  check(!!unstarred && nAfterRemove === 2, 'star: clicking again removes the favorite', `${nAfterRemove} left`)
  await clickStar() // SPY goes back in — the wheel checks below expect it
  await waitStar(true, 'SPY re-starred for the wheel checks')

  // ---------------------------------------------------------- the launcher
  // The provider apps live in a collapsed Google-style panel now. It renders
  // in the OVERLAY view (so it can drop over any tab) — and this click is
  // that view's very first attach in this run, which proves the launcher's
  // ready-handshake replay works cold.
  await chromeA.eval(`(document.querySelector('.apps-btn')?.click(), 'ok')`)
  const launcherTarget = await waitFor(
    async () => (await targets()).find((t) => t.url.includes('mode=wheel')),
    'the overlay view to exist for the launcher'
  )
  const launcherUi = await connect(launcherTarget)
  const panel = await waitFor(
    async () => {
      const s = await launcherUi.eval(`JSON.stringify({
        up: !!document.querySelector('.launcher-panel'),
        tiles: document.querySelectorAll('.launcher-tile').length,
        dead: document.querySelectorAll('.launcher-tile.dead').length,
        titles: [...document.querySelectorAll('.launcher-title')].map(t => t.textContent)
      })`)
      const p = JSON.parse(s)
      return p.up ? p : null
    },
    'the apps panel to open',
    8000
  ).catch(() => null)
  check(
    !!panel && panel.tiles >= 8 && panel.dead >= 1 && panel.titles.includes('Backtest'),
    'launcher: the apps button opens the full provider registry, unbuilt apps dimmed',
    JSON.stringify(panel)
  )
  if (panel) {
    const idx = panel.titles.indexOf('Backtest')
    // Real tiles are buttons with onClick — .click() is the honest dispatch.
    await launcherUi.eval(
      `(document.querySelectorAll('.launcher-tile')[${idx}].click(), 'ok')`
    )
    const landed = await waitFor(
      async () => {
        const gone = await launcherUi.eval(`document.querySelector('.launcher-panel') === null`)
        if (!gone) return null
        const s = await chromeA.eval('window.grindstoneTabs.getState()')
        return /backtest\.gs/.test(s.activeUrl ?? '') ? s.activeUrl : null
      },
      'the Backtest tile to open backtest.gs and close the panel',
      8000
    ).catch(() => null)
    check(!!landed, 'launcher: picking an app opens its page and collapses the panel', landed)
  }
  // Escape closes a reopened panel. The reopen must be CONFIRMED before the
  // Escape — otherwise a panel that never opened makes this pass vacuously.
  await chromeA.eval(`(document.querySelector('.apps-btn')?.click(), 'ok')`)
  const reopened = await waitFor(
    async () => ((await launcherUi.eval(`!!document.querySelector('.launcher-panel')`)) ? 'up' : null),
    'the panel to reopen',
    8000
  ).catch(() => null)
  await launcherUi.eval(
    `(document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})), 'ok')`
  )
  const escClosed = await waitFor(
    async () => ((await launcherUi.eval(`document.querySelector('.launcher-panel') === null`)) ? 'ok' : null),
    'Escape to collapse the panel',
    8000
  ).catch(() => null)
  check(reopened === 'up' && escClosed === 'ok', 'launcher: Escape collapses the reopened panel',
    `reopened=${reopened} escClosed=${escClosed}`)

  // -------------------------------------------------------- gesture wheels
  // Driven through the same wheel:evt channel real input uses, from a real
  // content view — main's state machine, the overlay renderer, and the
  // backend wheels doc all get exercised, not a mock of any of them.
  const sendWheel = (target, kind, x, y) =>
    target.eval(`(window.grindstone.wheelEvt(${JSON.stringify(kind)}, ${x}, ${y}), 'ok')`)

  // CLICK mode: press, idle, release without travel → wheel stays with hub.
  await sendWheel(spy, 'down', 420, 320)
  await sleep(350)
  await sendWheel(spy, 'up', 420, 320)
  const wheelTarget = await waitFor(
    async () => (await targets()).find((t) => t.url.includes('mode=wheel')),
    'the wheel overlay view'
  )
  const wheelUi = await connect(wheelTarget)
  const clickState = await waitFor(
    async () => {
      const s = await wheelUi.eval(
        `JSON.stringify({
           segs: document.querySelectorAll('.wf-seg').length,
           mode: document.querySelector('.wheel-face')?.dataset.mode ?? null,
           id: document.querySelector('.wheel-face')?.dataset.wheel ?? null,
           hub: !!document.querySelector('.wf-hub.lockable')
         })`
      )
      const st = JSON.parse(s)
      return st.mode === 'click' ? st : null
    },
    'the wheel to reach click mode',
    8000
  ).catch(() => null)
  check(
    !!clickState && clickState.segs === 8 && clickState.id === 'main' && clickState.hub,
    'wheel: right-click spawns the 8-segment main wheel with the lock hub',
    JSON.stringify(clickState)
  )

  // Left-clicking the Charts segment (SW, index 5 — v2 replaced the SPY
  // ticker with the multi-chart page) navigates there and the wheel closes.
  await wheelUi.eval(
    `(document.querySelector('[data-seg="5"]')
        .dispatchEvent(new MouseEvent('mousedown', {bubbles: true, button: 0})), 'ok')`
  )
  const afterCharts = await waitFor(
    async () => {
      const gone = await wheelUi.eval(`document.querySelector('.wheel-stage') === null`)
      if (!gone) return null
      const st = await chromeA.eval('window.grindstoneTabs.getState()')
      const active = st.tabs.find((t) => t.id === st.activeId)
      return active && active.title === 'Charts' ? active : null
    },
    'the Charts segment to open charts.gs and close the wheel',
    8000
  ).catch(() => null)
  check(!!afterCharts, 'wheel: the Charts segment opens the multi-chart page and closes the wheel')

  // HOLD mode: press, drag due WEST (segment 6 = Favorites wheel nav),
  // release → switches wheel and STAYS OPEN in click mode (the spec's
  // "after a wheel nav while holding, enter left-click mode"). The wheel is
  // DYNAMIC: 3 starred favorites + the Main nav = 4 segments, and the
  // symbol favorite renders as a ticker segment, page/web as links.
  await sendWheel(spy, 'down', 420, 320)
  await sleep(350)
  for (const dx of [40, 90, 150]) await sendWheel(spy, 'move', 420 - dx, 320)
  await sendWheel(spy, 'up', 420 - 170, 320)
  const holdState = await waitFor(
    async () => {
      const s = await wheelUi.eval(
        `JSON.stringify({
           id: document.querySelector('.wheel-face')?.dataset.wheel ?? null,
           mode: document.querySelector('.wheel-face')?.dataset.mode ?? null,
           segs: document.querySelectorAll('.wf-seg').length,
           text: [...document.querySelectorAll('.wf-seg')].map(t => t.textContent).join('|')
         })`
      )
      const st = JSON.parse(s)
      return st.id === 'favorites' && st.mode === 'click' ? st : null
    },
    'hold-drag west to switch to the Favorites wheel and stay open',
    8000
  ).catch(() => null)
  check(
    !!holdState && holdState.segs === 4 &&
      /SPY/.test(holdState.text) && /Backtest/i.test(holdState.text),
    'wheel: hold-drag onto a wheel-nav switches wheels and stays open',
    JSON.stringify(holdState).slice(0, 200)
  )

  // The hub locks THIS wheel as the default; a fresh spawn opens it first.
  await wheelUi.eval(
    `(document.querySelector('.wf-hub')
        .dispatchEvent(new MouseEvent('mousedown', {bubbles: true, button: 0})), 'ok')`
  )
  const lockedCfg = await waitFor(
    async () => {
      const r = await spy.eval(
        `window.grindstone.request('GET','/api/wheels').then(r => r.body?.config?.locked ?? null)`
      )
      return r === 'favorites' ? r : null
    },
    'the lock to persist',
    8000
  ).catch(() => null)
  check(lockedCfg === 'favorites', 'wheel: the center hub locks the shown wheel as default')

  // Close (left click outside), respawn: the locked wheel comes up first.
  await wheelUi.eval(
    `(document.querySelector('.wheel-stage')
        .dispatchEvent(new MouseEvent('mousedown', {bubbles: true, button: 0})), 'ok')`
  )
  await sleep(250)
  await sendWheel(spy, 'down', 420, 320)
  await sleep(350)
  await sendWheel(spy, 'up', 420, 320)
  const respawn = await waitFor(
    async () => {
      const id = await wheelUi.eval(
        `document.querySelector('.wheel-face')?.dataset.wheel ?? null`
      )
      return id === 'favorites' ? id : null
    },
    'the locked wheel to spawn first',
    8000
  ).catch(() => null)
  check(respawn === 'favorites', 'wheel: a locked wheel is the new default on spawn')

  // Unlock reverts to the true default; clean up for whatever runs next.
  await wheelUi.eval(
    `(document.querySelector('.wf-hub')
        .dispatchEvent(new MouseEvent('mousedown', {bubbles: true, button: 0})), 'ok')`
  )
  const unlocked = await waitFor(
    async () => {
      // JSON.stringify to tell "locked: null" (unlocked — what we want)
      // apart from "no answer at all" (undefined) — ?? would eat the null.
      const r = await spy.eval(
        `window.grindstone.request('GET','/api/wheels').then(r => JSON.stringify(r.body?.config?.locked))`
      )
      return r === 'null' ? 'ok' : null
    },
    'the unlock to persist',
    8000
  ).catch(() => null)
  check(unlocked === 'ok', 'wheel: unlocking the hub reverts to the true default')
  await wheelUi.eval(
    `(document.querySelector('.wheel-stage')
        .dispatchEvent(new MouseEvent('mousedown', {bubbles: true, button: 0})), 'ok')`
  )
  await sleep(250)

  // ------------------------------------------------- chart-context wheel
  // Right-clicking ANY chart spawns the CHART wheel; off-chart spawns the
  // default. The ctx rides the same channel real input uses.
  await spy.eval(
    `(window.grindstone.wheelEvt('down', 420, 320,
        {context: 'chart', symbols: ['SPY'], indicators: ['vol']}), 'ok')`
  )
  await sleep(350)
  await spy.eval(`(window.grindstone.wheelEvt('up', 420, 320), 'ok')`)
  const chartWheel = await waitFor(
    async () => {
      const id = await wheelUi.eval(
        `document.querySelector('.wheel-face')?.dataset.wheel ?? null`
      )
      return id === 'chart' ? id : null
    },
    'the chart wheel to spawn over a chart',
    8000
  ).catch(() => null)
  check(chartWheel === 'chart', 'wheel: right-clicking a chart spawns the chart wheel')

  // Hold-drag EAST onto Indicators -> the dynamic chart-ind wheel, marked
  // with the clicked chart's live state (vol was on).
  await wheelUi.eval(
    `(document.querySelector('.wheel-stage')
        .dispatchEvent(new MouseEvent('mousedown', {bubbles: true, button: 0})), 'ok')`
  )
  await sleep(250)
  await spy.eval(
    `(window.grindstone.wheelEvt('down', 420, 320,
        {context: 'chart', symbols: ['SPY'], indicators: ['vol']}), 'ok')`
  )
  await sleep(350)
  for (const dx of [40, 90, 150]) {
    await spy.eval(`(window.grindstone.wheelEvt('move', ${420 + dx}, 320), 'ok')`)
  }
  await spy.eval(`(window.grindstone.wheelEvt('up', ${420 + 170}, 320), 'ok')`)
  const indWheel = await waitFor(
    async () => {
      const s = await wheelUi.eval(
        `JSON.stringify({
           id: document.querySelector('.wheel-face')?.dataset.wheel ?? null,
           on: [...document.querySelectorAll('.wf-label')]
                 .some(t => (t.textContent ?? '').includes('●'))
         })`
      )
      const st = JSON.parse(s)
      return st.id === 'chart-ind' && st.on ? st : null
    },
    'the dynamic indicator wheel with live on/off marks',
    8000
  ).catch(() => null)
  check(!!indWheel, 'wheel: chart-ind builds from the clicked chart state', JSON.stringify(indWheel))
  await wheelUi.eval(
    `(document.querySelector('.wheel-stage')
        .dispatchEvent(new MouseEvent('mousedown', {bubbles: true, button: 0})), 'ok')`
  )

  // ------------------------------------------------------------ split view
  // Driven through the same IPC the native menu handlers call.
  const stBefore = await chromeA.eval('window.grindstoneTabs.getState()')
  const [tabA, tabB] = stBefore.tabs
  await chromeA.eval(`(window.grindstoneTabs.splitWith(${tabA.id}, ${tabB.id}), 'ok')`)
  const splitState = await waitFor(
    async () => {
      const s = await chromeA.eval('window.grindstoneTabs.getState()')
      return s.split && s.split.aId === tabA.id && s.split.bId === tabB.id ? s : null
    },
    'the split to activate',
    8000
  ).catch(() => null)
  check(!!splitState, 'split: two tabs pair side-by-side',
    splitState ? `ratio=${splitState.split.ratio}` : '')
  const mates = await chromeA.eval(`document.querySelectorAll('.strip-tab.split-mate').length`)
  check(mates === 2, 'split: the strip marks both pair members', `marked=${mates}`)
  await chromeA.eval(`(window.grindstoneTabs.closeSplit(), 'ok')`)
  const unsplit = await waitFor(
    async () => {
      const s = await chromeA.eval('window.grindstoneTabs.getState()')
      return s.split === null ? 'ok' : null
    },
    'the split to dissolve',
    8000
  ).catch(() => null)
  check(unsplit === 'ok', 'split: close split restores a single pane')

  // ------------------------------------------- chart tools, driven by hand
  // Kade: "currently it's very clunky to use the chart tools — do proper
  // testing." Everything below is TRUSTED CDP input on the live SPY chart:
  // real toolbar clicks, real canvas clicks, assertions on the same
  // data-draw-* attrs the page maintains for exactly this purpose.
  // Navigate the active tab to SPY EXPLICITLY — earlier tests reorder and
  // renavigate tabs, so hunting a tab by title is exactly the flake this
  // section is not allowed to have.
  await chromeA.eval(typeAddress('spy.gs'))
  let chartView = null
  const chartRect = await waitFor(
    async () => {
      for (const t of (await targets()).filter((x) => x.url.includes('mode=content'))) {
        const c = await connect(t)
        const r = await c.eval(
          `(() => { // the SYMBOL page specifically — the multi-chart page in a
             // background tab also carries data-chart-symbols="SPY" and once
             // stole this scan, sending every click into a detached view.
             if (document.querySelector('.page-head h1')?.textContent !== 'SPY') return null;
             const el = document.querySelector('[data-draw-tool]');
             if (!el || !el.querySelector('canvas')) return null;
             const b = el.getBoundingClientRect();
             return b.width > 300 ? { x: b.x, y: b.y, w: b.width, h: b.height } : null })()`
        )
        if (r) {
          chartView = c
          return r
        }
      }
      return null
    },
    'the SPY chart to render bars',
    30000
  )
  const attr = (name) =>
    chartView.eval(`document.querySelector('[data-draw-tool]')?.getAttribute('${name}') ?? null`)
  const toolBtn = (title) =>
    chartView.eval(
      `(() => { const b = [...document.querySelectorAll('button')]
          .find(x => (x.title ?? '').startsWith(${JSON.stringify(title)}));
        if (!b) return 'missing'; b.click(); return 'ok' })()`
    )
  /** A hand hunting a thin line: try the aim point, then small vertical/
   *  horizontal nudges until the target attr changes (the engine's 8px
   *  tolerance + hover halos make this exactly how humans hit lines). */
  const fanClick = async (fx, fy, attrName, want) => {
    for (const [dx, dy] of [[0, 0], [0, -8], [0, 8], [-8, 0], [8, 0], [0, -16], [0, 16]]) {
      const r = await chartView.eval(
        `(() => { const el = document.querySelector('[data-draw-tool]');
           el.scrollIntoView({ block: 'center' });
           const b = el.getBoundingClientRect();
           return { x: b.x, y: b.y, w: b.width, h: b.height } })()`
      )
      await sleep(100)
      await chartView.click(r.x + r.w * fx + dx, r.y + r.h * fy + dy)
      await sleep(350)
      if ((await attr(attrName)) === want) return true
    }
    return false
  }

  // Click through a FRESH rect every time: the metrics card above the chart
  // populates asynchronously and shoves the chart down after the first
  // measurement — a cached rect quietly aims clicks at the toolbar.
  const chartClick = async (fx, fy) => {
    const r = await chartView.eval(
      `(() => { const el = document.querySelector('[data-draw-tool]');
         // The metrics grid above grows after load and pushes the chart
         // below the window fold — a click past the viewport bottom is
         // silently dropped by the input pipeline. Center it first.
         el.scrollIntoView({ block: 'center' });
         const b = el.getBoundingClientRect();
         return { x: b.x, y: b.y, w: b.width, h: b.height } })()`
    )
    await sleep(120) // scroll settles before coordinates are used
    const rr = await chartView.eval(
      `(() => { const b = document.querySelector('[data-draw-tool]').getBoundingClientRect();
         return { x: b.x, y: b.y, w: b.width, h: b.height } })()`
    )
    await chartView.click(rr.x + rr.w * fx, rr.y + rr.h * fy)
  }
  const cx = (fx) => chartRect.x + chartRect.w * fx
  const cy = (fy) => chartRect.y + chartRect.h * fy

  /** A real drag: streamed approach, press, several HELD moves, release.
   *  `buttons: 1` on the moves is what makes them a drag — without it the page
   *  sees the button as up and nothing tracks. Same streamed approach as
   *  click() for the same reason: lightweight-charts derives coordinates from
   *  its crosshair, and a press with no prior move is point-less and dropped. */
  const chartDrag = async (fx1, fy1, fx2, fy2) => {
    const r = await chartView.eval(
      `(() => { const el = document.querySelector('[data-draw-tool]');
         el.scrollIntoView({ block: 'center' });
         const b = el.getBoundingClientRect();
         return { x: b.x, y: b.y, w: b.width, h: b.height } })()`
    )
    await sleep(120)
    const x1 = Math.round(r.x + r.w * fx1), y1 = Math.round(r.y + r.h * fy1)
    const x2 = Math.round(r.x + r.w * fx2), y2 = Math.round(r.y + r.h * fy2)
    for (const dx of [-24, -9, 0]) {
      await chartView.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: x1 + dx, y: y1 })
      await sleep(30)
    }
    await chartView.send('Input.dispatchMouseEvent',
      { type: 'mousePressed', x: x1, y: y1, button: 'left', buttons: 1, clickCount: 1 })
    await sleep(80)
    for (let i = 1; i <= 6; i++) {
      await chartView.send('Input.dispatchMouseEvent', {
        type: 'mouseMoved', button: 'left', buttons: 1,
        x: Math.round(x1 + ((x2 - x1) * i) / 6),
        y: Math.round(y1 + ((y2 - y1) * i) / 6),
      })
      await sleep(40)
    }
    await chartView.send('Input.dispatchMouseEvent',
      { type: 'mouseReleased', x: x2, y: y2, button: 'left', buttons: 0, clickCount: 1 })
    await sleep(350)
  }
  /** The exact-value editor's first field, as a number. It is the only place
   *  the engine's stored geometry is visible to the DOM. */
  const editorValue = () =>
    chartView.eval(
      `(() => { const i = document.querySelector('.draw-editor-float input');
         return i ? parseFloat(i.value) : null })()`
    )

  // Candle depth: the default setting is ALL history — a keyless profile
  // gets Yahoo 'max', which for SPY is decades of dailies, not one year.
  const allBars = await chartView.eval(
    `parseInt(document.querySelector('.chart-source')?.textContent ?? '0', 10)`
  )
  check(allBars > 1000, 'candles: default "all" loads full history', `${allBars} bars`)

  /** Place-with-verify: clicks can lose the crosshair race on very dense
   *  charts even with the streamed approach, so placements retry until the
   *  drawing count reflects them — like a human clicking again when nothing
   *  appeared. */
  const placeOne = async (fx, fy, wantCount) => {
    for (let i = 0; i < 3; i++) {
      await chartClick(fx, fy)
      await sleep(350)
      if ((await attr('data-draw-count')) === wantCount) return true
    }
    return (await attr('data-draw-count')) === wantCount
  }
  const placeTwo = async (fx1, fy1, fx2, fy2, wantCount) => {
    for (let i = 0; i < 3; i++) {
      await chartClick(fx1, fy1)
      await sleep(300)
      await chartClick(fx2, fy2)
      await sleep(400)
      if ((await attr('data-draw-count')) === wantCount) return true
      // a half-placed anchor would corrupt the next attempt — cancel it.
      // Escape now also DISARMS the tool as its last rung (the user asked for
      // "escape cancels the tool you are using"), and this loop exists for the
      // case where BOTH clicks were dropped — no pending, no selection, so
      // that last rung is exactly what fires. Re-arm or every remaining retry
      // clicks into a disarmed engine.
      await chartView.eval(
        `document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))`
      )
      await sleep(200)
      await toolBtn('Trend line') // both call sites arm the trend tool
      await sleep(150)
    }
    return (await attr('data-draw-count')) === wantCount
  }

  // Trend line: arm from the toolbar, two real clicks, one drawing.
  check((await toolBtn('Trend line')) === 'ok', 'tools: the Line button exists')
  await waitFor(async () => (await attr('data-draw-tool')) === 'trend', 'trend armed', 5000)
  const drew = await placeTwo(0.30, 0.70, 0.60, 0.35, '1')
  check(drew, 'tools: two real clicks place a trend line')

  // Select it mid-span with a PLAIN left click — Pointer is the default tool
  // and picking is what it does, so this also proves the trend tool disarms
  // rather than drawing a second line on top of the first.
  await toolBtn('Pointer')
  await waitFor(async () => (await attr('data-draw-tool')) === 'pointer', 'pointer armed', 5000)
  let selHit = await fanClick(0.45, 0.525, 'data-draw-selected', '1')
  if (!selHit) {
    // Measured 1 failure in 6 runs on this line, cause not identified — all
    // seven fan offsets missed on an 8435-bar chart, the same crosshair race
    // placeTwo already retries for. One retry rather than a wider fan, because
    // a miss here is not cosmetic: Delete with nothing selected ARMS
    // click-to-delete instead of deleting, so the single flake also failed
    // both trim checks and left 10 drawings on the chart.
    await sleep(500)
    selHit = await fanClick(0.45, 0.525, 'data-draw-selected', '1')
  }
  const selected = await waitFor(
    async () => {
      const n = await attr('data-draw-selected')
      if (n !== '1') return null
      const fields = await chartView.eval(
        `document.querySelectorAll('.draw-editor-float input').length`
      )
      return fields >= 4 ? { fields } : null
    },
    'selection + the 4-field endpoint editor',
    6000
  ).catch(() => null)
  check(!!selected, 'tools: select opens the exact-value editor',
    selected ? `${selected.fields} fields` : `fanHit=${selHit}`)

  // Delete the selection from the toolbar.
  await toolBtn('Delete')
  const deleted = await waitFor(
    async () => ((await attr('data-draw-count')) === '0' ? 'ok' : null),
    'the selection to delete',
    6000
  ).catch(() => null)
  check(deleted === 'ok', 'tools: Delete removes the selected drawing')

  // ---- drag to move -------------------------------------------------------
  // Until now a drawing could only be moved by typing coordinates into the
  // editor. Uses an h-line because its stored price is exactly what the editor
  // shows, so "did it move, and the right way" is one number, not a guess.
  await toolBtn('Horizontal price line')
  const placedDrag = await placeOne(0.5, 0.42, '1')
  await toolBtn('Pointer')
  await waitFor(async () => (await attr('data-draw-tool')) === 'pointer', 'pointer armed', 5000)
  const grabbed = await fanClick(0.5, 0.42, 'data-draw-selected', '1')
  const priceBefore = await editorValue()
  check(placedDrag && grabbed && typeof priceBefore === 'number',
    'tools: an h-line is placed and picked, and the editor shows its price',
    `placed=${placedDrag} picked=${grabbed} price=${priceBefore}`)

  // Drag DOWN the chart, which on a price axis means a LOWER price.
  await chartDrag(0.5, 0.42, 0.5, 0.62)
  const priceAfter = await editorValue()
  check(
    typeof priceAfter === 'number' && typeof priceBefore === 'number' &&
      priceAfter < priceBefore - 0.01,
    'tools: dragging a drawing moves it, and downward means a lower price',
    `before=${priceBefore} after=${priceAfter}`
  )
  // The library fires a click on the mouseup that ends a drag. If that click
  // is not swallowed it lands at the DROP point: on empty chart it would clear
  // the selection, and with a tool armed it would place geometry there.
  const afterDragCount = await attr('data-draw-count')
  const afterDragSel = await attr('data-draw-selected')
  check(afterDragCount === '1' && afterDragSel === '1',
    'tools: the click ending a drag is swallowed (nothing placed, nothing deselected)',
    `count=${afterDragCount} selected=${afterDragSel}`)

  // Leave the chart empty for the trim block, which counts from zero.
  await toolBtn('Delete')
  await waitFor(async () => ((await attr('data-draw-count')) === '0' ? 'ok' : null),
    'the dragged h-line to delete', 6000).catch(() => null)

  // H-line + a crossing trend, then TRIM the trend's lower-left span:
  // the trend splits at the intersection (clicked span dies), the h-line
  // donor splits in two -> 3 drawings remain.
  await toolBtn('Horizontal price line')
  const placedH = await placeOne(0.5, 0.5, '1')
  await toolBtn('Trend line')
  const placedT = await placeTwo(0.30, 0.70, 0.70, 0.30, '2')
  check(placedH && placedT, 'tools: h-line + crossing trend placed for trim')
  await toolBtn('Trim')
  const trimHit = await fanClick(0.35, 0.65, 'data-draw-count', '3') // lower-left span
  const trimmed = await waitFor(
    async () => {
      const n = await attr('data-draw-count')
      return n === '3' ? 'ok' : null
    },
    'trim to cut back to the intersection',
    6000
  ).catch(() => null)
  check(trimmed === 'ok', 'tools: trim removes only the clicked span (SolidWorks-style)',
    `count=${await attr('data-draw-count')} fanHit=${trimHit}`)

  // Measure between two candles: one annotation, then clear it.
  await toolBtn('Measure')
  let measured = false
  for (let i = 0; i < 3 && !measured; i++) {
    await chartClick(0.35, 0.45)
    await sleep(300)
    await chartClick(0.65, 0.55)
    await sleep(400)
    measured = (await attr('data-measure-count')) === '1'
    if (!measured) {
      // See placeTwo: Escape's last rung disarms the tool, so re-arm Measure
      // before retrying or the remaining attempts click into a dead engine.
      await chartView.eval(
        `document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))`
      )
      await sleep(200)
      await toolBtn('Measure')
      await sleep(150)
    }
  }
  check(measured, 'tools: measure connects two real points')

  // ---- can a PLACED measure be picked? -----------------------------------
  // Trend-line selection is proven above; measure selection has never been
  // exercised, which is exactly why "measures are not clickable" survived a
  // green gate (selftest's chart-selection check greps ChartDraw.ts for the
  // widened hitAny and passes on wiring that reads correct).
  //
  // The engine stores a measure's hot zone as the chip's {left,top,w,h}
  // computed against the PANE, then positions that same chip inside
  // this.labels using those numbers. So the zone is only where the user sees
  // the chip if labels shares the canvas origin. Measure that first: if it is
  // offset, every click misses by exactly this much and the click test below
  // cannot tell you why on its own.
  const geom = await chartView.eval(
    `(() => {
       const host = document.querySelector('[data-draw-tool]');
       const chip = document.querySelector('.cd-chip');
       const cv   = host && host.querySelector('canvas');
       const lbl  = chip && chip.parentElement;
       const r = (e) => { if (!e) return null; const b = e.getBoundingClientRect();
         return { x: Math.round(b.x), y: Math.round(b.y),
                  w: Math.round(b.width), h: Math.round(b.height) }; };
       return { host: r(host), canvas: r(cv), labels: r(lbl), chip: r(chip),
                zones: document.querySelectorAll('.cd-chip').length,
                styled: chip ? chip.style.left + ',' + chip.style.top : null };
     })()`
  )
  check(!!geom.chip, 'tools: a placed measure renders a chip in the DOM',
    JSON.stringify(geom))

  // NOT a chip-vs-hot-zone check: a measure's hot zone IS the chip's own rect
  // (chip() returns the numbers it just wrote to style.left/top and
  // renderMeasure spreads that same object into the zone list), so those two
  // can never disagree and comparing them would always pass. What is worth
  // asserting is that the label layer sits on the canvas origin, because the
  // zone is stored in PANE coordinates while the click arrives in the same
  // space - a skew here would offset every pick.
  const originSkew = geom.labels && geom.canvas
    ? { dx: geom.labels.x - geom.canvas.x, dy: geom.labels.y - geom.canvas.y }
    : null
  check(originSkew !== null && originSkew.dx === 0 && originSkew.dy === 0,
    'tools: the label layer sits on the canvas origin (pane and click coords agree)',
    `skew=${JSON.stringify(originSkew)} labels=${JSON.stringify(geom.labels)} canvas=${JSON.stringify(geom.canvas)}`)

  // Now the click the user actually makes: NOTHING armed. Pointer is the
  // default and picks on plain left-click, which is the whole point — there
  // is no Select tool to forget to arm any more. Aim at the chip's own centre
  // in page coordinates rather than a chart fraction, so a miss cannot be
  // blamed on aiming at the wrong place.
  await toolBtn('Pointer')
  await sleep(200)
  // data-draw-selected is a COUNT across every kind, and three trimmed
  // drawings are still on screen from the trim block - two of them crossing
  // near the chip. Asserting "selected === 1" would therefore go green if the
  // click picked a LINE instead of the measure, i.e. exactly when measure
  // picking is broken. Ask the DOM which chip carries cd-sel instead: only
  // renderMeasure/renderPin apply it, so it can only mean a measure was hit.
  const measureSelected = () =>
    chartView.eval(`document.querySelectorAll('.cd-chip.cd-sel').length`)
  let measurePicked = null
  if (geom.chip) {
    for (const [dx, dy] of [[0, 0], [0, -6], [0, 6], [-10, 0], [10, 0]]) {
      await chartView.click(geom.chip.x + geom.chip.w / 2 + dx, geom.chip.y + geom.chip.h / 2 + dy)
      await sleep(350)
      if ((await measureSelected()) >= 1) { measurePicked = `chip+${dx},${dy}`; break }
    }
  }
  check(!!measurePicked, 'tools: a placed measure is selected by a PLAIN left click',
    measurePicked ?? `chip=${JSON.stringify(geom.chip)} cd-sel=${await measureSelected()} selected=${await attr('data-draw-selected')}`)

  // Plain click REPLACES: clicking a drawing after the measure must leave one
  // object held, not two. This is what stops the editor showing one object
  // while Delete quietly takes several.
  await fanClick(0.5, 0.5, 'data-draw-selected', '1')
  const afterOther = await attr('data-draw-selected')
  check(afterOther === '1', 'tools: a plain click replaces the selection rather than adding',
    `selected=${afterOther} cd-sel=${await measureSelected()}`)
  await chartView.eval(
    `document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))`
  )
  await sleep(150)

  const clearedBtn = await toolBtn('Clear all measurements')
  const cleared = await waitFor(
    async () => ((await attr('data-measure-count')) === '0' ? 'ok' : null),
    'measures cleared',
    8000
  ).catch(async () => `btn=${clearedBtn} count=${await attr('data-measure-count')}`)
  check(cleared === 'ok', 'tools: clear-measures wipes the annotations', String(cleared))

  // ---- axis-locked dimensions --------------------------------------------
  // Two h-lines measured against each other used to draw a DIAGONAL between
  // wherever the two clicks landed, and print a time row describing only the
  // clicks. Both are artefacts of where you clicked, not facts about the
  // lines. It should be a vertical dimension: two prices at one time.
  await toolBtn('Clear every drawing')
  // Assert the clean slate rather than assuming it. toolBtn returns the STRING
  // 'missing' for a title that matches nothing, so a mistyped button is a
  // silent no-op: the trim block's drawings stay, placeOne never sees its
  // expected count, retries three times, and the real cause surfaces as a
  // baffling pile of nine drawings several checks later.
  const swept = await waitFor(
    async () => ((await attr('data-draw-count')) === '0' ? 'ok' : null),
    'the chart to be swept before the dimension block', 6000
  ).catch(async () => `count=${await attr('data-draw-count')}`)
  check(swept === 'ok', 'tools: the dimension block starts from a clean chart', String(swept))

  await toolBtn('Horizontal price line')
  const h1 = await placeOne(0.35, 0.35, '1')
  const h2 = await placeOne(0.35, 0.60, '2')
  await toolBtn('Measure')
  await sleep(150)
  await chartClick(0.30, 0.35)
  await sleep(300)
  await chartClick(0.70, 0.60)
  await sleep(400)
  // cd-ax-time can ONLY come from renderMeasure seeing place.axis === 'time',
  // so it tells "locked vertical" apart from "drew a diagonal". A selection
  // count could not.
  const locked = await chartView.eval(
    `(() => { const c = document.querySelector('.cd-chip.cd-ax-time');
       return c ? { rows: c.childElementCount, x: Math.round(c.getBoundingClientRect().x) } : null })()`
  )
  check(h1 && h2 && locked !== null,
    'tools: two h-lines measure as a VERTICAL dimension, not a diagonal',
    `h1=${h1} h2=${h2} locked=${JSON.stringify(locked)}`)
  // One row: the two ends share a time, so a bar-count row would describe the
  // dimension's own placement rather than anything measured.
  check(locked !== null && locked.rows === 1,
    'tools: a locked dimension prints only the question it answers',
    `rows=${locked ? locked.rows : 'n/a'}`)

  // Drag the dimension sideways: its TIME moves, the h-lines do not.
  await toolBtn('Pointer')
  await waitFor(async () => (await attr('data-draw-tool')) === 'pointer', 'pointer armed', 5000)
  const chipAt = await chartView.eval(
    `(() => { const c = document.querySelector('.cd-chip.cd-ax-time'); if (!c) return null;
       const b = c.getBoundingClientRect();
       const h = document.querySelector('[data-draw-tool]').getBoundingClientRect();
       return { fx: (b.x + b.width / 2 - h.x) / h.width, fy: (b.y + b.height / 2 - h.y) / h.height,
                x: Math.round(b.x) } })()`
  )
  if (chipAt) await chartDrag(chipAt.fx, chipAt.fy, chipAt.fx + 0.18, chipAt.fy)
  const afterDim = await chartView.eval(
    `(() => { const c = document.querySelector('.cd-chip.cd-ax-time');
       return c ? Math.round(c.getBoundingClientRect().x) : null })()`
  )
  check(chipAt !== null && afterDim !== null && Math.abs(afterDim - chipAt.x) > 30,
    'tools: dragging a dimension moves the dimension',
    `before=${chipAt ? chipAt.x : 'n/a'} after=${afterDim}`)
  // Still vertical, and the measured lines are untouched — a dimension drag
  // moves the dimension, never what it measures.
  const stillLocked = await chartView.eval(`document.querySelectorAll('.cd-chip.cd-ax-time').length`)
  const intact = await attr('data-draw-count')
  check(stillLocked === 1 && intact === '2',
    'tools: the drag kept it vertical and left the measured lines alone',
    `locked=${stillLocked} drawings=${intact}`)

  await toolBtn('Clear all measurements')
  await toolBtn('Clear every drawing')
  await sleep(250)

  // Visibility toggle reflects into the wheel-context flags.
  await toolBtn('Hide drawings')
  const visFlag = await waitFor(
    async () => (((await attr('data-chart-flags')) ?? '').includes('drawhidden') ? 'ok' : null),
    'the drawings-hidden flag',
    5000
  ).catch(() => null)
  check(visFlag === 'ok', 'tools: drawings-visibility toggle sets the context flag')
  await toolBtn('Show drawings')

  // The chart wheel's Timeframe companion marks the live timeframe.
  await chartView.eval(
    `(window.grindstone.wheelEvt('down', 420, 320,
        {context: 'chart', symbols: ['SPY'], indicators: ['vol'], timeframe: '1Day'}), 'ok')`
  )
  await sleep(350)
  for (const d of [40, 90, 140]) {
    await chartView.eval(`(window.grindstone.wheelEvt('move', ${420 - d}, ${320 + d}), 'ok')`)
  }
  await chartView.eval(`(window.grindstone.wheelEvt('up', 260, 480), 'ok')`) // SW = Timeframe
  const tfWheel = await waitFor(
    async () => {
      const s = await wheelUi.eval(
        `JSON.stringify({
           id: document.querySelector('.wheel-face')?.dataset.wheel ?? null,
           mark: [...document.querySelectorAll('.wf-label')]
                   .some(t => (t.textContent ?? '').includes('● 1Day'))
         })`
      )
      const st = JSON.parse(s)
      return st.id === 'chart-tf' && st.mark ? st : null
    },
    'the timeframe wheel with the live mark',
    8000
  ).catch(() => null)
  check(!!tfWheel, 'wheel: chart-tf marks the chart´s current timeframe')
  await wheelUi.eval(
    `(document.querySelector('.wheel-stage')
        .dispatchEvent(new MouseEvent('mousedown', {bubbles: true, button: 0})), 'ok')`
  )
  await sleep(250)

  // ---------------------------------------------- candle-depth setting cap
  // Set 200 candles, revisit the symbol page: the chart must load exactly
  // the cap, then restore 'all' so later tests see full history again.
  await chartView.eval(
    `window.grindstone.request('PUT','/api/settings',{chart_candles:'200'}).then(r=>r.status)`
  )
  await chromeA.eval(typeAddress('home.gs'))
  await sleep(600)
  await chromeA.eval(typeAddress('spy.gs'))
  const capped = await waitFor(
    async () => {
      const n = await chartView.eval(
        `parseInt(document.querySelector('.chart-source')?.textContent ?? '0', 10)`
      )
      return n === 200 ? 'ok' : null
    },
    'the 200-candle cap to apply',
    20000
  ).catch(async () =>
    `got=${await chartView.eval(
      `document.querySelector('.chart-source')?.textContent ?? 'none'`
    )}`)
  check(capped === 'ok', 'candles: the setting caps chart history', String(capped))
  await chartView.eval(
    `window.grindstone.request('PUT','/api/settings',{chart_candles:'all'}).then(r=>r.status)`
  )

  // -------------------------------------------- fixed tab actions (strip)
  // New-tab lives OUTSIDE the tab scroller now — click it, then bounce
  // between the two most recent tabs with Previous-tab, twice.
  const beforeNew = (await chromeA.eval('window.grindstoneTabs.getState()')).tabs.length
  await chromeA.eval(
    `([...document.querySelectorAll('.strip-actions .strip-btn')]
        .find(b => b.title === 'New tab').click(), 'ok')`
  )
  const grew = await waitFor(
    async () => {
      const s = await chromeA.eval('window.grindstoneTabs.getState()')
      return s.tabs.length === beforeNew + 1 ? s : null
    },
    'the fixed New-tab button to open a tab',
    6000
  ).catch(() => null)
  check(!!grew, 'tabs: the always-available New-tab button works',
    grew ? `tabs=${grew.tabs.length}` : '')
  const activeNow = grew?.activeId
  await chromeA.eval(
    `([...document.querySelectorAll('.strip-actions .strip-btn')]
        .find(b => b.title.startsWith('Previous tab')).click(), 'ok')`
  )
  const bounced = await waitFor(
    async () => {
      const s = await chromeA.eval('window.grindstoneTabs.getState()')
      return s.activeId !== activeNow ? s.activeId : null
    },
    'Previous-tab to jump back',
    6000
  ).catch(() => null)
  check(bounced !== null, 'tabs: Previous-tab returns to the last active tab')
  await chromeA.eval(
    `([...document.querySelectorAll('.strip-actions .strip-btn')]
        .find(b => b.title.startsWith('Previous tab')).click(), 'ok')`
  )
  const bouncedBack = await waitFor(
    async () => {
      const s = await chromeA.eval('window.grindstoneTabs.getState()')
      return s.activeId === activeNow ? 'ok' : null
    },
    'Previous-tab to bounce back again',
    6000
  ).catch(() => null)
  check(bouncedBack === 'ok', 'tabs: Previous-tab twice bounces between the same two tabs')

  // ------------------------------------------------------------- charts.gs
  // One retry: typing into the omnibox immediately after a burst of strip
  // state pushes has flaked exactly once — a human would just retype.
  await chromeA.eval(typeAddress('charts.gs'))
  const firstTry = await waitFor(
    async () => {
      const s = await chromeA.eval('window.grindstoneTabs.getState()')
      const t = s.tabs.find((x) => x.id === s.activeId)
      return t && t.title === 'Charts' ? 'ok' : null
    },
    'charts.gs (first attempt)',
    6000
  ).catch(() => null)
  if (firstTry === null) {
    await sleep(800)
    await chromeA.eval(typeAddress('charts.gs'))
  }
  const chartsPage = await waitFor(
    async () => {
      const s = await chromeA.eval('window.grindstoneTabs.getState()')
      const t = s.tabs.find((x) => x.id === s.activeId)
      return t && t.title === 'Charts' ? s.activeUrl : null
    },
    'charts.gs to open the multi-chart page',
    8000
  ).catch(async () => {
    const s2 = await chromeA.eval('window.grindstoneTabs.getState()')
    const act = s2.tabs.find((x) => x.id === s2.activeId)
    console.log('      charts.gs diag:', JSON.stringify({
      active: act ? { title: act.title, kind: act.kind } : null,
      url: s2.activeUrl,
      addr: await chromeA.eval(`document.querySelector('input.addr')?.value ?? null`),
    }))
    return null
  })
  check(chartsPage === 'charts.gs', 'charts: charts.gs is a real addressable page', String(chartsPage))


  // Isolate: solo a ticker from its legend chip, then restore.
  const chartsTargets = (await targets()).filter((t) => t.url.includes('mode=content'))
  let multi = null
  for (const t of chartsTargets) {
    const c = await connect(t)
    const isCharts = await c.eval(
      `!!document.querySelector('[data-chart-isolated]') &&
       [...document.querySelectorAll('button')].some(b => (b.textContent ?? '').includes('⦿'))`
    )
    if (isCharts) {
      multi = c
      break
    }
  }
  check(!!multi, 'charts: the multi-chart page exposes isolate controls')
  if (multi) {
    await multi.eval(
      `([...document.querySelectorAll('button')]
          .find(b => (b.textContent ?? '').includes('⦿')).click(), 'ok')`
    )
    const iso = await waitFor(
      async () =>
        (await multi.eval(
          `document.querySelector('[data-chart-isolated]')?.getAttribute('data-chart-isolated')`
        )) || null,
      'isolation to engage',
      6000
    ).catch(() => null)
    check(iso === 'SPY', 'charts: solo isolates the ticker', String(iso))
    await multi.eval(
      `([...document.querySelectorAll('button')]
          .find(b => (b.textContent ?? '').includes('⦿')).click(), 'ok')`
    )
    const isoOff = await waitFor(
      async () => {
        const v = await multi.eval(
          `document.querySelector('[data-chart-isolated]')?.getAttribute('data-chart-isolated')`
        )
        return v === '' || v === null ? 'ok' : null
      },
      'isolation to disable',
      6000
    ).catch(() => null)
    check(isoOff === 'ok', 'charts: disabling isolation restores the previous set')
  }

  // ------------------------------------------------------------------ help
  // Feature search deep-links into the manual: "drawing" surfaces the
  // drawing SECTION (not just the page), and the address round-trips.
  const helpHit = await chromeA.eval(
    `window.grindstone === undefined ? null :
     null`
  )
  const helpRows = await (async () => {
    // ask through any content view (chrome has no request bridge)
    for (const t of (await targets()).filter((x) => x.url.includes('mode=content'))) {
      const c = await connect(t)
      const ok = await c.eval(`typeof window.grindstone?.request === 'function'`)
      if (ok) {
        return await c.eval(
          `window.grindstone.request('GET','/api/search?q=drawing')
             .then(r => JSON.stringify((r.body?.results ?? [])
               .filter(x => x.page === 'help').slice(0, 1)))`
        )
      }
    }
    return null
  })()
  const helpRow = helpRows ? JSON.parse(helpRows)[0] : null
  check(
    !!helpRow && helpRow.section === 'drawing',
    'help: searching "drawing" surfaces the drawing section',
    helpRows ?? 'no content view answered'
  )
  await chromeA.eval(typeAddress('help.gs?s=drawing'))
  const helpOpen = await waitFor(
    async () => {
      for (const t of (await targets()).filter((x) => x.url.includes('mode=content'))) {
        const c = await connect(t)
        const cur = await c.eval(
          `document.querySelector('.help-page')?.getAttribute('data-help-current') ?? null`
        )
        if (cur === 'drawing') return 'ok'
      }
      return null
    },
    'help.gs?s=drawing to open scrolled to the drawing section',
    10000
  ).catch(() => null)
  check(helpOpen === 'ok', 'help: help.gs?s=drawing lands on the drawing section')
  void helpHit

  // The new feature indexes like every other: searching its words surfaces
  // its manual section, and the deep link lands scrolled to it.
  const favHelp = await waitFor(
    async () => {
      const s = await spy.eval(
        `window.grindstone.request('GET','/api/search?q=favorites')
          .then(r => JSON.stringify((r.body?.results ?? [])
            .filter(x => x.page === 'help' && x.section === 'favorites').slice(0, 1)))`
      )
      const rows = JSON.parse(s ?? '[]')
      return rows.length ? rows : null
    },
    'searching "favorites" to surface the manual section',
    10000
  ).catch(() => null)
  check(!!favHelp, 'help: searching "favorites" surfaces the Favorites & apps section',
    JSON.stringify(favHelp))
  await chromeA.eval(typeAddress('help.gs?s=favorites'))
  const favHelpOpen = await waitFor(
    async () => {
      for (const t of (await targets()).filter((x) => x.url.includes('mode=content'))) {
        const c = await connect(t)
        const cur = await c.eval(
          `document.querySelector('.help-page')?.getAttribute('data-help-current') ?? null`
        )
        if (cur === 'favorites') return 'ok'
      }
      return null
    },
    'help.gs?s=favorites to open scrolled to the section',
    10000
  ).catch(() => null)
  check(favHelpOpen === 'ok', 'help: help.gs?s=favorites lands on the section')

  // ---- drawings survive a restart (NOTES D7) -----------------------------
  // LAST in the run, and deliberately: it reloads the renderer, which resets
  // the engine's module-level session Map — the only way to prove a drawing
  // came back from the DATABASE rather than from memory that never died.
  // Nothing may follow it, because a reload throws away the page state every
  // earlier block set up.
  //
  // What the offline gate cannot reach, and this can: the Electron proxy. Every
  // selftest talks to the backend directly; a proxy bug broke search three
  // times before a human found it, which is why this file exists at all.
  await chromeA.eval(typeAddress('spy.gs'))
  let persistView = null
  await waitFor(
    async () => {
      for (const t of (await targets()).filter((x) => x.url.includes('mode=content'))) {
        const c = await connect(t)
        const ready = await c.eval(
          `(() => {
             if (document.querySelector('.page-head h1')?.textContent !== 'SPY') return null;
             const el = document.querySelector('[data-draw-tool]');
             return el && el.querySelector('canvas') ? 'ok' : null })()`
        )
        if (ready) {
          persistView = c
          return 'ok'
        }
      }
      return null
    },
    'the SPY chart for the persistence block',
    30000
  )

  const pAttr = (name) =>
    persistView.eval(`document.querySelector('[data-draw-tool]')?.getAttribute('${name}') ?? null`)
  const pTool = (title) =>
    persistView.eval(
      `(() => { const b = [...document.querySelectorAll('button')]
          .find(x => (x.title ?? '').startsWith(${JSON.stringify(title)}));
        if (!b) return 'missing'; b.click(); return 'ok' })()`
    )
  // The engine's own bucket name for this page: symbol|timeframe.
  const CHART_KEY = 'SPY|1Day'
  const readDoc = () =>
    persistView
      .eval(
        `window.grindstone.request('GET','/api/chart-objects?key=' +
           encodeURIComponent(${JSON.stringify(CHART_KEY)}))
          .then(r => JSON.stringify({ status: r.status, doc: r.body?.doc ?? null }))`
      )
      .then((s) => JSON.parse(s ?? '{}'))

  await pTool('Clear every drawing')
  await sleep(700) // clear is itself a save; let it land before measuring

  // ---- a trend endpoint snaps onto an h-line, VISIBLY -----------------------
  // Kade: "the lines dont snap together so its hard to tell when they actually
  // get connected." The claim is not that the coordinate matches — that was
  // already true and invisible — but that a joint marker appears where they
  // meet. Asserted on the marker, because a price comparison alone would pass
  // on exactly the version that prompted the complaint.
  const pRect0 = await persistView
    .eval(
      `(() => { const b = document.querySelector('[data-draw-tool]').getBoundingClientRect();
         return JSON.stringify({ x: b.x, y: b.y, w: b.width, h: b.height }) })()`
    )
    .then((s) => JSON.parse(s))
  await pTool('Horizontal price line')
  await sleep(200)
  for (let i = 0; i < 3; i++) {
    await persistView.click(pRect0.x + pRect0.w * 0.5, pRect0.y + pRect0.h * 0.4)
    await sleep(400)
    if ((await pAttr('data-draw-count')) === '1') break
  }
  const hPlaced = (await pAttr('data-draw-count')) === '1'
  // Two clicks for the trend, the second landing ON the h-line's row. Retried
  // like placeTwo above: clicks lose the crosshair race on dense charts, and
  // Escape's last rung DISARMS the tool, so each retry has to re-arm or it
  // clicks into a dead engine. fy stays <= 0.70 — below that is the volume
  // pane, and the engine ignores anything outside pane 0.
  for (let i = 0; i < 3; i++) {
    await pTool('Trend line')
    await sleep(200)
    await persistView.click(pRect0.x + pRect0.w * 0.25, pRect0.y + pRect0.h * 0.68)
    await sleep(350)
    await persistView.click(pRect0.x + pRect0.w * 0.7, pRect0.y + pRect0.h * 0.4)
    await sleep(500)
    if ((await pAttr('data-draw-count')) === '2') break
    await persistView.eval(
      `document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))`
    )
    await sleep(200)
  }
  const joints = await persistView.eval(
    `JSON.stringify({
       joints: document.querySelectorAll('.cd-joint:not(.cd-joint-live)').length,
       count: document.querySelector('[data-draw-tool]')?.getAttribute('data-draw-count'),
       dof: document.querySelector('[data-draw-tool]')?.getAttribute('data-draw-dof') })`
  ).then((s) => JSON.parse(s))
  check(
    hPlaced && joints.count === '2' && joints.joints >= 1,
    'snap: a trend endpoint dropped on an h-line draws a joint where they meet',
    JSON.stringify(joints)
  )
  // 1 hline + 1 trend = 5 coordinates; one 'on' merges two of them into one,
  // so a bound figure reports 4. An unbound one would say 5 — which is how the
  // DOF badge tells "connected" from "merely touching".
  check(
    joints.dof === '4',
    'snap: and the relation shows up as one fewer degree of freedom',
    `dof=${joints.dof} (5 means the endpoint only looks attached)`
  )
  await pTool('Clear every drawing')
  await sleep(700)

  // ---- option legs: zones, colors, honest empty chain --------------------
  // The scratch profile has NO Alpaca creds, deliberately: leg GEOMETRY needs
  // no market data, and the chain panel's no-provider state is the first
  // state every fresh install sees — so both are asserted here, offline.
  const legView = persistView
  const applyPreset = (key) =>
    legView.eval(
      `(() => { const s = [...document.querySelectorAll('select.seg-select')][0];
         if (!s) return 'no-select';
         const set = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set;
         set.call(s, ${JSON.stringify(key)});
         s.dispatchEvent(new Event('change', { bubbles: true }));
         return 'ok' })()`
    )
  check((await applyPreset('iron_condor')) === 'ok', 'legs: the preset picker exists')
  const condor = await waitFor(
    async () => {
      const s = await legView.eval(
        `JSON.stringify({
           zones: document.querySelectorAll('.cd-leg-zone').length,
           hues: [...new Set([...document.querySelectorAll('.cd-leg-zone')]
                    .map((z) => z.getAttribute('stroke')))].length,
           count: document.querySelector('[data-draw-tool]')?.getAttribute('data-leg-count'),
         })`
      )
      const st = JSON.parse(s)
      return st.zones === 4 && st.count === '4' ? st : null
    },
    'the condor to land as four zones',
    8000
  ).catch(() => null)
  check(condor !== null, 'legs: one click places an iron condor as four zones',
    JSON.stringify(condor))
  check(condor !== null && condor.hues === 4,
    'legs: and each leg wears its own color', `hues=${condor ? condor.hues : '?'}`)

  // The chain panel auto-opened on the preset, and with no creds it must say
  // WHY it is empty — the endpoint's reason, verbatim, never a spinner.
  const chainEmpty = await waitFor(
    async () => {
      const t = await legView.eval(
        `document.querySelector('.chain-empty')?.textContent ?? null`
      )
      return t && /no data key/i.test(t) ? t : null
    },
    'the chain panel to explain the missing provider',
    8000
  ).catch(() => null)
  check(chainEmpty !== null, 'legs: the no-creds chain state names its reason',
    String(chainEmpty).slice(0, 80))

  // NOT covered here: drawing a line PAST the last candle. The engine change
  // is real and Kade confirmed it by hand, and selftest's _chart_legs pins the
  // extended-lattice arithmetic with nine assertions (seven of them
  // mutation-proven). But reaching the whitespace from this harness needs a
  // drag-pan, and every attempt at one wedged the renderer — the CDP eval
  // after it never settled and the run died on its timeout. A hanging test is
  // worse than an absent one, so this gap is declared rather than faked.
  // Closing it means finding out why a held-button pan over this chart wedges
  // CDP, which is its own investigation.


  // Clear, then leave exactly ONE leg for the reload block to prove
  // persistence with — the reload assertion checks it came back from the DB.
  await legView.eval(
    `(() => { const b = [...document.querySelectorAll('button')]
        .find(x => (x.title ?? '') === 'Clear legs'); if (!b) return 'missing';
      b.click(); return 'ok' })()`
  )
  await waitFor(
    async () => ((await pAttr('data-leg-count')) === '0' ? 'ok' : null),
    'legs cleared', 6000
  )
  await legView.eval(
    `(() => { const b = [...document.querySelectorAll('button')]
        .find(x => (x.title ?? '').startsWith('Leg —')); if (!b) return 'missing';
      b.click(); return 'ok' })()`
  )
  const oneLeg = await waitFor(
    async () => ((await pAttr('data-leg-count')) === '1' ? 'ok' : null),
    'a single leg placed', 6000
  ).catch(() => null)
  check(oneLeg === 'ok', 'legs: the Leg button places a single at-the-money leg')


  // Place one h-line by hand, then wait for the debounced write.
  await pTool('Horizontal price line')
  await sleep(200)
  const pRect = await persistView.eval(
    `(() => { const b = document.querySelector('[data-draw-tool]').getBoundingClientRect();
       return JSON.stringify({ x: b.x, y: b.y, w: b.width, h: b.height }) })()`
  ).then((s) => JSON.parse(s))
  // THE ARITHMETIC, spelled out because it has been wrong twice. A leg mints
  // FOUR bounding lines of its own (two hlines for the strike range, two vlines
  // for the expiration range), and data-draw-count is b.drawings.length, which
  // counts them. The single leg placed just above therefore contributes 4
  // before this block draws anything. One hand-drawn h-line makes 5.
  //
  // These numbers were written when legs minted no lines at all, and 'Clear
  // legs' used to leak its guides on top of that — so the counts were stale in
  // two different directions at once and the block failed for reasons that had
  // nothing to do with persistence.
  const LEG_GUIDES = 4
  const WANT = String(LEG_GUIDES + 1)
  let placed = false
  for (let i = 0; i < 3 && !placed; i++) {
    await persistView.click(pRect.x + pRect.w * 0.5, pRect.y + pRect.h * 0.45)
    await sleep(400)
    placed = (await pAttr('data-draw-count')) === WANT
  }
  check(placed, 'persist: an h-line is placed to be saved',
    `count=${await pAttr('data-draw-count')} want=${WANT}`)

  // The USER's line, found by the absence of legOwned rather than by index:
  // the leg's guides are hlines too, so drawings[0] was satisfied by a guide
  // and the assertion passed without ever seeing the drawn line.
  const mine = (doc) => (doc?.drawings ?? []).filter((d) => !d.legOwned)
  const saved = await waitFor(
    async () => {
      const r = await readDoc()
      return r.status === 200 && mine(r.doc).length === 1 ? r : null
    },
    'the drawing to reach the backend through the real proxy',
    8000
  ).catch(async () => ({ status: 'timeout', doc: (await readDoc()).doc }))
  check(
    saved.status === 200 && mine(saved.doc)[0]?.kind === 'hline',
    'persist: a drawn line crosses the IPC proxy and lands in the database',
    JSON.stringify(saved.doc)
  )
  check(
    (saved.doc?.drawings ?? []).filter((d) => d.legOwned).length === LEG_GUIDES,
    'persist: the leg saved its four bounding lines with it',
    JSON.stringify((saved.doc?.drawings ?? []).map((d) => `${d.kind}${d.legOwned ? '*' : ''}`))
  )
  check(
    Number.isFinite(mine(saved.doc)[0]?.points?.[0]?.price),
    'persist: it stores a real data-space point, not a pixel',
    JSON.stringify(mine(saved.doc)[0]?.points)
  )
  check(
    (saved.doc?.legs?.length ?? 0) === 1 && typeof saved.doc.legs[0].expiration === 'string',
    'persist: the leg rode the same save, expiration as a calendar date',
    JSON.stringify(saved.doc?.legs)
  )

  // THE ACTUAL PROMISE: reload the renderer — the session Map dies with it —
  // and the line must come back from the database.
  //
  // Stamp the live context FIRST. Page.reload is asynchronous, so a scan that
  // only looks for "a SPY page showing 1 drawing" is satisfied by the page
  // that has not navigated yet — a false green that proves nothing about the
  // database. A global set here cannot survive a real reload, so its ABSENCE
  // is the proof the JS context (and with it the engine's session Map) is new.
  await persistView.eval(`(window.__preReload = 1, 'ok')`)
  await persistView.send('Page.reload', { ignoreCache: false })
  let reloadedView = null
  const restored = await waitFor(
    async () => {
      // Every content tab is its OWN renderer process with its own module
      // state, and several of them are showing SPY by this point in the run.
      // Match the reloaded one specifically and keep that connection: clearing
      // on a different SPY view would be a no-op on an already-empty bucket,
      // which is exactly the false failure this replaced.
      for (const t of (await targets()).filter((x) => x.url.includes('mode=content'))) {
        const c = await connect(t)
        const n = await c.eval(
          `(() => {
             if (window.__preReload) return null;          // not the reloaded context
             if (document.querySelector('.page-head h1')?.textContent !== 'SPY') return null;
             const el = document.querySelector('[data-draw-tool]');
             if (!el || !el.querySelector('canvas')) return null;
             // Drawing AND leg both came back from the database, or the check
             // does not pass — the leg was placed by the block above.
             return el.getAttribute('data-draw-count') + '/' + el.getAttribute('data-leg-count') })()`
        )
        // 5/1 = one hand-drawn h-line + the leg's four bounding lines, and the
        // one leg. All of it came back from the database or none of it did.
        if (n === `${WANT}/1`) {
          reloadedView = c
          return 'ok'
        }
      }
      return null
    },
    'the drawing to come back after a reload',
    30000
  ).catch(() => null)
  check(restored === 'ok',
    'persist: the drawing survives a renderer reload (fresh JS context, read from the DB)')

  // And clearing it removes the row, so the store does not accumulate empty
  // documents for every chart ever opened. Driven through the reloaded view —
  // the one whose engine actually holds the restored drawing.
  if (reloadedView) persistView = reloadedView
  // 'Clear every drawing' clears the drawings the USER made and leaves the
  // lines that ARE a leg. Wiping those left the leg naming four ids that no
  // longer existed, which silently reverted its filter to the window it was
  // born with — the bug Kade reported from the other side ("deleting a leg
  // leaves the lines"). So the row must still hold exactly the four guides.
  const clearBtn = await pTool('Clear every drawing')
  const keptGuides = await waitFor(
    async () => {
      const r = await readDoc()
      const d = r.doc?.drawings ?? []
      return d.length === LEG_GUIDES && d.every((x) => x.legOwned) ? 'ok' : null
    },
    'the cleared chart to keep only the leg-owned lines',
    8000
  ).catch(() => null)
  check(keptGuides === 'ok', 'persist: clearing drawings spares the lines that ARE a leg',
    `btn=${clearBtn} domCount=${await pAttr('data-draw-count')} ` +
    `doc=${JSON.stringify((await readDoc()).doc)}`)

  // And THEN clearing the legs empties the row, so the store does not
  // accumulate a document for every chart ever opened.
  await persistView.eval(
    `(() => { const b = [...document.querySelectorAll('button')]
        .find(x => (x.title ?? '') === 'Clear legs'); if (!b) return 'missing';
      b.click(); return 'ok' })()`
  )
  const emptied = await waitFor(
    async () => {
      const r = await readDoc()
      return (r.doc?.drawings?.length ?? 0) === 0 && (r.doc?.legs?.length ?? 0) === 0 ? 'ok' : null
    },
    'the cleared chart to empty in the store',
    8000
  ).catch(() => null)
  check(emptied === 'ok', 'persist: clearing the legs too empties the stored document',
    `domCount=${await pAttr('data-draw-count')} ` +
    `doc=${JSON.stringify((await readDoc()).doc)}`)
} catch (err) {
  console.log('FAIL  harness error —', err.message)
  failures += 1
} finally {
  cleanup()
}

console.log(failures === 0 ? 'E2E OK' : `E2E FAILED (${failures})`)
// Name the log on the way out, so the run that just scrolled past is findable
// without knowing this file. Every earlier run is still in there above it.
if (!logBroken) console.log(`      run log appended to ${LOG}`)
process.exit(failures === 0 ? 0 : 1)
