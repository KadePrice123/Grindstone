/** The Insure page's e2e: the FRESH-INSTALL truth on a scratch profile.
 *
 *  NO market keys and NO archive on purpose — honest emptiness is this app's
 *  designed first state, and this harness proves the page renders it in
 *  words rather than a spinner or (worse) a fabricated dot. Measured NUMBERS
 *  are not asserted here; they live in the offline gate with fixtures
 *  (_insurance_engine), the _options_chain division of labor.
 *
 *  Set INSURE_ARCHIVE=1 to also copy the real archive in (when present) and
 *  prove the measured half renders: expectancy builds, the honesty caption
 *  names what is not plotted, and a dot click deep-links to opt:SYM:OCC.
 *
 *  Usage: node e2e/insureshot.mjs <outDir>
 */
import { spawn } from 'node:child_process'
import { copyFileSync, mkdirSync, writeFileSync, existsSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const APP = path.resolve(HERE, '..')
const OUT = process.argv[2] ? path.resolve(process.argv[2]) : path.join(HERE, 'shots')
const PORT = 9456 // not optshot's 9455, so the two can coexist
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const dataDir = path.join(OUT, 'profile')
mkdirSync(dataDir, { recursive: true })
const withArchive = process.env.INSURE_ARCHIVE === '1'
if (withArchive) {
  const histSrc = path.resolve(APP, '..', '..', 'data', 'options_history.db')
  if (existsSync(histSrc)) copyFileSync(histSrc, path.join(dataDir, 'options_history.db'))
  else console.log('WARN: INSURE_ARCHIVE=1 but no options_history.db to copy')
}

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
  // optshot's rule holds here too: never screenshot a backgrounded view.
  const r = await conn.send('Page.captureScreenshot', { format: 'png' })
  writeFileSync(path.join(OUT, name), Buffer.from(r.data, 'base64'))
  console.log('shot:', name)
}

try {
  // ---- boot + first profile ----------------------------------------------
  const authT = await waitFor(async () => {
    const ts = await targets()
    return ts.find((t) => t.url.includes('mode=auth'))
  }, 'auth view')
  const auth = await connect(authT)
  await waitFor(() => auth.eval(`document.querySelectorAll('input.field').length >= 2`),
    'the signup form')
  await sleep(1200)
  await auth.eval(`(() => {
    const set = (el, v) => {
      Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')
        .set.call(el, v)
      el.dispatchEvent(new Event('input', { bubbles: true }))
    }
    const inputs = document.querySelectorAll('input.field')
    set(inputs[0], 'shot')
    for (let i = 1; i < inputs.length; i++) set(inputs[i], 'insureshot-pass-1')
    const btn = [...document.querySelectorAll('button')]
      .find((b) => /create profile|unlock/i.test(b.textContent || ''))
    btn.click()
    return 'ok'
  })()`)

  const homeT = await waitFor(async () => {
    const ts = await targets()
    return ts.find((t) => t.url.includes('mode=content'))
  }, 'a content view')
  const home = await connect(homeT)
  await sleep(1500)

  // Favorites drive the scan: star two symbols the real archive carries, so
  // the with-archive branch measures and the bare branch shows honest rows.
  for (const sym of ['SPY', 'USO']) {
    await home.eval(`window.grindstone.request('POST', '/api/favorites', {
      kind: 'symbol', key: ${JSON.stringify(sym)}, label: ${JSON.stringify(sym)}
    }).then(() => 'ok').catch(e => 'fav failed: ' + e)`)
  }

  // ---- open insure.gs THROUGH THE ADDRESS PATH ----------------------------
  // openTab with the route key exercises the same seams the omnibox uses;
  // a typo in urls.ts/App.tsx routing dead-ends on Home and fails the h1 wait.
  await home.eval(`window.grindstone.openTab('insure')`)
  const page = await waitFor(async () => {
    const ts = await targets()
    for (const t of ts.filter((x) => x.url.includes('mode=content'))) {
      const c = await connect(t)
      if (await c.eval(`!!document.querySelector('.insure-page')`)) return c
    }
    return null
  }, 'the insure page', 30000)

  await waitFor(() => page.eval(
    `document.querySelector('.insure-page h1')?.textContent === 'Insure'`),
    'the h1')
  // Let the status + staggered scans land (or refuse) before reading proofs.
  await waitFor(() => page.eval(`(() => {
    const p = document.querySelector('.insure-page')
    return p && p.getAttribute('data-insure-scanned') === p.getAttribute('data-insure-total')
      && p.getAttribute('data-insure-total') !== '0'
  })()`), 'all symbols scanned', 60000)
  await sleep(2000)

  const proof = await page.eval(`(() => {
    const p = document.querySelector('.insure-page')
    const dots = document.querySelector('[data-insure-dots]')
    return JSON.stringify({
      scanned: p?.getAttribute('data-insure-scanned'),
      total: p?.getAttribute('data-insure-total'),
      nodb: document.querySelector('[data-insure-nodb]')?.textContent?.slice(0, 90) ?? null,
      dots: dots ? Number(dots.getAttribute('data-insure-dots')) : 0,
      emptyNote: document.querySelector('[data-insure-dots-empty]')?.textContent
        ?.slice(0, 120) ?? null,
      sections: [...document.querySelectorAll('.insure-section h2')]
        .map((h) => h.textContent.split('—')[0].trim()),
      refusals: [...document.querySelectorAll('[data-insure-refused]')]
        .map((d) => d.textContent.slice(0, 80)),
      // The split sentence: archive prices the risk, only a live chain says
      // what it pays — the page must SAY it, per symbol, not just show less.
      chain_sentences: [...document.querySelectorAll('.insure-page > .dim.subtle')]
        .map((d) => d.textContent.slice(0, 100))
        .filter((t) => t.includes('only a live chain')),
    })
  })()`)
  console.log('INSURE PROOF:', proof)

  const p = JSON.parse(proof)
  if (!withArchive) {
    // THE FRESH-INSTALL CONTRACT: no archive -> the NO_DB sentence renders,
    // and exactly ZERO dots exist — a fabricated dot here is the one failure
    // this harness exists to catch.
    console.log('EMPTY-STATE VERDICT:', JSON.stringify({
      zero_fabricated_dots: p.dots === 0,
      names_the_absence: p.nodb !== null || p.emptyNote !== null,
    }))
  } else {
    // With the archive but no creds, the measured half stands alone: the
    // sweep ran, and each symbol says WHY it has no offered side.
    await shot(page, 'insure-archive-keyless.png')
  }
  if (withArchive && p.dots > 0) {
    // With the archive AND a live chain: a dot click must deep-link.
    await shot(page, 'insure-scanner.png')
    const dot = await page.eval(`(() => {
      const g = document.querySelector('.insure-plot svg g')
      if (!g) return null
      g.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      return 'clicked'
    })()`)
    console.log('DOT CLICK:', dot)
    await sleep(2000)
    const optOpened = await waitFor(async () => {
      const ts = await targets()
      for (const t of ts.filter((x) => x.url.includes('mode=content'))) {
        const c = await connect(t)
        if (await c.eval(`!!document.querySelector('[data-opt-symbol]')`)) return true
      }
      return null
    }, 'an Opt tab from the dot click', 20000).catch(() => false)
    console.log('DEEP LINK:', JSON.stringify({ opt_tab_opened: !!optOpened }))
  }
  if (!withArchive) await shot(page, 'insure-empty.png')
} catch (e) {
  console.log('FAILED:', e.message)
} finally {
  try {
    child.kill()
  } catch { /* gone */ }
}
console.log('done')
