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

  onSidecarStatus: (cb: (s: { status: string; detail?: string }) => void): (() => void) => {
    const listener = (_e: unknown, payload: { status: string; detail?: string }) => cb(payload)
    ipcRenderer.on('sidecar:status', listener)
    return () => ipcRenderer.removeListener('sidecar:status', listener)
  },

  /** Content pages report tab identity + how deep their in-tab history is. */
  setTabMeta: (title: string, icon: string, depth: number): void => {
    ipcRenderer.send('tab:meta', { title, icon, depth })
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
  draggingId: number | null
}

const grindstoneTabs = {
  getState: (): Promise<StripState | null> => ipcRenderer.invoke('tabs:state'),
  onState: (cb: (s: StripState) => void): (() => void) => {
    const listener = (_e: unknown, s: StripState) => cb(s)
    ipcRenderer.on('tabs:state', listener)
    return () => ipcRenderer.removeListener('tabs:state', listener)
  },
  newTab: (): void => ipcRenderer.send('tabs:new'),
  activate: (id: number): void => ipcRenderer.send('tabs:activate', id),
  close: (id: number): void => ipcRenderer.send('tabs:close', id),
  reorder: (id: number, toIndex: number): void => ipcRenderer.send('tabs:reorder', id, toIndex),
  dragStart: (id: number): void => ipcRenderer.send('tabdrag:start', id),
  dragMove: (sx: number, sy: number): void => ipcRenderer.send('tabdrag:move', sx, sy),
  dragEnd: (sx: number, sy: number): void => ipcRenderer.send('tabdrag:end', sx, sy),
  back: (): void => ipcRenderer.send('nav:back'),
  home: (): void => ipcRenderer.send('nav:home'),
  minimize: (): void => ipcRenderer.send('win:minimize'),
  maximizeToggle: (): void => ipcRenderer.send('win:maximize'),
  closeWindow: (): void => ipcRenderer.send('win:close'),
}

contextBridge.exposeInMainWorld('grindstone', grindstone)
contextBridge.exposeInMainWorld('grindstoneTabs', grindstoneTabs)

export type GrindstoneBridge = typeof grindstone
export type GrindstoneTabsBridge = typeof grindstoneTabs
