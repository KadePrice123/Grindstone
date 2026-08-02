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

  // Left-clicking the SPY segment (SW, index 5) opens/focuses the SPY tab
  // and the wheel closes.
  await wheelUi.eval(
    `(document.querySelector('[data-seg="5"]')
        .dispatchEvent(new MouseEvent('mousedown', {bubbles: true, button: 0})), 'ok')`
  )
  const afterTicker = await waitFor(
    async () => {
      const gone = await wheelUi.eval(`document.querySelector('.wheel-stage') === null`)
      if (!gone) return null
      const st = await chromeA.eval('window.grindstoneTabs.getState()')
      const active = st.tabs.find((t) => t.id === st.activeId)
      return active && active.title === 'SPY' ? active : null
    },
    'the SPY segment to open the ticker and close the wheel',
    8000
  ).catch(() => null)
  check(!!afterTicker, 'wheel: the SPY segment opens the ticker page and closes the wheel')

  // HOLD mode: press, drag due WEST (segment 6 = Tickers wheel nav),
  // release → switches wheel and STAYS OPEN in click mode (the spec's
  // "after a wheel nav while holding, enter left-click mode").
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
           segs: document.querySelectorAll('.wf-seg').length
         })`
      )
      const st = JSON.parse(s)
      return st.id === 'tickers' && st.mode === 'click' ? st : null
    },
    'hold-drag west to switch to the Tickers wheel and stay open',
    8000
  ).catch(() => null)
  check(
    !!holdState && holdState.segs === 6,
    'wheel: hold-drag onto a wheel-nav switches wheels and stays open',
    JSON.stringify(holdState)
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
      return r === 'tickers' ? r : null
    },
    'the lock to persist',
    8000
  ).catch(() => null)
  check(lockedCfg === 'tickers', 'wheel: the center hub locks the shown wheel as default')

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
      return id === 'tickers' ? id : null
    },
    'the locked wheel to spawn first',
    8000
  ).catch(() => null)
  check(respawn === 'tickers', 'wheel: a locked wheel is the new default on spawn')

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
} catch (err) {
  console.log('FAIL  harness error —', err.message)
  failures += 1
} finally {
  cleanup()
}

console.log(failures === 0 ? 'E2E OK' : `E2E FAILED (${failures})`)
process.exit(failures === 0 ? 0 : 1)
