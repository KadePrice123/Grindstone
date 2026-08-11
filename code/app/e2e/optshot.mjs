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
  // Chromium will not composite a BACKGROUND view, so captureScreenshot on a
  // page whose tab is not active NEVER SETTLES — the process then exits 13 on
  // an unsettled top-level await, naming no page. Page.bringToFront does not
  // rescue it either: visibility here belongs to Electron's WebContentsView,
  // not to Chromium's tab machinery. So the rule is ordering — anything that
  // opens a new tab runs AFTER the last screenshot.
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

    // The pad SURVIVES between runs — the profile is scratch per suite, not
    // per run — so the first version of this asserted an empty pad against 19
    // leftover entries and read the held-data rung as the class one. Clear it
    // rather than assume it.
    const cleared = await spy.eval(`window.grindstone.request('GET','/api/notepad')
      .then((r)=>Promise.all((r.body??[]).map((e)=>
        window.grindstone.request('DELETE','/api/notepad/'+e.id))))
      .then((xs)=>xs.length)`)
    console.log('pad cleared:', cleared)

    // ---------------------------------------------- PREDICTIVE QUICK INTENT
    // The hint has to be VISIBLE (DX-14): a predicted action the user cannot
    // see before releasing is a misclick generator, and a source scan cannot
    // tell whether it reached the face. This spawns with a CHAIN context and
    // reads the overlay's own DOM.
    //
    // Order matters: the pad is still EMPTY here, so what shows is the class
    // prediction. The held-data rung is asserted after the seed below — the
    // same two spawns, in the order that proves which one outranks.
    const sendWheel = (kind, x, y, ctx) =>
      spy.eval(`(window.grindstone.wheelEvt(${JSON.stringify(kind)}, ${x}, ${y}` +
               (ctx ? `, ${JSON.stringify(ctx)}` : '') + `), 'ok')`)
    let lastWheelUi = null
    const hintsNow = async () => {
      // DISMISS ANY OPEN WHEEL FIRST. A spawn while the overlay is up is not
      // a spawn — main keeps the session and the face never re-materializes,
      // so the second call read the FIRST call's face and the two rungs of
      // the priority looked identical. run.mjs dismisses the same way.
      const open = (await targets()).find((t) => t.url.includes('mode=wheel'))
      if (open) {
        const ui0 = await connect(open)
        await ui0.eval(`(document.querySelector('.wheel-stage')
          ?.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, button: 0})), 'ok')`)
        await sleep(250)
      }
      await sendWheel('down', 900, 300,
        { context: 'chain', symbols: ['SPY'], occ: 'SPY260918P00755000' })
      await sleep(350)
      await sendWheel('up', 900, 300)
      const t = await waitFor(async () =>
        (await targets()).find((x) => x.url.includes('mode=wheel')), 'the wheel overlay', 15000)
      const ui = await connect(t)
      lastWheelUi = ui
      return await waitFor(async () => {
        const raw = await ui.eval(`JSON.stringify({
          mode: document.querySelector('.wheel-face')?.dataset.mode ?? null,
          hints: [...document.querySelectorAll('.wf-hint')].map((h)=>h.textContent.trim()),
          labels: [...document.querySelectorAll('.wf-seg')].map((t)=>t.textContent.trim()),
        })`)
        const st = JSON.parse(raw)
        return st.mode === 'click' ? st : null
      }, 'the wheel in click mode', 10000)
    }
    const classHint = await hintsNow()
    console.log('PREDICT class:', JSON.stringify(classHint))

    const seed = await home.eval(`window.grindstone.request('POST','/api/notepad',{
      payload:{v:1,kind:'contract',
        data:{occ_symbol:'SPY260918P00755000',expiration:'2026-09-18',strike:755,
              right:'P',bid:38.1,ask:38.9,iv:0.19,delta:-0.39},
        provenance:{workspace:'user',capturedAt:new Date().toISOString(),
                    page:'e2e',address:'spy.gs'}},
      label:'e2e 755P'}).then((r)=>r.status)`)
    console.log('notepad seed:', seed)

    // Same spawn, same chain context — but now something is held, so the
    // held-data rung must take the face over the class prediction.
    const heldHint = await hintsNow()
    console.log('PREDICT held:', JSON.stringify(heldHint))
    // The hint is a VISIBILITY promise (DX-14), and a DOM node that exists
    // can still be invisible. The screenshot is the only thing that says so.
    if (lastWheelUi) await shot(lastWheelUi, 'wheel-hint.png')
    const predictOk =
      classHint.hints.some((h) => h.includes('SPY Opt')) &&
      heldHint.hints.some((h) => h.includes('e2e 755P')) &&
      !heldHint.hints.some((h) => h.includes('SPY Opt'))
    console.log('PREDICT:', JSON.stringify({ predictOk }))

    const legsOf = `window.grindstone.request('GET','/api/chart-objects?key='+encodeURIComponent('SPY|1Day'))
      .then((r)=>{const d=(r.body&&(r.body.doc??r.body))||{};return JSON.stringify((d.legs??[]).map((l)=>l.strike))})`
    const before = JSON.parse(await spy.eval(legsOf))
    console.log('legs before:', JSON.stringify(before))

    // Spawn the wheel the way a right-click does: down, idle, up with no
    // travel -> click mode. Away from the chart so the MAIN wheel spawns.
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

    // And the notepad renders what was grabbed, by KIND -- the page the
    // picker's top segment leads to. Seed one of every kind first so every
    // declared renderer is exercised, not just the one the tripwire made.
    for (const [kind, data] of [
      ['note', { text: 'a note, rendered as text' }],
      ['chain', { underlying: 'SPY', contracts: [
        { occ_symbol: 'SPY260918P00755000', expiration: '2026-09-18', strike: 755,
          right: 'P', bid: 38.1, ask: 38.9, last: 38.5, iv: 0.19, delta: -0.39,
          gamma: 0.01, theta: -0.09, vega: 0.55, rho: -0.3 }] }],
      ['backtest-spec', { spec: { name: 'demo', capital: 100000,
        legs: [{ action: 'sell', right: 'put', delta: 0.3, dte: 45 }] } }],
    ]) {
      await spy.eval(`window.grindstone.request('POST','/api/notepad',{payload:{
        v:1,kind:${JSON.stringify(kind)},data:${JSON.stringify(data)},
        provenance:{workspace:'user',capturedAt:new Date().toISOString(),
                    page:'e2e',address:'spy.gs',symbol:'SPY'}}}).then(r=>r.status)`)
    }
    await spy.eval(`window.grindstone.openTab('notepad')`)
    const padT = await waitFor(async () => {
      const ts = await targets()
      for (const t of ts.filter((x) => x.url.includes('mode=content'))) {
        const c = await connect(t)
        if (await c.eval(`document.querySelector('h1')?.textContent === 'Notepad'`)) return c
      }
      return null
    }, 'the notepad page', 20000)
    await padT.send('Emulation.setDeviceMetricsOverride', {
      width: 1400, height: 1100, deviceScaleFactor: 1, mobile: false,
    })
    await sleep(1800)
    const pad = JSON.parse(await padT.eval(`JSON.stringify({
      entries: document.querySelectorAll('.np-entry').length,
      kinds: [...document.querySelectorAll('[data-entry-kind]')].map((e)=>e.dataset.entryKind),
      chainCols: document.querySelector('.np-table thead tr')?.children.length ?? 0,
      rawJson: document.body.textContent.includes('"occ_symbol"'),
    })`))
    console.log('NOTEPAD:', JSON.stringify(pad))
    await shot(padT, 'notepad.png')
    const padOk = pad.entries >= 4 && pad.chainCols === 12 && !pad.rawJson

    // ---- AND THE RELEASE MUST ACTUALLY OPEN IT (the bug Kade reported).
    // LAST, deliberately: it opens a tab, which pushes every other view
    // into the background, and a background view cannot be screenshotted.
    // Rendering the hint and opening the tab are two different things, and
    // the first version of this asserted only the first. The face said
    // "-> SPY Opt", the release called openAddress('opt.gs?s=SPY'), and
    // main's private copy of gsRoute -- which never learned 'opt' -- turned
    // that into the bare route 'opt', which parseRoute resolves to idle.
    // A tab opened. It was Home. So: assert the PAGE, not the hint.
    await spy.eval(`window.grindstone.request('GET','/api/notepad')
      .then((r)=>Promise.all((r.body??[]).map((e)=>
        window.grindstone.request('DELETE','/api/notepad/'+e.id))))`)
    const classAgain = await hintsNow()          // empty pad -> the class rung
    // The label text carries the segment's icon glyph AND now its hint, so an
    // anchored /^tabs/ matches nothing — the real text is "⧉Tabs→ SPY Opt".
    const tabsIdx = classAgain.labels.findIndex((t) => /tabs/i.test(t))
    console.log('tabs segment index:', tabsIdx, JSON.stringify(classAgain.labels))
    if (tabsIdx < 0) throw new Error('the main wheel has no Tabs segment')

    const wheelT2 = await waitFor(async () =>
      (await targets()).find((t) => t.url.includes('mode=wheel')), 'the wheel overlay', 15000)
    const wheelUi2 = await connect(wheelT2)
    await wheelUi2.eval(`(document.querySelector('[data-seg="${tabsIdx}"]')
      .dispatchEvent(new MouseEvent('mousedown', {bubbles:true, button:2})), 'ok')`)

    const opened = await waitFor(async () => {
      for (const t of (await targets()).filter((x) => x.url.includes('mode=content'))) {
        const c = await connect(t)
        const sym = await c.eval(
          `document.querySelector('[data-opt-symbol]')?.dataset.optSymbol ?? null`)
        if (sym) return sym
      }
      return null
    }, 'a tab actually showing the Opt page', 20000).catch(() => null)
    console.log('PREDICT opened:', JSON.stringify(opened))
    const openOk = opened === 'SPY'

    // ---- A HELD CONTRACT GOES TO ITS OWN OPT PAGE (Kade's correction).
    // Grab a contract off a chain and ask where it goes: the answer is that
    // contract's workstation -- its strike, its expiration, its history --
    // NOT the ticker page it was grabbed from, which is where the user
    // already is. The hint must name it and the release must land on it,
    // with the contract SELECTED rather than a blank page to click back into.
    const HELD_OCC = 'SPY260918P00755000'
    await spy.eval(`window.grindstone.request('POST','/api/notepad',{
      payload:{v:1,kind:'contract',
        data:{occ_symbol:'${HELD_OCC}',expiration:'2026-09-18',strike:755,
              right:'P',bid:38.1,ask:38.9,iv:0.19,delta:-0.39},
        provenance:{workspace:'user',capturedAt:new Date().toISOString(),
                    page:'e2e',address:'spy.gs',symbol:'SPY'}},
      label:'held 755P'}).then((r)=>r.status)`)
    const destHint = await hintsNow()
    console.log('DEST hint:', JSON.stringify(destHint.hints))
    const destIdx = destHint.labels.findIndex((t) => /tabs/i.test(t))
    const wheelT3 = await waitFor(async () =>
      (await targets()).find((t) => t.url.includes('mode=wheel')), 'the wheel overlay', 15000)
    const wheelUi3 = await connect(wheelT3)
    await wheelUi3.eval(`(document.querySelector('[data-seg="${destIdx}"]')
      .dispatchEvent(new MouseEvent('mousedown', {bubbles:true, button:2})), 'ok')`)

    const landed = await waitFor(async () => {
      for (const t of (await targets()).filter((x) => x.url.includes('mode=content'))) {
        const c = await connect(t)
        const raw = await c.eval(`JSON.stringify({
          sym: document.querySelector('[data-opt-symbol]')?.dataset.optSymbol ?? null,
          addr: document.querySelector('[data-tab-address]')?.dataset.tabAddress ?? null,
          body: (document.body.textContent || '').slice(0, 4000),
        })`)
        const st = JSON.parse(raw)
        // The page must be showing the CONTRACT, not merely the symbol: 755
        // and the September expiry have to appear on it.
        if (st.sym === 'SPY' && /755/.test(st.body)) return st
      }
      return null
    }, 'the Opt page opened ON the held contract', 25000).catch(() => null)
    console.log('DEST landed:', landed ? 'yes (755 present)' : 'no')
    const destOk = !!landed && destHint.hints.some((h) => h.includes('held 755P'))

    // ---- THE FLICK MUST STILL NAVIGATE (the regression Kade caught).
    // A hold-release is dispatched as 'quick' too, so a prediction gated on
    // intent alone ate this gesture: sweeping onto Tabs closed the wheel and
    // did nothing. Every assertion above drove CLICK mode and none of them
    // noticed. So drive the real flick: press, travel east past the click
    // threshold, release over Tabs -- the tabs wheel must open AND STAY.
    await spy.eval(`(window.grindstone.wheelEvt('down', 900, 300,
        {context:'chain', symbols:['SPY']}), 'ok')`)
    await sleep(350)
    for (const dx of [40, 90, 150]) {
      await spy.eval(`(window.grindstone.wheelEvt('move', ${900 + dx}, 300), 'ok')`)
    }
    await spy.eval(`(window.grindstone.wheelEvt('up', ${900 + 170}, 300), 'ok')`)
    const flicked = await waitFor(async () => {
      const t = (await targets()).find((x) => x.url.includes('mode=wheel'))
      if (!t) return null
      const ui = await connect(t)
      const raw = await ui.eval(`JSON.stringify({
        id: document.querySelector('.wheel-face')?.dataset.wheel ?? null,
        segs: document.querySelectorAll('.wf-seg').length,
      })`)
      const st = JSON.parse(raw)
      return st.id === 'tabs' ? st : null
    }, 'the flick to open the tabs wheel', 10000).catch(() => null)
    console.log('FLICK:', JSON.stringify(flicked))
    const flickOk = !!flicked && flicked.id === 'tabs'



    process.exit(
      seen !== null && survived && padOk && predictOk && openOk && flickOk && destOk
        ? 0 : 1)
  }

  // SHOT_PAGE=data screenshots the Data and Settings pages instead of the Opt
  // page. Same boot, same scratch profile — a second harness would duplicate
  // ninety lines of auth just to open a different tab.
  // SHOT_PAGE=opthist: Kade's visual verification — the SPXL history chart
  // must PLOT through the current session, not merely receive it. The June-29
  // cliff lived exactly in that gap: serieshistory answered with today in it,
  // and the chart projected the option onto an underlying spine whose limited
  // bars request came back oldest-first and stopped six weeks short. So the
  // assertion reads data-series-last (what was DRAWN) and the screenshot is
  // the human-checkable axis.
  if (process.env.SHOT_PAGE === 'opthist') {
    // The Opt page analyses the LEGS on the symbol's daily chart — with none
    // it renders the "no legs yet" idle card and no History toggle exists.
    const seeded = await home.eval(`window.grindstone.request('PUT','/api/chart-objects',{
      key: 'SPXL|1Day',
      doc: { legs: [{
        id: 'lgx1', side: 'short', right: 'P', expiration: '2026-09-04',
        strike: 267.5, dteTol: 3, strikeTol: 25, slot: 0,
        pick: 'SPXL260904P00267500'
      }] }
    }).then((r)=>r.status)`)
    console.log('SPXL leg seed:', seeded)
    await home.eval(`window.grindstone.openTab('opt:SPXL:SPXL260904P00267500')`)
    const opt = await waitFor(async () => {
      const ts = await targets()
      for (const t of ts.filter((x) => x.url.includes('mode=content'))) {
        const c = await connect(t)
        if (await c.eval(`document.querySelector('[data-opt-symbol="SPXL"]') !== null`)) return c
      }
      return null
    }, 'the SPXL Opt page', 30000)
    await sleep(1500)
    const hist = await opt.eval(`(() => {
      const b = [...document.querySelectorAll('button')]
        .find((x) => x.textContent.trim() === 'History')
      if (!b) return 'no-history-button'
      b.click(); return 'ok' })()`)
    console.log('history tab:', hist)
    const last = await waitFor(async () => {
      const v = await opt.eval(
        `document.querySelector('[data-series-last]')?.dataset.seriesLast ?? null`)
      return v || null
    }, 'the history chart to plot', 25000).catch(() => null)
    const n = await opt.eval(
      `document.querySelector('[data-series-n]')?.dataset.seriesN ?? '0'`)
    console.log('HIST last plotted:', last, '| points:', n)
    await opt.send('Emulation.setDeviceMetricsOverride', {
      width: 1500, height: 950, deviceScaleFactor: 1, mobile: false,
    })
    await sleep(1200)
    await shot(opt, 'opthist-spxl.png')
    // The archive's own last SPXL session is the honest expectation — the
    // scratch profile carries a copy of the real options_history.db.
    const sq = await home.eval(
      `window.grindstone.request('GET','/api/symbols/SPXL/options/serieshistory'
        + '?right=P&dte=24&dte_tol=3&delta=-0.22&delta_tol=0.08')
        .then((r)=> r.body && r.body.last ? r.body.last : 'none')`)
    console.log('archive last session:', sq)
    const ok = !!last && sq !== 'none' && last >= sq
    console.log('OPTHIST:', JSON.stringify({ plotted_to: last, archive_last: sq, ok }))
    process.exit(ok ? 0 : 1)
  }

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

  // ---- THE ANNUALISED UNIT, against the heatmap cell it must match --------
  // This unit exists because the two panels disagreed on screen. Proving it in
  // the REAL app means reading both surfaces at once: the chart's today line
  // and the picked cell's own printed rate.
  const setUnit = (v) => opt.eval(`(() => {
    const s = document.querySelector('.opt-unit')
    if (!s) return 'no-select'
    s.value = ${JSON.stringify(v)}
    s.dispatchEvent(new Event('change', { bubbles: true }))
    return s.value
  })()`)
  const peakOf = () => opt.eval(
    `document.querySelector('.opt-card')?.getAttribute('data-peak') ?? ''`)

  // WHAT THIS HARNESS CAN AND CANNOT PROVE. The scratch profile carries no
  // market keys, so there is no live chain: no today line and no heatmap cells.
  // The exact chart-equals-cell equality is therefore pinned in the OFFLINE
  // gate (_chart_legs, ===) where it can be checked to the last bit. What is
  // proved HERE is the rendering half — the unit switches, the axis rescales
  // per-point, and the caption names the unit it is drawing.
  await setUnit('pct')
  await sleep(2000)
  const rawPeak = await peakOf()
  await setUnit('annual')
  await sleep(2500)
  const annPeak = await peakOf()
  console.log('ANNUAL PROOF:', JSON.stringify({
    pct_peak: rawPeak,
    annual_peak: annPeak,
    // Annualising a ~38-DTE series scales by 252/~27 sessions, so the peak must
    // climb by roughly an order of magnitude. Equality here would mean the unit
    // relabelled the axis without touching the data.
    scaled: !!rawPeak && !!annPeak && Number(annPeak) > Number(rawPeak) * 2,
    unit: await opt.eval(`document.querySelector('.opt-card')?.getAttribute('data-unit')`),
    names_its_unit: (await opt.eval(
      `(document.querySelector('.opt-note')?.textContent ?? '').includes('scaled to a year')`)),
  }))
  await shot(opt, 'opt-history-annual.png')

  // ---- THE MATCH TOGGLE: two questions, not two views of one series -------
  // Delta-matched lets the strike walk; strike-matched holds it. If the toggle
  // were cosmetic the two would plot identically, so compare the actual series.
  await setUnit('pct')
  await sleep(1500)
  const setMatch = (v) => opt.eval(`(() => {
    const s = document.querySelector('.opt-match')
    if (!s) return 'no-select'
    s.value = ${JSON.stringify(v)}
    s.dispatchEvent(new Event('change', { bubbles: true }))
    return s.value
  })()`)
  const matchState = () => opt.eval(`(() => {
    const c = document.querySelector('.opt-card')
    return JSON.stringify({
      asked: c?.getAttribute('data-match'),
      got: c?.getAttribute('data-match-got'),
      n: c?.getAttribute('data-series-points'),
      peak: c?.getAttribute('data-peak'),
      // The caption names which thing is held fixed — read it back rather
      // than trusting that the request carried the right query param.
      says_walks: (document.querySelector('.opt-note')?.textContent ?? '')
        .includes('whatever the strike'),
      says_held: (document.querySelector('.opt-note')?.textContent ?? '')
        .includes('THE STRIKE held fixed'),
    })
  })()`)
  await setMatch('delta')
  await sleep(2500)
  const byDelta = await matchState()
  await setMatch('strike')
  await sleep(2500)
  const byStrike = await matchState()
  console.log('MATCH delta :', byDelta)
  console.log('MATCH strike:', byStrike)
  // WHAT THIS HARNESS CAN PROVE, AND WHAT IT CANNOT. With no market keys there
  // is no live chain, so the selected contract carries no delta and the delta
  // match has nothing to hold fixed. That makes the DIVERGENCE path the thing
  // under test here: the page must ask for delta, receive strike, and SAY SO —
  // silently answering a different question is the failure mode. That the two
  // modes plot differently when a delta IS known is pinned in the offline gate
  // (_options_chain: the delta match walks its strike, the strike match holds).
  const d = JSON.parse(byDelta), s = JSON.parse(byStrike)
  console.log('MATCH PROOF:', JSON.stringify({
    delta_unavailable_here: d.asked === 'delta' && d.got === 'strike',
    divergence_is_announced: (await opt.eval(
      `(document.querySelector('.opt-note')?.textContent ?? '')
         .includes('carries no delta in the feed')`)) === false, // strike mode: no note
    strike_mode_is_clean: s.asked === 'strike' && s.got === 'strike',
    both_name_what_is_held: d.says_held && s.says_held,
  }))
  await shot(opt, 'opt-history-strike.png')
  await setMatch('delta')
  await sleep(2000)

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

  // ---- THE EXIT VIEW: click a node, follow that trade out -----------------
  // lightweight-charts only hears TRUSTED input (its click derives from the
  // crosshair, which a synthetic DOM event never moves — run.mjs learned
  // this on the draw tools). So: stream mouseMoved onto the history canvas,
  // then press, then read back the data-exit-* attributes the page stamps.
  // The x is fanned across the chart until a click lands within the page's
  // 14px node tolerance — where the nodes are depends on the data.
  await setUnit('pct')
  await sleep(1500)
  const histRect = () => opt.eval(`(() => {
    const c = document.querySelector('.opt-card canvas')
    if (!c) return null
    const r = c.getBoundingClientRect()
    return JSON.stringify({ x: r.x, y: r.y, w: r.width, h: r.height })
  })()`)
  const exitState = () => opt.eval(`(() => {
    const e = document.querySelector('.opt-exit')
    return JSON.stringify({
      picked: e?.getAttribute('data-exit-picked') ?? null,
      points: e?.getAttribute('data-exit-points') ?? null,
      pnl: e?.getAttribute('data-exit-pnl') ?? null,
      canvases: document.querySelectorAll('.opt-exit canvas').length,
      caption: (e?.querySelector('.opt-note')?.textContent ?? '').slice(0, 220),
    })
  })()`)
  let clicked = false
  for (const fx of [0.55, 0.45, 0.65, 0.35, 0.75, 0.5, 0.6, 0.4, 0.7, 0.3]) {
    const rj = await histRect()
    if (!rj) break
    const r = JSON.parse(rj)
    const x = Math.round(r.x + r.w * fx)
    const y = Math.round(r.y + r.h * 0.5)
    // Crosshair first (three strides, the draw tools' own pattern), then press.
    for (const dx of [-24, -9, 0]) {
      await opt.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: x + dx, y })
      await sleep(30)
    }
    await opt.send('Input.dispatchMouseEvent',
      { type: 'mousePressed', x, y, button: 'left', clickCount: 1 })
    await opt.send('Input.dispatchMouseEvent',
      { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 })
    await sleep(1200)
    const st = JSON.parse(await exitState())
    if (st.picked) {
      clicked = true
      // Wait for the fetch to RESOLVE, not merely for the panel to exist:
      // the loading placeholder is also a non-empty .lc-empty, so accepting
      // any text made this wait a tautology that could screenshot the
      // pre-fetch state and log a working feature as broken.
      await waitFor(() => opt.eval(
        `(() => {
           if (document.querySelectorAll('.opt-exit canvas').length >= 1) return true
           const t = document.querySelector('.opt-exit .lc-empty')?.textContent ?? ''
           return t !== '' && !t.startsWith('loading')
         })()`),
        'the exit sub-chart or its settled reason', 15000)
      await sleep(1500)
      break
    }
  }
  const finalExit = await exitState()
  console.log('EXIT PROOF:', JSON.stringify({
    node_clicked: clicked,
    ...JSON.parse(finalExit),
    // The caption must NAME the denominator — an unlabelled % here would be
    // the two-panels-disagree bug reborn one level down.
    names_denominator: (await opt.eval(
      `(document.querySelector('.opt-exit .opt-note')?.textContent ?? '')
         .includes('buying power')`)),
  }))
  await shot(opt, 'opt-history-exit.png')
  // The CLOSE control dismisses the view. (Re-clicking the node toggles too,
  // but through the same setter — and re-landing a canvas click is a
  // crosshair race this harness kept losing, while a DOM button is not.)
  if (clicked) {
    const was = JSON.parse(finalExit).picked
    await opt.eval(`[...document.querySelectorAll('.opt-exit-head button')]
      .find((b) => b.textContent === 'close')?.click()`)
    await sleep(800)
    const now = JSON.parse(await exitState())
    console.log('EXIT CLOSE:', JSON.stringify({
      was, now: now.picked, closed: now.picked === null }))
  }
} catch (e) {
  console.log('FAILED:', e.message)
} finally {
  try {
    child.kill()
  } catch { /* gone */ }
}
console.log('done')
