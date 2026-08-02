/**
 * The tab system (REQUIREMENTS.md FR-SHELL-1..3, 6.1).
 *
 * Chrome's architecture, deliberately: every tab is a live WebContentsView;
 * the tab strip is its own view rendered by our own UI; moving a tab between
 * windows RE-PARENTS the existing view — the page keeps running, nothing
 * reloads. Windows are frameless; the strip doubles as the title bar.
 *
 * Drag protocol (chrome renderer -> main):
 *   tabdrag:start {tabId} -> tabdrag:move {sx, sy} -> tabdrag:end {sx, sy}
 * While dragging over a strip: live reorder (same window) or highlight
 * (other window). On release: over a strip -> adopt into that window at the
 * hovered index; outside every strip -> tear off into a new window there.
 */
import { BaseWindow, WebContentsView, ipcMain, shell } from 'electron'
import path from 'node:path'
import { log } from './log'

export const TABBAR_H = 40
const MIN_W = 900
const MIN_H = 600
const DARK_BG = '#101214'
const NEW_WINDOW_SIZE = { width: 1100, height: 720 }

export interface TabInfo {
  id: number
  title: string
  icon: string // page-type key the strip maps to an icon
}

interface Tab {
  id: number
  view: WebContentsView
  title: string
  icon: string
}

interface Win {
  id: number
  win: BaseWindow
  chrome: WebContentsView
  tabs: Tab[]
  activeId: number | null
}

type StripState = {
  tabs: TabInfo[]
  activeId: number | null
  maximized: boolean
  bounds: { x: number; y: number; width: number; height: number }
}

export class TabManager {
  private wins: Win[] = []
  private nextTabId = 1
  private nextWinId = 1
  private preload: string
  private locked = true
  private drag: { tabId: number; overWinId: number | null } | null = null
  private quitting = false
  onAllClosed: (() => void) | null = null

  constructor(preload: string) {
    this.preload = preload
    this.registerIpc()
  }

  // ------------------------------------------------------------ url helpers
  private rendererUrl(params: Record<string, string>): { load: (wc: Electron.WebContents) => void } {
    const dev = process.env['ELECTRON_RENDERER_URL']
    const qs = new URLSearchParams(params).toString()
    return {
      load: (wc) => {
        if (dev) wc.loadURL(`${dev}/?${qs}`)
        else wc.loadFile(path.join(__dirname, '../renderer/index.html'), { search: `?${qs}` })
      },
    }
  }

  private makeView(params: Record<string, string>): WebContentsView {
    const view = new WebContentsView({
      webPreferences: {
        preload: this.preload,
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
      },
    })
    view.setBackgroundColor(DARK_BG)
    view.webContents.setWindowOpenHandler(({ url }) => {
      if (url.startsWith('https://')) shell.openExternal(url)
      return { action: 'deny' }
    })
    const dev = process.env['ELECTRON_RENDERER_URL']
    view.webContents.on('will-navigate', (event, url) => {
      const allowed = dev ? url.startsWith(dev) : url.startsWith('file://')
      if (!allowed) {
        event.preventDefault()
        if (url.startsWith('https://')) shell.openExternal(url)
      }
    })
    this.rendererUrl(params).load(view.webContents)
    return view
  }

  // -------------------------------------------------------------- windows
  createWindow(at?: { x: number; y: number }): Win {
    const win = new BaseWindow({
      ...NEW_WINDOW_SIZE,
      ...(at ? { x: at.x, y: at.y } : {}),
      minWidth: MIN_W,
      minHeight: MIN_H,
      frame: false,
      backgroundColor: DARK_BG,
      title: 'Grindstone',
      show: false,
    })
    const chrome = this.makeView({ mode: this.locked ? 'auth' : 'chrome' })
    win.contentView.addChildView(chrome)

    const w: Win = { id: this.nextWinId++, win, chrome, tabs: [], activeId: null }
    this.wins.push(w)

    const relayout = () => this.layout(w)
    win.on('resize', relayout)
    win.on('maximize', () => this.pushStrip(w))
    win.on('unmaximize', () => this.pushStrip(w))
    win.on('closed', () => this.onWindowClosed(w))
    this.layout(w)
    chrome.webContents.once('did-finish-load', () => win.show())
    return w
  }

  private onWindowClosed(w: Win) {
    // Electron does NOT destroy a window's WebContentsViews when the window
    // closes — they leak as orphaned renderers (observed: a closed window's
    // chrome view stayed alive as a live CDP target and answered IPC with a
    // window that no longer existed). Close every view we own, chrome
    // included. A user-closed window closes its tabs (Chrome parity).
    for (const t of w.tabs) t.view.webContents.close()
    w.tabs = []
    w.chrome.webContents.close()
    this.wins = this.wins.filter((x) => x !== w)
    log('window closed', w.id, 'remaining', this.wins.length)
    if (this.wins.length === 0 && !this.quitting) this.onAllClosed?.()
  }

  private layout(w: Win) {
    const { width, height } = w.win.getContentBounds()
    if (this.locked) {
      w.chrome.setBounds({ x: 0, y: 0, width, height })
      return
    }
    w.chrome.setBounds({ x: 0, y: 0, width, height: TABBAR_H })
    const active = w.tabs.find((t) => t.id === w.activeId)
    if (active) active.view.setBounds({ x: 0, y: TABBAR_H, width, height: height - TABBAR_H })
  }

  // ----------------------------------------------------------------- tabs
  newTab(w: Win, route = 'idle', activate = true): Tab {
    const tab: Tab = {
      id: this.nextTabId++,
      view: this.makeView({ mode: 'content', route }),
      title: 'New tab',
      icon: route === 'idle' ? 'home' : route,
    }
    w.tabs.push(tab)
    if (activate) this.activate(w, tab.id)
    this.pushStrip(w)
    return tab
  }

  activate(w: Win, tabId: number) {
    const tab = w.tabs.find((t) => t.id === tabId)
    if (!tab) return
    const prev = w.tabs.find((t) => t.id === w.activeId)
    if (prev && prev !== tab) w.win.contentView.removeChildView(prev.view)
    if (w.activeId !== tab.id) {
      w.win.contentView.addChildView(tab.view)
      w.activeId = tab.id
    } else if (!w.win.contentView.children.includes(tab.view)) {
      w.win.contentView.addChildView(tab.view)
    }
    this.layout(w)
    this.pushStrip(w)
  }

  closeTab(w: Win, tabId: number) {
    const idx = w.tabs.findIndex((t) => t.id === tabId)
    if (idx < 0) return
    const [tab] = w.tabs.splice(idx, 1)
    if (w.activeId === tab.id) {
      w.win.contentView.removeChildView(tab.view)
      const next = w.tabs[Math.min(idx, w.tabs.length - 1)]
      w.activeId = null
      if (next) this.activate(w, next.id)
    }
    tab.view.webContents.close()
    if (w.tabs.length === 0) w.win.close()
    else this.pushStrip(w)
  }

  /** Move a live tab between windows — the view is re-parented, never
   *  reloaded. This is the load-bearing Chrome-parity operation. */
  adoptTab(from: Win, to: Win, tabId: number, index: number) {
    const idx = from.tabs.findIndex((t) => t.id === tabId)
    if (idx < 0) return
    const [tab] = from.tabs.splice(idx, 1)
    if (from.activeId === tab.id) {
      from.win.contentView.removeChildView(tab.view)
      from.activeId = null
      const next = from.tabs[Math.min(idx, from.tabs.length - 1)]
      if (next) this.activate(from, next.id)
    }
    to.tabs.splice(Math.max(0, Math.min(index, to.tabs.length)), 0, tab)
    this.activate(to, tab.id)
    if (from.tabs.length === 0) from.win.close()
    else this.pushStrip(from)
    this.pushStrip(to)
  }

  detachToNewWindow(from: Win, tabId: number, sx: number, sy: number) {
    if (from.tabs.length === 1) {
      // Tearing the only tab off just moves the window (Chrome parity).
      from.win.setPosition(Math.round(sx - 100), Math.round(sy - 12))
      return
    }
    const to = this.createWindow({ x: Math.round(sx - 100), y: Math.round(sy - 12) })
    this.adoptTab(from, to, tabId, 0)
  }

  reorder(w: Win, tabId: number, toIndex: number) {
    const idx = w.tabs.findIndex((t) => t.id === tabId)
    if (idx < 0) return
    const [tab] = w.tabs.splice(idx, 1)
    w.tabs.splice(Math.max(0, Math.min(toIndex, w.tabs.length)), 0, tab)
    this.pushStrip(w)
  }

  // ------------------------------------------------------------- auth mode
  setLocked(locked: boolean) {
    if (this.locked === locked) return
    this.locked = locked
    if (locked) {
      // Collapse to one lock window; tabs die (their sessions are gone).
      for (const w of [...this.wins.slice(1)]) w.win.close()
      const w = this.wins[0]
      if (!w) return
      for (const t of w.tabs) {
        w.win.contentView.removeChildView(t.view)
        t.view.webContents.close()
      }
      w.tabs = []
      w.activeId = null
      this.rendererUrl({ mode: 'auth' }).load(w.chrome.webContents)
      this.layout(w)
    } else {
      const w = this.wins[0] ?? this.createWindow()
      this.rendererUrl({ mode: 'chrome' }).load(w.chrome.webContents)
      this.layout(w)
      this.newTab(w, 'idle')
    }
  }

  // -------------------------------------------------------------- strip io
  private stripState(w: Win): StripState {
    return {
      tabs: w.tabs.map((t) => ({ id: t.id, title: t.title, icon: t.icon })),
      activeId: w.activeId,
      maximized: w.win.isMaximized(),
      bounds: w.win.getContentBounds(),
    }
  }

  private pushStrip(w: Win) {
    if (!w.win.isDestroyed()) {
      w.chrome.webContents.send('tabs:state', this.stripState(w))
    }
  }

  private winFromSender(wc: Electron.WebContents): Win | undefined {
    return this.wins.find((w) => w.chrome.webContents.id === wc.id)
  }

  private winFromContent(wc: Electron.WebContents): Win | undefined {
    return this.wins.find((w) => w.tabs.some((t) => t.view.webContents.id === wc.id))
  }

  /** Which window's strip contains this screen point? */
  private stripHit(sx: number, sy: number): { win: Win; index: number } | null {
    for (const w of this.wins) {
      if (w.win.isDestroyed() || w.win.isMinimized()) continue
      const b = w.win.getContentBounds()
      if (sx >= b.x && sx <= b.x + b.width && sy >= b.y && sy <= b.y + TABBAR_H + 8) {
        const TAB_W = 180 // must match the strip CSS
        const index = Math.max(0, Math.min(Math.floor((sx - b.x) / TAB_W), w.tabs.length))
        return { win: w, index }
      }
    }
    return null
  }

  // ------------------------------------------------------------------- ipc
  private registerIpc() {
    ipcMain.on('tabs:new', (e) => {
      const w = this.winFromSender(e.sender)
      if (w && !this.locked) this.newTab(w, 'idle')
    })
    ipcMain.on('tabs:activate', (e, tabId: number) => {
      const w = this.winFromSender(e.sender)
      if (w) this.activate(w, tabId)
    })
    ipcMain.on('tabs:close', (e, tabId: number) => {
      const w = this.winFromSender(e.sender)
      if (w) this.closeTab(w, tabId)
    })
    ipcMain.on('tabs:reorder', (e, tabId: number, toIndex: number) => {
      const w = this.winFromSender(e.sender)
      if (w) this.reorder(w, tabId, toIndex)
    })
    ipcMain.handle('tabs:state', (e) => {
      const w = this.winFromSender(e.sender)
      if (!w) {
        log('tabs:state from unknown sender', e.sender.id,
            'known chrome ids', this.wins.map((x) => x.chrome.webContents.id))
        return null
      }
      return this.stripState(w)
    })

    // window controls (frameless)
    ipcMain.on('win:minimize', (e) => this.winFromSender(e.sender)?.win.minimize())
    ipcMain.on('win:maximize', (e) => {
      const w = this.winFromSender(e.sender)
      if (!w) return
      if (w.win.isMaximized()) w.win.unmaximize()
      else w.win.maximize()
    })
    ipcMain.on('win:close', (e) => this.winFromSender(e.sender)?.win.close())

    // content tabs report their identity (title/icon) as the user navigates
    ipcMain.on('tab:meta', (e, meta: { title?: string; icon?: string }) => {
      const w = this.winFromContent(e.sender)
      const tab = w?.tabs.find((t) => t.view.webContents.id === e.sender.id)
      if (!w || !tab) return
      if (typeof meta.title === 'string') tab.title = meta.title.slice(0, 80)
      if (typeof meta.icon === 'string') tab.icon = meta.icon.slice(0, 24)
      this.pushStrip(w)
    })
    // content asks to open a route in a NEW tab (ctrl+click behavior)
    ipcMain.on('tab:open', (e, route: string) => {
      const w = this.winFromContent(e.sender)
      if (w && !this.locked && typeof route === 'string') this.newTab(w, route.slice(0, 200))
    })

    // ------------------------------------------------------------- dragging
    ipcMain.on('tabdrag:start', (e, tabId: number) => {
      const w = this.winFromSender(e.sender)
      if (w && w.tabs.some((t) => t.id === tabId)) {
        this.drag = { tabId, overWinId: null }
      }
    })
    ipcMain.on('tabdrag:move', (_e, sx: number, sy: number) => {
      if (!this.drag) return
      const src = this.wins.find((w) => w.tabs.some((t) => t.id === this.drag!.tabId))
      if (!src) {
        this.drag = null
        return
      }
      const hit = this.stripHit(sx, sy)
      this.drag.overWinId = hit?.win.id ?? null
      // Live reorder while dragging within the SOURCE window's strip.
      if (hit && hit.win === src) this.reorder(src, this.drag.tabId, hit.index)
    })
    ipcMain.on('tabdrag:end', (_e, sx: number, sy: number) => {
      const drag = this.drag
      this.drag = null
      if (!drag) {
        log('tabdrag:end with no active drag')
        return
      }
      const src = this.wins.find((w) => w.tabs.some((t) => t.id === drag.tabId))
      if (!src) {
        log('tabdrag:end: source window for tab', drag.tabId, 'is gone')
        return
      }
      const hit = this.stripHit(sx, sy)
      log('tabdrag:end tab', drag.tabId, 'at', { sx, sy },
          'src win', src.id, 'hit', hit ? { win: hit.win.id, index: hit.index } : null,
          'windows', this.wins.map((w) => ({ id: w.id, b: w.win.getContentBounds(), tabs: w.tabs.length })))
      if (!hit) {
        this.detachToNewWindow(src, drag.tabId, sx, sy)
      } else if (hit.win !== src) {
        this.adoptTab(src, hit.win, drag.tabId, hit.index)
      }
      // same-window drop: reorder already happened live
    })
  }

  // ------------------------------------------------------------ lifecycle
  bootstrap() {
    this.createWindow()
  }

  broadcastToContent(channel: string, payload: unknown) {
    for (const w of this.wins) {
      for (const t of w.tabs) t.view.webContents.send(channel, payload)
      w.chrome.webContents.send(channel, payload)
    }
  }

  shutdown() {
    this.quitting = true
    for (const w of [...this.wins]) w.win.close()
  }
}
