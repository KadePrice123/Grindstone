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
      // a half-placed anchor would corrupt the next attempt — cancel it
      await chartView.eval(
        `document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))`
      )
      await sleep(200)
    }
    return (await attr('data-draw-count')) === wantCount
  }

  // Trend line: arm from the toolbar, two real clicks, one drawing.
  check((await toolBtn('Trend line')) === 'ok', 'tools: the Line button exists')
  await waitFor(async () => (await attr('data-draw-tool')) === 'trend', 'trend armed', 5000)
  const drew = await placeTwo(0.30, 0.70, 0.60, 0.35, '1')
  check(drew, 'tools: two real clicks place a trend line')

  // Select it mid-span: the editor appears with per-endpoint fields.
  await toolBtn('Select drawings')
  const selHit = await fanClick(0.45, 0.525, 'data-draw-selected', '1')
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
      await chartView.eval(
        `document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))`
      )
    }
  }
  check(measured, 'tools: measure connects two real points')
  const clearedBtn = await toolBtn('Clear all measurements')
  const cleared = await waitFor(
    async () => ((await attr('data-measure-count')) === '0' ? 'ok' : null),
    'measures cleared',
    8000
  ).catch(async () => `btn=${clearedBtn} count=${await attr('data-measure-count')}`)
  check(cleared === 'ok', 'tools: clear-measures wipes the annotations', String(cleared))

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
} catch (err) {
  console.log('FAIL  harness error —', err.message)
  failures += 1
} finally {
  cleanup()
}

console.log(failures === 0 ? 'E2E OK' : `E2E FAILED (${failures})`)
process.exit(failures === 0 ? 0 : 1)
