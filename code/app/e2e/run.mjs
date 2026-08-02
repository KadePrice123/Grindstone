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
  const setup = await auth.eval(
    `window.grindstone.request('POST','/api/auth/setup',{username:'e2e',password:'e2e-password-1'}).then(r=>JSON.stringify(r))`
  )
  check(setup.includes('"status":200'), 'auth: first-run setup succeeds')
  await sleep(2500)

  const chromeTarget = await waitFor(
    async () => (await targets()).find((t) => t.url.includes('mode=chrome')),
    'the tab strip'
  )
  const chromeA = await connect(chromeTarget)
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
} catch (err) {
  console.log('FAIL  harness error —', err.message)
  failures += 1
} finally {
  cleanup()
}

console.log(failures === 0 ? 'E2E OK' : `E2E FAILED (${failures})`)
process.exit(failures === 0 ? 0 : 1)
