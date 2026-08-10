/** Visual verification for the Opt page: boots the BUILT app on a scratch
 *  profile, seeds a leg whose pick points at a real archived contract, opens
 *  opt.gs?s=SPY and screenshots both tabs.
 *
 *  A screenshot harness, not a test: it asserts nothing beyond "the page
 *  exists" — its output is PNGs a person (or Claude) actually looks at,
 *  because layout is the one thing the offline gate is structurally blind to.
 *
 *  The scratch profile has no market keys on purpose; the HISTORY side runs
 *  entirely on options_history.db (copied in), so what these shots show is
 *  the archive path — and the live-chain surfaces show their honest empty
 *  states, which are worth looking at too.
 *
 *  Usage: node e2e/optshot.mjs <outDir>
 */
import { spawn } from 'node:child_process'
import { copyFileSync, mkdirSync, writeFileSync, existsSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const APP = path.resolve(HERE, '..')
const OUT = process.argv[2] ? path.resolve(process.argv[2]) : path.join(HERE, 'shots')
const PORT = 9455
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const dataDir = path.join(OUT, 'profile')
mkdirSync(dataDir, { recursive: true })
const histSrc = path.resolve(APP, '..', '..', 'data', 'options_history.db')
if (existsSync(histSrc)) copyFileSync(histSrc, path.join(dataDir, 'options_history.db'))
else console.log('WARN: no options_history.db to copy — history views will refuse')

const child = spawn(
  path.join(APP, 'node_modules', 'electron', 'dist', 'electron.exe'),
  [`--remote-debugging-port=${PORT}`, '.'],
  { cwd: APP, env: { ...process.env, GRINDSTONE_DATA_DIR: dataDir }, stdio: 'ignore' }
)

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
  await new Promise((res, rej) => {
    ws.onopen = res
    ws.onerror = rej
  })
  const send = (method, params) =>
    new Promise((resolve) => {
      const mid = ++id
      pending.set(mid, (m) => resolve(m.result))
      ws.send(JSON.stringify({ id: mid, method, params }))
    })
  const evaluate = async (expr) => {
    const r = await send('Runtime.evaluate', {
      expression: expr, awaitPromise: true, returnByValue: true,
    })
    return r?.result?.value
  }
  return { send, eval: evaluate }
}

async function waitFor(fn, what, ms = 30000) {
  const t0 = Date.now()
  for (;;) {
    const v = await fn().catch(() => null)
    if (v) return v
    if (Date.now() - t0 > ms) throw new Error(`timeout: ${what}`)
    await sleep(400)
  }
}

async function shot(conn, name) {
  const r = await conn.send('Page.captureScreenshot', { format: 'png' })
  writeFileSync(path.join(OUT, name), Buffer.from(r.data, 'base64'))
  console.log('shot:', name)
}

try {
  // ---- boot + first profile ------------------------------------------------
  const authT = await waitFor(async () => {
    const ts = await targets()
    return ts.find((t) => t.url.includes('mode=auth'))
  }, 'auth view')
  const auth = await connect(authT)
  await waitFor(() => auth.eval(`document.querySelectorAll('input.field').length >= 2`),
    'the signup form')
  await sleep(1200)
  const submitted = await auth.eval(`(() => {
    const set = (el, v) => {
      Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')
        .set.call(el, v)
      el.dispatchEvent(new Event('input', { bubbles: true }))
    }
    const inputs = document.querySelectorAll('input.field')
    set(inputs[0], 'shot')
    for (let i = 1; i < inputs.length; i++) set(inputs[i], 'optshot-pass-1')
    const btn = [...document.querySelectorAll('button')]
      .find((b) => /create profile|unlock/i.test(b.textContent || ''))
    btn.click()
    return 'ok'
  })()`)
  console.log('auth:', submitted)

  const homeT = await waitFor(async () => {
    const ts = await targets()
    return ts.find((t) => t.url.includes('mode=content'))
  }, 'a content view')
  const home = await connect(homeT)

  // ---- seed the trade: one short put whose pick is a real archived contract
  const put = await home.eval(`window.grindstone.request('PUT', '/api/chart-objects', {
    key: 'SPY|1Day',
    doc: { legs: [{
      id: 'lg1', side: 'short', right: 'P', expiration: '2026-09-18',
      strike: 761, dteTol: 3, strikeTol: 4, slot: 0,
      pick: 'SPY260918P00761000'
    }] }
  }).then(r => JSON.stringify(r).slice(0, 120))`)
  console.log('seed:', put)

  // SHOT_PAGE=datapad: THE AUTOSAVE TRIPWIRE. Drives the REAL wheel through
  // the same wheelEvt channel real input uses -- main's state machine, the
  // overlay renderer and the backend wheels doc all take part -- then asserts
  // the posted leg reaches the store AND STAYS. If a post path ever bypasses
  // the live engine, the engine's own 400ms autosave reverts it and this goes
  // red; that failure shipped once (the Opt-page pick deletion: success
  // reported, data gone, no error anywhere).
  if (process.env.SHOT_PAGE === 'datapad') {
    const seed = await home.eval(`window.grindstone.request('POST','/api/notepad',{
      payload:{v:1,kind:'contract',
        data:{occ_symbol:'SPY260918P00755000',expiration:'2026-09-18',strike:755,
              right:'P',bid:38.1,ask:38.9,iv:0.19,delta:-0.39},
        provenance:{workspace:'user',capturedAt:new Date().toISOString(),
                    page:'e2e',address:'spy.gs'}},
      label:'e2e 755P'}).then((r)=>r.status)`)
    console.log('notepad seed:', seed)

    await home.eval(`window.grindstone.openTab('symbol:SPY')`)
    const spy = await waitFor(async () => {
      const ts = await targets()
      for (const t of ts.filter((x) => x.url.includes('mode=content'))) {
        const c = await connect(t)
        if (await c.eval(`!!document.querySelector('[data-wheel-context="chart"]')`)) return c
      }
      return null
    }, 'the symbol page chart', 30000)
    await sleep(2500)

    const legsOf = `window.grindstone.request('GET','/api/chart-objects?key='+encodeURIComponent('SPY|1Day'))
      .then((r)=>{const d=(r.body&&(r.body.doc??r.body))||{};return JSON.stringify((d.legs??[]).map((l)=>l.strike))})`
    const before = JSON.parse(await spy.eval(legsOf))
    console.log('legs before:', JSON.stringify(before))

    // Spawn the wheel the way a right-click does: down, idle, up with no
    // travel -> click mode. Away from the chart so the MAIN wheel spawns.
    const sendWheel = (kind, x, y) =>
      spy.eval(`(window.grindstone.wheelEvt(${JSON.stringify(kind)}, ${x}, ${y}), 'ok')`)
    await sendWheel('down', 900, 300)
    await sleep(350)
    await sendWheel('up', 900, 300)

    const wheelT = await waitFor(async () =>
      (await targets()).find((t) => t.url.includes('mode=wheel')), 'the wheel overlay', 15000)
    const wheelUi = await connect(wheelT)
    const face = await waitFor(async () => {
      const raw = await wheelUi.eval(`JSON.stringify({
        segs: document.querySelectorAll('.wf-seg').length,
        id: document.querySelector('.wheel-face')?.dataset.wheel ?? null,
        mode: document.querySelector('.wheel-face')?.dataset.mode ?? null,
        labels: [...document.querySelectorAll('.wf-seg')].map((t)=>t.textContent.trim())
      })`)
      const st = JSON.parse(raw)
      return st.mode === 'click' ? st : null
    }, 'the wheel in click mode', 10000)
    console.log('wheel:', JSON.stringify(face))

    const postIdx = face.labels.findIndex((t) => /post/i.test(t))
    console.log('post segment index:', postIdx)
    if (postIdx < 0) throw new Error('the main wheel has no Post segment')

    // RIGHT-click the Post segment: the quick variant, which posts the most
    // recent compatible entry with no picker (DX-6 / DX-10b).
    await wheelUi.eval(`(document.querySelector('[data-seg="${postIdx}"]')
      .dispatchEvent(new MouseEvent('mousedown', {bubbles:true, button:2})), 'ok')`)

    await sleep(700)
    const notice = await spy.eval(
      `document.querySelector('[data-datapad-notice]')?.textContent ?? ''`)
    console.log('announce:', JSON.stringify(notice))

    // The engine's own save is debounced 400ms; poll, then re-check after a
    // further beat. A direct-PUT post would be reverted in that window.
    let seen = null
    for (let i = 0; i < 12 && !seen; i++) {
      await sleep(400)
      const now = JSON.parse(await spy.eval(legsOf))
      if (now.length > before.length) seen = now
    }
    console.log('legs after:', JSON.stringify(seen))
    await sleep(1500)
    const still = JSON.parse(await spy.eval(legsOf))
    const survived = seen !== null && still.length >= seen.length
    console.log('TRIPWIRE:', JSON.stringify({
      posted: seen !== null, survived, announce: notice,
      before: before.length, after: still.length,
    }))
    await shot(spy, 'datapad-post.png')
    process.exit(seen !== null && survived ? 0 : 1)
  }

  // SHOT_PAGE=data screenshots the Data and Settings pages instead of the Opt
  // page. Same boot, same scratch profile — a second harness would duplicate
  // ninety lines of auth just to open a different tab.
  if (process.env.SHOT_PAGE === 'data') {
    // Match the page's OWN heading, not "either of them": a loose regex found
    // the already-open Data tab again and reported its headings as Settings'.
    for (const [route, name, h1re] of [
      ['data', 'data', /Data management/],
      ['settings', 'settings', /^Settings$/],
    ]) {
      await home.eval(`window.grindstone.openTab('${route}')`)
      const t = await waitFor(async () => {
        const ts = await targets()
        for (const x of ts.filter((y) => y.url.includes('mode=content'))) {
          const c = await connect(x)
          const h1 = await c.eval(`document.querySelector('h1')?.textContent ?? ''`)
          if (h1re.test(h1.trim())) return { c, h1 }
        }
        return null
      }, `the ${route} page`, 25000)
      await t.c.send('Emulation.setDeviceMetricsOverride', {
        width: 1500, height: 1150, deviceScaleFactor: 1, mobile: false,
      })
      await sleep(2500)
      console.log(`${name.toUpperCase()} HEADINGS:`, await t.c.eval(
        `JSON.stringify([...document.querySelectorAll('.card h2')].map(e => e.textContent))`))
      await shot(t.c, `${name}.png`)
    }
    process.exit(0)
  }

  await home.eval(`window.grindstone.openTab('opt:SPY')`)
  const optT = await waitFor(async () => {
    const ts = await targets()
    for (const t of ts.filter((x) => x.url.includes('mode=content'))) {
      const c = await connect(t)
      const is = await c.eval(`!!document.querySelector('[data-opt-symbol="SPY"]')`)
      if (is) return { t, c }
    }
    return null
  }, 'the opt page')
  const opt = optT.c

  // A workstation needs its width: below 1100px the right rail collapses
  // under the chart. Electron implements neither Browser.setWindowBounds nor
  // window sizing over CDP, but Emulation.setDeviceMetricsOverride reshapes
  // the CONTENT's layout viewport — which is the thing being photographed.
  await opt.send('Emulation.setDeviceMetricsOverride', {
    width: 1680, height: 1050, deviceScaleFactor: 1, mobile: false,
  })
  await sleep(900)

  // Let the poll, auto-pick and fetches land, then look at both tabs.
  await waitFor(() => opt.eval(
    `document.querySelectorAll('.opt-leg').length > 0`), 'the selector legs')
  await sleep(2500)
  await shot(opt, 'opt-future.png')

  await opt.eval(`[...document.querySelectorAll('button')]
    .find((b) => b.textContent === 'History')?.click()`)
  await waitFor(() => opt.eval(
    `document.querySelectorAll('.opt-card canvas').length >= 1`), 'a history chart', 20000)
  await sleep(3000)
  // HARD PROOF, not pixel-reading: the counts the page itself stamps, plus
  // whether lightweight-charts actually created a LEFT price scale (it only
  // does so when a series is assigned to it and has data).
  const proof = await opt.eval(`(() => {
    const card = document.querySelector('.opt-card')
    return JSON.stringify({
      under: card?.getAttribute('data-under-points'),
      series: card?.getAttribute('data-series-points'),
      canvases: document.querySelectorAll('.opt-card canvas').length,
      // the left axis renders its own label column when visible
      axisText: [...document.querySelectorAll('.opt-card td, .opt-card canvas')].length,
      err: document.querySelector('.opt-note .loss')?.textContent ?? null,
      // The unit is a claim the caption makes in words; read it back rather
      // than trusting that a '%' appeared somewhere on the canvas.
      unit: card?.getAttribute('data-unit'),
      peak: card?.getAttribute('data-peak'),
      caption: (document.querySelector('.opt-note')?.textContent ?? '').slice(0, 150),
    })
  })()`)
  console.log('UNDERLYING PROOF:', proof)
  await shot(opt, 'opt-history.png')

  // AND THE SAME SERIES IN DOLLARS, so the two units can be compared side by
  // side: the percent view should flatten drift the dollar view shows.
  await opt.eval(`(() => {
    const s = document.querySelector('.opt-unit')
    if (!s) return
    s.value = 'usd'
    s.dispatchEvent(new Event('change', { bubbles: true }))
  })()`)
  await sleep(2500)
  console.log('USD PROOF:', await opt.eval(`(() => {
    const c = document.querySelector('.opt-card')
    return JSON.stringify({ unit: c?.getAttribute('data-unit'), peak: c?.getAttribute('data-peak') })
  })()`))
  await shot(opt, 'opt-history-usd.png')
} catch (e) {
  console.log('FAILED:', e.message)
} finally {
  try {
    child.kill()
  } catch { /* gone */ }
}
console.log('done')
