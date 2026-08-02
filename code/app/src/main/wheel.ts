/**
 * Gesture wheels (FR-SHELL-4): SolidWorks-style right-click radial menus.
 *
 * Two ways in, exactly as specced:
 *   CLICK  — press and release without moving: the wheel stays up with the
 *            lock/unlock hub in the center; LEFT clicks interact; left click
 *            outside closes; right click moves the wheel.
 *   HOLD   — press, drag over a segment, release: the segment acts. Releasing
 *            over a wheel-nav segment switches wheels and KEEPS the wheel
 *            open in click mode (so the new wheel does not auto-despawn);
 *            releasing in the center dead-zone or over nothing despawns.
 *            No lock hub while holding.
 *
 * Architecture: ONE transparent WebContentsView overlay, attached to
 * whichever window spawned the wheel and removed on despawn. Right-button
 * events reach main from every view kind — app pages and the chrome via the
 * bridge, third-party pages via the minimal browser preload — because during
 * a hold the ORIGIN view has mouse capture and the overlay may never hear a
 * thing. Main is the single state machine; the overlay is the face.
 */
import { WebContentsView, ipcMain } from 'electron'
import path from 'node:path'
import { isSignedIn, mainRequest } from './api'
import { log } from './log'
import { NAVBAR_H, TABBAR_H, TabManager, WheelTabInfo } from './tabs'

// Geometry lives in src/shared/wheelGeometry.ts, imported by BOTH sides.
// Main computes the selection ITSELF at release: an earlier design had the
// overlay report hover back over IPC, but the release event can beat the
// last hover report across the process boundary — acting on a stale segment.
import { WHEEL_RADIUS, segmentAt } from '../shared/wheelGeometry'

const CLICK_MOVE_THRESHOLD = 10 // px of travel that turns a click into a hold
const EDGE_MARGIN = 12

interface Segment {
  type: 'wheel' | 'nav' | 'tool' | 'ticker' | 'placeholder' | 'empty' | 'tab' | 'page'
  label: string
  // one of, depending on type:
  wheel?: string
  route?: string
  tool?: string
  ticker?: string
  tabId?: number
  dir?: number // page: -1 back / +1 forward
  icon?: string
  symbol?: string // the target wheel's symbol, for wheel segments
  disabled?: boolean
}

interface WheelDef {
  id: string
  name: string
  symbol: string
  dynamic?: string
  segments: Segment[]
}

interface WheelDoc {
  config: { ticker_display: 'percent' | 'price'; ticker_colors: boolean; locked: string | null }
  wheels: WheelDef[]
}

interface Session {
  winId: number
  mode: 'pending' | 'hold' | 'click'
  center: { x: number; y: number }
  start: { x: number; y: number }
  moved: boolean
  wheelId: string
  tabPage: number
  /** Last hold-mode pointer position, window coords — the release acts on
   *  segmentAt(center, THIS), never on renderer-reported hover. */
  lastPointer: { x: number; y: number }
  doc: WheelDoc
  segments: Segment[]
  detachListeners: () => void
}

export class WheelManager {
  private tabs: TabManager
  private overlay: WebContentsView | null = null
  private session: Session | null = null
  /** spawn() awaits the config fetch; a release landing inside that window
   *  must not be dropped — a very fast click would leave the wheel stuck in
   *  pending, interacting with nothing. */
  private spawnSeq = 0
  private releaseDuringSpawn = false

  constructor(tabs: TabManager) {
    this.tabs = tabs
    tabs.onWindowGone = (winId) => {
      if (this.session?.winId === winId) this.despawn()
    }
    this.registerIpc()
  }

  // ------------------------------------------------------------- the overlay
  /** Created once, loaded once, then attached/detached per spawn — a wheel
   *  must appear instantly, not after a renderer boots. */
  private ensureOverlay(preload: string): WebContentsView {
    if (this.overlay && !this.overlay.webContents.isDestroyed()) return this.overlay
    const view = new WebContentsView({
      webPreferences: {
        preload,
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
      },
    })
    view.setBackgroundColor('#00000000') // transparent: the page below stays visible
    const dev = process.env['ELECTRON_RENDERER_URL']
    const qs = 'mode=wheel'
    if (dev) view.webContents.loadURL(`${dev}/?${qs}`)
    else {
      view.webContents.loadFile(path.join(__dirname, '../renderer/index.html'), {
        search: `?${qs}`,
      })
    }
    this.overlay = view
    return view
  }

  preloadPath = ''

  // --------------------------------------------------------------- the doc
  private async loadDoc(): Promise<WheelDoc | null> {
    const res = await mainRequest<WheelDoc>('GET', '/api/wheels')
    return res.status === 200 && res.body ? res.body : null
  }

  /** Resolve a wheel id into renderable segments (dynamic wheels included). */
  private materialize(doc: WheelDoc, wheelId: string, tabPage: number): {
    def: WheelDef
    segments: Segment[]
    pages: number
  } {
    const def = doc.wheels.find((w) => w.id === wheelId) ?? doc.wheels[0]
    if (def.dynamic !== 'tabs') {
      const segments = def.segments.map((s) => ({
        ...s,
        disabled: s.type === 'placeholder' || s.type === 'empty',
        symbol: s.type === 'wheel'
          ? doc.wheels.find((w) => w.id === s.wheel)?.symbol
          : undefined,
        label: s.label || (s.type === 'ticker' ? (s.ticker ?? '') : s.label),
      }))
      return { def, segments, pages: 1 }
    }
    // The Tabs wheel is built from reality at spawn time. Eight tabs fit on
    // one wheel; more than eight paginate — 6 per page, next at E, prev at W,
    // wrapping — exactly the spec's "right goes to tab wheel 2, 3…".
    const all = this.tabs.allTabs()
    const tabSeg = (t: WheelTabInfo): Segment => ({
      type: 'tab', label: t.title.slice(0, 14), tabId: t.id, icon: t.icon,
    })
    if (all.length <= 8) {
      return { def, segments: all.map(tabSeg), pages: 1 }
    }
    const pages = Math.ceil(all.length / 6)
    const page = ((tabPage % pages) + pages) % pages
    const slice = all.slice(page * 6, page * 6 + 6).map(tabSeg)
    const segments: Segment[] = [
      ...slice.slice(0, 2),
      { type: 'page', dir: +1, label: `Tabs ${((page + 1) % pages) + 1}/${pages}` }, // E
      ...slice.slice(2, 5),
      { type: 'page', dir: -1, label: `Tabs ${((page - 1 + pages) % pages) + 1}/${pages}` }, // W
      ...slice.slice(5),
    ]
    return { def, segments, pages }
  }

  // ------------------------------------------------------------ session ops
  private clamp(winId: number, x: number, y: number): { x: number; y: number } {
    const size = this.tabs.windowContentSize(winId)
    if (!size) return { x, y }
    const r = WHEEL_RADIUS + EDGE_MARGIN
    const topMin = this.tabs.isLocked ? r : TABBAR_H + NAVBAR_H + 4 + r
    return {
      x: Math.max(r, Math.min(size.width - r, x)),
      y: Math.max(Math.min(topMin, size.height - r), Math.min(size.height - r, y)),
    }
  }

  private async spawn(winId: number, x: number, y: number): Promise<void> {
    const seq = ++this.spawnSeq
    this.releaseDuringSpawn = false
    const doc = await this.loadDoc()
    if (seq !== this.spawnSeq) return // a newer press superseded this spawn
    if (!doc) {
      log('wheel: no config (backend down or locked) — not spawning')
      return
    }
    // A dead session from a race (window closed mid-await) must not leak.
    if (this.session) this.despawn()

    const view = this.ensureOverlay(this.preloadPath)
    if (!this.tabs.attachOverlay(winId, view)) return

    const wheelId = doc.config.locked ?? 'main'
    const { def, segments } = this.materialize(doc, wheelId, 0)
    const center = this.clamp(winId, x, y)

    const win = this.tabs.baseWindow(winId)
    const onBlurOrResize = () => this.despawn()
    win?.on('blur', onBlurOrResize)
    win?.on('resize', onBlurOrResize)

    const sess: Session = {
      winId,
      mode: 'pending',
      center,
      start: { x, y },
      moved: false,
      wheelId: def.id,
      tabPage: 0,
      lastPointer: { x, y },
      doc,
      segments,
      detachListeners: () => {
        win?.off('blur', onBlurOrResize)
        win?.off('resize', onBlurOrResize)
      },
    }
    this.session = sess
    this.push('wheel:spawn', {
      center,
      mode: 'pending',
      wheel: { id: def.id, name: def.name, symbol: def.symbol, segments },
      config: doc.config,
    })
    this.fetchQuotes(segments, doc.config)

    if (this.releaseDuringSpawn) {
      // The button came back up while we were fetching config: that was a
      // click. Land in click mode instead of a pending state nobody holds.
      this.releaseDuringSpawn = false
      sess.mode = 'click'
      this.push('wheel:mode', 'click')
      this.overlay?.webContents.focus()
    }
  }

  private switchWheel(wheelId: string, tabPage = 0): void {
    const s = this.session
    if (!s) return
    const { def, segments } = this.materialize(s.doc, wheelId, tabPage)
    s.wheelId = def.id
    s.tabPage = tabPage
    s.segments = segments
    this.push('wheel:update', {
      wheel: { id: def.id, name: def.name, symbol: def.symbol, segments },
      config: s.doc.config,
    })
    this.fetchQuotes(segments, s.doc.config)
  }

  private despawn(): void {
    const s = this.session
    if (!s) return
    this.session = null
    s.detachListeners()
    this.push('wheel:despawn', null)
    if (this.overlay) this.tabs.detachOverlay(s.winId, this.overlay)
  }

  private push(channel: string, payload: unknown): void {
    if (this.overlay && !this.overlay.webContents.isDestroyed()) {
      this.overlay.webContents.send(channel, payload)
    }
  }

  /** Ticker segments show a price/% snapshot taken at spawn. Deliberately
   *  fetched ONCE — the spec forbids colors flashing while the wheel is up;
   *  close and reopen to refresh. */
  private async fetchQuotes(segments: Segment[], _config: WheelDoc['config']): Promise<void> {
    const syms = [...new Set(segments.filter((s) => s.type === 'ticker' && s.ticker)
      .map((s) => s.ticker!))]
    if (syms.length === 0) return
    const mySession = this.session
    const res = await mainRequest<{ quotes: Record<string, unknown> }>(
      'GET', `/api/quotes?symbols=${encodeURIComponent(syms.join(','))}`)
    // Only deliver to the wheel that asked; a despawn/respawn in between
    // must not paint stale numbers on a different wheel.
    if (res.status === 200 && res.body && this.session === mySession) {
      this.push('wheel:quotes', res.body.quotes)
    }
  }

  // ---------------------------------------------------------------- actions
  /** Act on a segment. Returns 'stay' if the wheel remains open. */
  private act(index: number | null): 'stay' | 'close' {
    const s = this.session
    if (!s) return 'close'
    const seg = index !== null ? s.segments[index] : undefined
    if (!seg || seg.disabled) return 'close'
    switch (seg.type) {
      case 'wheel':
        this.switchWheel(seg.wheel ?? 'main')
        return 'stay'
      case 'page':
        this.switchWheel('tabs', s.tabPage + (seg.dir ?? 1))
        return 'stay'
      case 'nav':
        this.tabs.gotoRoute(s.winId, seg.route ?? 'idle')
        return 'close'
      case 'tool':
        if (seg.tool === 'search') this.tabs.focusOmnibox(s.winId)
        return 'close'
      case 'ticker':
        if (seg.ticker) this.tabs.openTicker(s.winId, seg.ticker)
        return 'close'
      case 'tab':
        if (seg.tabId !== undefined) this.tabs.activateTabGlobal(seg.tabId)
        return 'close'
      default:
        return 'close'
    }
  }

  private async toggleLock(): Promise<void> {
    const s = this.session
    if (!s) return
    const doc = s.doc
    doc.config.locked = doc.config.locked === s.wheelId ? null : s.wheelId
    const res = await mainRequest('PUT', '/api/wheels', doc)
    if (res.status !== 200) {
      log('wheel: lock persist failed', res.status)
      return
    }
    this.push('wheel:update', {
      wheel: {
        id: s.wheelId,
        name: doc.wheels.find((w) => w.id === s.wheelId)?.name ?? s.wheelId,
        symbol: doc.wheels.find((w) => w.id === s.wheelId)?.symbol ?? '',
        segments: s.segments,
      },
      config: doc.config,
    })
  }

  // -------------------------------------------------------------------- ipc
  private registerIpc(): void {
    // Right-button stream, from ANY view: app pages and chrome via the
    // bridge, browser pages via the minimal preload, and the overlay itself
    // (during a hold, Chromium routes events to whoever has capture — we
    // listen everywhere so it does not matter who that is).
    ipcMain.on('wheel:evt', (e, kind: string, cx: number, cy: number) => {
      if (this.tabs.isLocked || !isSignedIn()) return
      if (typeof cx !== 'number' || typeof cy !== 'number') return

      const fromOverlay = this.overlay?.webContents.id === e.sender.id
      let winId: number
      let x = cx
      let y = cy
      if (fromOverlay) {
        if (!this.session) return
        winId = this.session.winId
      } else {
        const src = this.tabs.resolveSender(e.sender)
        if (!src) return
        winId = src.winId
        x = cx + src.offsetX
        y = cy + src.offsetY
      }

      const s = this.session
      if (kind === 'down') {
        // A fresh right-press while a wheel is up: reposition if it is this
        // window's click-mode wheel; otherwise start over where the user is.
        if (s && s.mode === 'click' && s.winId === winId) {
          s.center = this.clamp(winId, x, y)
          this.push('wheel:update', { center: s.center })
          return
        }
        if (s) this.despawn()
        void this.spawn(winId, x, y)
        return
      }
      if (!s) {
        // No session yet: a spawn may be mid-fetch — remember the release.
        if (kind === 'up') this.releaseDuringSpawn = true
        return
      }
      if (s.winId !== winId) return

      if (kind === 'move') {
        if (s.mode === 'click') return // click mode: the overlay owns hover
        if (!s.moved
            && Math.hypot(x - s.start.x, y - s.start.y) >= CLICK_MOVE_THRESHOLD) {
          s.moved = true
          s.mode = 'hold'
          this.push('wheel:mode', 'hold')
        }
        if (s.mode === 'hold') {
          s.lastPointer = { x, y }
          if (this.overlay && !this.overlay.webContents.isDestroyed()) {
            this.overlay.webContents.send('wheel:pointer', x, y)
          }
        }
        return
      }
      if (kind === 'up') {
        if (s.mode === 'pending') {
          // No travel: this was a CLICK. The wheel stays, grows its lock
          // hub, and left clicks take over.
          s.mode = 'click'
          this.push('wheel:mode', 'click')
          this.overlay?.webContents.focus() // so Escape closes it
          return
        }
        if (s.mode === 'hold') {
          const index = segmentAt(s.center.x, s.center.y,
                                  s.lastPointer.x, s.lastPointer.y,
                                  s.segments.length)
          const result = this.act(index)
          if (result === 'stay') {
            // Spec: after a wheel-nav during a hold, enter left-click mode —
            // the freshly opened wheel must not vanish under the cursor.
            s.mode = 'click'
            this.push('wheel:mode', 'click')
            this.overlay?.webContents.focus()
          } else {
            this.despawn()
          }
        }
      }
    })
    ipcMain.on('wheelui:ready', (e) => {
      if (this.overlay?.webContents.id !== e.sender.id) return
      // The first spawn of an app run raced the overlay's page load and its
      // payload evaporated — replay the LIVE session state now that someone
      // is listening. No session = nothing to replay.
      const s = this.session
      if (!s) return
      const def = s.doc.wheels.find((w) => w.id === s.wheelId)
      this.push('wheel:spawn', {
        center: s.center,
        mode: s.mode,
        wheel: {
          id: s.wheelId,
          name: def?.name ?? s.wheelId,
          symbol: def?.symbol ?? '',
          segments: s.segments,
        },
        config: s.doc.config,
      })
      this.fetchQuotes(s.segments, s.doc.config)
      if (s.mode === 'click') this.overlay?.webContents.focus()
    })
    ipcMain.on('wheelui:act', (e, index: number) => {
      if (this.overlay?.webContents.id !== e.sender.id) return
      if (!this.session || this.session.mode !== 'click') return
      if (this.act(typeof index === 'number' ? index : null) === 'close') this.despawn()
    })
    ipcMain.on('wheelui:lock', (e) => {
      if (this.overlay?.webContents.id !== e.sender.id) return
      if (this.session?.mode === 'click') void this.toggleLock()
    })
    ipcMain.on('wheelui:close', (e) => {
      if (this.overlay?.webContents.id !== e.sender.id) return
      this.despawn()
    })
    ipcMain.on('wheelui:move', (e, x: number, y: number) => {
      if (this.overlay?.webContents.id !== e.sender.id) return
      const s = this.session
      if (!s || s.mode !== 'click') return
      if (typeof x !== 'number' || typeof y !== 'number') return
      s.center = this.clamp(s.winId, x, y)
      this.push('wheel:update', { center: s.center })
    })
  }
}
