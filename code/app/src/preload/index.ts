/**
 * The ONLY surface renderers get. No tokens, no Node, no direct network.
 * `grindstone` is the data bridge; `grindstoneTabs` is the shell bridge the
 * tab strip uses (content pages use a small slice: meta, open, openUrl, nav).
 *
 * In-app browser tabs get NO preload at all — they never see any of this.
 */
import { contextBridge, ipcRenderer } from 'electron'

export interface ApiResponse<T = unknown> {
  status: number
  body: T
}

const grindstone = {
  request: <T = unknown>(
    method: string,
    path: string,
    body?: unknown
  ): Promise<ApiResponse<T>> => ipcRenderer.invoke('api:request', { method, path, body }),
  /** Native file picker; resolves to an absolute path, or null if cancelled.
   *  The renderer never sees file bytes — it hands the backend a path. */
  pickFile: (): Promise<string | null> => ipcRenderer.invoke('data:pickFile'),

  onSidecarStatus: (cb: (s: { status: string; detail?: string }) => void): (() => void) => {
    const listener = (_e: unknown, payload: { status: string; detail?: string }) => cb(payload)
    ipcRenderer.on('sidecar:status', listener)
    return () => ipcRenderer.removeListener('sidecar:status', listener)
  },

  /** Content pages report tab identity, history depth, and their .gs address. */
  setTabMeta: (title: string, icon: string, depth: number, address: string): void => {
    ipcRenderer.send('tab:meta', { title, icon, depth, address })
  },
  /** Any view mutated the favorites store (main broadcasts from the API
   *  proxy): the home grid, the omnibox star, and the picker all refetch. */
  onFavoritesChanged: (cb: () => void): (() => void) => {
    const listener = () => cb()
    ipcRenderer.on('favorites:changed', listener)
    return () => ipcRenderer.removeListener('favorites:changed', listener)
  },
  /** The lock screen calls this once sign-in has RESOLVED in its hands.
   *  Main then swaps this window into tab mode — doing it any earlier
   *  destroys the frame before it receives the reply. */
  signalUnlocked: (): void => {
    ipcRenderer.send('auth:unlocked')
  },
  /** Open an app route in a NEW tab of this window. */
  openTab: (route: string): void => {
    ipcRenderer.send('tab:open', route)
  },
  /** Open a URL as an in-app browser tab (news articles stay in the app). */
  openUrl: (url: string): void => {
    ipcRenderer.send('tab:openUrl', url)
  },
  /** Open a backtest run's HTML report in a browser tab. Main mints the
   *  single-use report key and builds the URL — the renderer never holds
   *  the sidecar port or anything key-shaped. */
  openBacktestReport: (runId: number, file?: string): void => {
    ipcRenderer.send('backtest:openReport', runId, file)
  },
  /** Chrome's Back/Home buttons reach the active content page through main. */
  onNav: (cb: (what: 'back' | 'home') => void): (() => void) => {
    const back = () => cb('back')
    const home = () => cb('home')
    ipcRenderer.on('nav:back', back)
    ipcRenderer.on('nav:home', home)
    return () => {
      ipcRenderer.removeListener('nav:back', back)
      ipcRenderer.removeListener('nav:home', home)
    }
  },
  /** The omnibox asking this tab to go to a platform route (.gs address). */
  onRoute: (cb: (route: string) => void): (() => void) => {
    const listener = (_e: unknown, route: string) => cb(route)
    ipcRenderer.on('nav:route', listener)
    return () => ipcRenderer.removeListener('nav:route', listener)
  },
  /** Raw right-button events for the gesture wheel (chrome + content modes).
   *  Browser tabs forward the same events from their own minimal preload.
   *  ctx carries what was under the cursor (a chart and its state) so main
   *  can pick the context wheel — chart charts, default everywhere else. */
  wheelEvt: (
    kind: 'down' | 'move' | 'up',
    x: number,
    y: number,
    ctx?: {
      context: string
      symbols?: string[]
      indicators?: string[]
      hidden?: string[]
      timeframe?: string
      /** 'drawhidden' | 'indhidden' — global visibility states, for marks. */
      flags?: string[]
      /** The soloed symbol when isolation is active. */
      isolated?: string
    }
  ): void => {
    ipcRenderer.send('wheel:evt', kind, x, y, ctx ?? null)
  },
  /** Chart-wheel segments land here: the page that owns the chart the wheel
   *  was spawned over receives {tool, symbol?}. */
  onChartAction: (cb: (a: { tool: string; symbol?: string }) => void): (() => void) => {
    const listener = (_e: unknown, a: { tool: string; symbol?: string }) => cb(a)
    ipcRenderer.on('chart:action', listener)
    return () => ipcRenderer.removeListener('chart:action', listener)
  },
}

/** The gesture-wheel overlay's own surface (mode=wheel only). */
const grindstoneWheel = {
  onSpawn: (cb: (payload: unknown) => void): (() => void) => {
    const listener = (_e: unknown, p: unknown) => cb(p)
    ipcRenderer.on('wheel:spawn', listener)
    return () => ipcRenderer.removeListener('wheel:spawn', listener)
  },
  onUpdate: (cb: (payload: unknown) => void): (() => void) => {
    const listener = (_e: unknown, p: unknown) => cb(p)
    ipcRenderer.on('wheel:update', listener)
    return () => ipcRenderer.removeListener('wheel:update', listener)
  },
  onMode: (cb: (mode: string) => void): (() => void) => {
    const listener = (_e: unknown, m: string) => cb(m)
    ipcRenderer.on('wheel:mode', listener)
    return () => ipcRenderer.removeListener('wheel:mode', listener)
  },
  onPointer: (cb: (x: number, y: number) => void): (() => void) => {
    const listener = (_e: unknown, x: number, y: number) => cb(x, y)
    ipcRenderer.on('wheel:pointer', listener)
    return () => ipcRenderer.removeListener('wheel:pointer', listener)
  },
  onQuotes: (cb: (quotes: unknown) => void): (() => void) => {
    const listener = (_e: unknown, q: unknown) => cb(q)
    ipcRenderer.on('wheel:quotes', listener)
    return () => ipcRenderer.removeListener('wheel:quotes', listener)
  },
  onDespawn: (cb: () => void): (() => void) => {
    const listener = () => cb()
    ipcRenderer.on('wheel:despawn', listener)
    return () => ipcRenderer.removeListener('wheel:despawn', listener)
  },
  /** Mount handshake: the first spawn can beat the overlay's listeners, so
   *  main re-sends the live session when the page says it is ready. */
  ready: (): void => ipcRenderer.send('wheelui:ready'),
  act: (index: number): void => ipcRenderer.send('wheelui:act', index),
  lockToggle: (): void => ipcRenderer.send('wheelui:lock'),
  close: (): void => ipcRenderer.send('wheelui:close'),
  move: (x: number, y: number): void => ipcRenderer.send('wheelui:move', x, y),
  /** Hold-mode input that Chromium routed to the overlay instead of the
   *  origin view — forwarded so main hears it from wherever it lands. */
  evt: (kind: 'move' | 'up', x: number, y: number): void => {
    ipcRenderer.send('wheel:evt', kind, x, y)
  },
}

export interface StripTab {
  id: number
  title: string
  icon: string
  kind: 'app' | 'browser'
  url?: string
}
export interface StripState {
  tabs: StripTab[]
  activeId: number | null
  maximized: boolean
  bounds: { x: number; y: number; width: number; height: number }
  canGoBack: boolean
  canGoForward: boolean
  activeKind: 'app' | 'browser' | null
  activeUrl: string
  /** The active BROWSER tab's favicon URL ('' otherwise) — the star's icon_url. */
  activeFavicon: string
  loading: boolean
  draggingId: number | null
  /** Chrome-style split view: the paired tabs and the divider position. */
  split: { aId: number; bId: number; ratio: number } | null
  prevId: number | null
}

const grindstoneTabs = {
  getState: (): Promise<StripState | null> => ipcRenderer.invoke('tabs:state'),
  onState: (cb: (s: StripState) => void): (() => void) => {
    const listener = (_e: unknown, s: StripState) => cb(s)
    ipcRenderer.on('tabs:state', listener)
    return () => ipcRenderer.removeListener('tabs:state', listener)
  },
  newTab: (): void => ipcRenderer.send('tabs:new'),
  /** Jump to the previously active tab (alt-tab for tabs). */
  prevTab: (): void => ipcRenderer.send('tabs:prev'),
  activate: (id: number): void => ipcRenderer.send('tabs:activate', id),
  close: (id: number): void => ipcRenderer.send('tabs:close', id),
  reorder: (id: number, toIndex: number): void => ipcRenderer.send('tabs:reorder', id, toIndex),
  dragStart: (id: number): void => ipcRenderer.send('tabdrag:start', id),
  dragMove: (sx: number, sy: number): void => ipcRenderer.send('tabdrag:move', sx, sy),
  dragEnd: (sx: number, sy: number): void => ipcRenderer.send('tabdrag:end', sx, sy),
  back: (): void => ipcRenderer.send('nav:back'),
  forward: (): void => ipcRenderer.send('nav:forward'),
  reload: (): void => ipcRenderer.send('nav:reload'),
  goto: (kind: 'url' | 'route', value: string): void =>
    ipcRenderer.send('nav:goto', kind, value),
  home: (): void => ipcRenderer.send('nav:home'),
  /** The wheel's Search tool asks the strip to focus the address bar. */
  onFocusOmnibox: (cb: () => void): (() => void) => {
    const listener = () => cb()
    ipcRenderer.on('omnibox:focus', listener)
    return () => ipcRenderer.removeListener('omnibox:focus', listener)
  },
  /** The apps launcher (the Google-style grid): rendered in the overlay
   *  view so it can drop over ANY tab, browser tabs included. */
  launcherToggle: (): void => ipcRenderer.send('launcher:toggle'),
  minimize: (): void => ipcRenderer.send('win:minimize'),
  maximizeToggle: (): void => ipcRenderer.send('win:maximize'),
  closeWindow: (): void => ipcRenderer.send('win:close'),
  /** Right-clicking a TAB opens the native tab menu (split etc.), never the
   *  gesture wheel. Coordinates are window-relative for menu placement. */
  tabMenu: (tabId: number, x: number, y: number): void =>
    ipcRenderer.send('tabmenu:open', tabId, x, y),
  /** Chrome-style split view (also the e2e's way to drive it directly). */
  splitWith: (tabId: number, withTabId: number): void =>
    ipcRenderer.send('tabs:split', tabId, withTabId),
  closeSplit: (): void => ipcRenderer.send('tabs:unsplit'),
}

/** The split divider's own surface (mode=divider): live drag reports. */
const grindstoneSplit = {
  drag: (screenX: number): void => ipcRenderer.send('split:drag', screenX),
}

contextBridge.exposeInMainWorld('grindstone', grindstone)
contextBridge.exposeInMainWorld('grindstoneTabs', grindstoneTabs)
contextBridge.exposeInMainWorld('grindstoneWheel', grindstoneWheel)
contextBridge.exposeInMainWorld('grindstoneSplit', grindstoneSplit)

export type GrindstoneBridge = typeof grindstone
export type GrindstoneTabsBridge = typeof grindstoneTabs
export type GrindstoneWheelBridge = typeof grindstoneWheel
export type GrindstoneSplitBridge = typeof grindstoneSplit
