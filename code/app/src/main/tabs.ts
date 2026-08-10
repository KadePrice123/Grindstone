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
import { BaseWindow, Menu, WebContentsView, ipcMain, session, shell } from 'electron'
import { existsSync } from 'node:fs'
import path from 'node:path'
import { backtestReportUrl } from './api'
import { log } from './log'
import { asGs, gsRoute, isKnownPage } from '../renderer/src/urls'

/** Untrusted third-party pages live in their own session: no preload, no
 *  node, denied permissions, downloads blocked. Article reading must never
 *  become a foothold into the app that holds broker credentials. */
const BROWSE_PARTITION = 'persist:browsing'

/**
 * Sign-in origins, which never load in a tab.
 *
 * Google blocks account sign-in in ANY embedded view as policy, not as a bug,
 * and its published criteria are explicit that a browser "must not use another
 * browser's User-Agent string, such as Chrome" when connecting to
 * accounts.google.com — which is exactly what browsingUserAgent() does. So the
 * compatibility UA below is actively counterproductive on these hosts, and no
 * amount of it would help: RFC 8252 (OAuth for Native Apps) says native apps
 * MUST NOT use embedded user-agents for authorization, and the sanctioned
 * answer is to hand the flow to the real browser. That is also the better one
 * for the user, whose password manager and passkeys live there.
 *
 * DELIBERATELY MINIMAL. Only providers that genuinely refuse to render in an
 * embedded view belong here. An earlier draft also listed GitHub, Okta, Auth0,
 * LinkedIn, Facebook and Yahoo — none of which block embedded views at all, so
 * all that achieved was evicting sign-ins that worked perfectly well in-app and
 * stranding the user in a browser without any of this app's tools. Every host
 * added here costs the user their tooling; add one only after seeing it fail.
 *
 * Suffix-matched against the host, so accounts.google.co.uk matches too.
 */
const IDENTITY_HOSTS = [
  'accounts.google.com',
  'accounts.youtube.com',
]

/** Parse without throwing. A page-supplied string reaches these handlers, and
 *  an unguarded `new URL()` throws inside a main-process event handler. */
function parseUrl(u: string): URL | null {
  try {
    return new URL(u)
  } catch {
    return null
  }
}

function isWebUrl(u: string): boolean {
  const p = parseUrl(u)
  return !!p && (p.protocol === 'https:' || p.protocol === 'http:')
}

function isIdentityUrl(u: string): boolean {
  const p = parseUrl(u)
  if (!p) return false
  const hostPath = p.host + p.pathname
  return IDENTITY_HOSTS.some(
    (h) => p.host === h || p.host.endsWith('.' + h) || hostPath.startsWith(h)
  )
}

/** Hand a sign-in URL to the real browser and say so in the log. */
function handOffToBrowser(target: string, why: string): void {
  log('identity flow ->', why, new URL(target).host)
  shell.openExternal(target)
}

// --------------------------------------------------------------- identity
// One place decides who this browser says it is. The UA string, the client
// hint headers and navigator.userAgentData must all tell the SAME story: a
// view that claims Chrome in its User-Agent while its client hints say
// Chromium (or say nothing) is a contradiction every device-detection
// library on the web reads as "not a real browser".

function chromeMajor(): string {
  return process.versions.chrome.split('.')[0]
}

/** The platform token Chrome itself puts in the UA string on each OS. */
function uaPlatformToken(): string {
  switch (process.platform) {
    case 'darwin': return 'Macintosh; Intel Mac OS X 10_15_7'
    case 'linux': return 'X11; Linux x86_64'
    default: return 'Windows NT 10.0; Win64; x64'
  }
}

/** The Sec-CH-UA-Platform value, which Chromium derives from the real OS —
 *  hardcoding Windows here while running on a Mac is a self-contradiction. */
function chPlatform(): string {
  switch (process.platform) {
    case 'darwin': return 'macOS'
    case 'linux': return 'Linux'
    default: return 'Windows'
  }
}

/**
 * A clean Chrome user agent. Electron's default advertises "Electron/43" and
 * our app name, which makes sites serve degraded or "unsupported browser"
 * experiences — the thing that makes an embedded view feel like an iframe
 * rather than a browser.
 */
function browsingUserAgent(): string {
  const chrome = process.versions.chrome
  return (
    `Mozilla/5.0 (${uaPlatformToken()}) AppleWebKit/537.36 ` +
    `(KHTML, like Gecko) Chrome/${chrome} Safari/537.36`
  )
}

/** The low-entropy client-hint triple, matching browsingUserAgent(). */
function clientHintHeaders(): Record<string, string> {
  const major = chromeMajor()
  return {
    'Sec-CH-UA': `"Not;A=Brand";v="8", "Chromium";v="${major}", "Google Chrome";v="${major}"`,
    'Sec-CH-UA-Mobile': '?0',
    'Sec-CH-UA-Platform': `"${chPlatform()}"`,
  }
}

/**
 * The permission policy for untrusted pages.
 *
 * Previously both handlers were a blanket `false`. That is stricter than any
 * real browser and it silently breaks ordinary sites: the worst offender is
 * storage-access, whose denial puts any site using an embedded SSO iframe into
 * an endless login loop. Denying what Chrome never even prompts for does not
 * buy security, it just looks like the site is broken.
 *
 * Tier A below is exactly the set Chrome grants without ever asking, and every
 * one of them is confined to this isolated partition — none of them can reach
 * session.defaultSession, where the broker credentials live. Everything else,
 * including camera, microphone, geolocation and notifications, stays denied.
 */
const AUTO_GRANT = new Set([
  // Chromium sanitizes the payload; that is what the permission name means.
  'clipboard-sanitized-write',
  // Chrome gates these on a user gesture, not a permission prompt.
  'fullscreen', 'pointerLock', 'keyboardLock',
  // The login-loop fix. Grants an embedded frame access to its OWN cookies
  // inside a partition that is already walled off from the app.
  'storage-access', 'top-level-storage-access',
  // Chrome does not prompt for EME on desktop. (Playback can still fail:
  // Electron ships no Widevine CDM, which is a separate, unfixable matter.)
  'mediaKeySystem',
])

function permissionAllowed(permission: string): boolean {
  return AUTO_GRANT.has(permission)
}

const hardened = new WeakSet<Electron.Session>()

function browsingSession(): Electron.Session {
  const s = session.fromPartition(BROWSE_PARTITION)
  // Keyed on the session, not a module flag: a second partition would
  // otherwise silently inherit "already hardened" and get no policy at all.
  if (!hardened.has(s)) {
    hardened.add(s)

    s.setPermissionRequestHandler((_wc, permission, cb, details) => {
      // mailto:/tel:/sms: arrive here as 'openExternal' rather than as a
      // navigation. Handing those three to the OS is what a browser does;
      // anything else external stays blocked.
      if (permission === 'openExternal') {
        const ext = (details as { externalURL?: string }).externalURL ?? ''
        const proto = parseUrl(ext)?.protocol ?? ''
        const ok = /^(mailto|tel|sms):$/.test(proto)
        if (!ok) log('blocked external scheme', ext.slice(0, 80))
        return cb(ok)
      }
      const ok = permissionAllowed(permission)
      if (!ok) log('permission denied', permission)
      return cb(ok)
    })
    // Must agree with the request handler: this one is what
    // navigator.permissions.query() and feature detection read, and it is
    // synchronous, so it can never prompt.
    s.setPermissionCheckHandler((_wc, permission) => permissionAllowed(permission))

    // Downloads used to be cancelled with no error, no UI and no navigation —
    // indistinguishable from a dead link, and it also killed the CSV export in
    // our OWN backtest reports, which load in this partition. Still no file
    // lands in the app's process; the real browser takes it, where the
    // download manager and AV scanning already work.
    s.on('will-download', (e, item) => {
      const url = item.getURL()
      e.preventDefault()
      log('download handed to the browser:', item.getFilename())
      if (isWebUrl(url)) shell.openExternal(url)
    })

    // Electron has no ClientHintsControllerDelegate, and that delegate is what
    // adds client hints to a TOP-LEVEL NAVIGATION on the browser side. So the
    // document request goes out with no Sec-CH-UA at all while its User-Agent
    // claims Chrome — measured against real Chrome 150, which sends the full
    // triple to the identical URL. Blink still adds the hints to subresource
    // requests (fetch/XHR carry them either way), so this handler exists
    // specifically to fix the navigation, which is the request a site's
    // server-side browser detection actually sees first.
    s.webRequest.onBeforeSendHeaders((d, cb) => {
      cb({ requestHeaders: { ...d.requestHeaders, ...clientHintHeaders() } })
    })

    s.setUserAgent(browsingUserAgent())
  }
  return s
}

// NOT DONE, deliberately: aligning navigator.userAgentData too.
//
// setUserAgent() leaves the client-hint brand list alone, so
// navigator.userAgentData.brands stays [Not;A=Brand, Chromium] with no
// "Google Chrome" entry, and the only way to change it is
// webContents.debugger.attach() + Network.setUserAgentOverride. That was
// implemented and then removed on 2026-08-04, because:
//
//  - It makes DevTools unusable on every browser tab: only one debugger may
//    attach to a WebContents, and ours would already be there. Certain cost.
//  - It bought nothing measurable. A/B against Cloudflare's Turnstile over
//    four runs each: baseline 0/4 verified, header-injection-only 2/4, CDP
//    attached 0/4. It never once helped.
//
// So the wire headers are fixed (browsingSession above, which is a real gap —
// Electron ships no ClientHintsControllerDelegate and therefore sends no
// Sec-CH-UA at all) and the JS-visible brand list is left honest. A site that
// branches on navigator.userAgentData will read Chromium, which is what this
// is. Note also that going further — shimming window.chrome, patching
// navigator internals — is not a compatibility fix but bot-detection evasion,
// and is out of scope on purpose.

export const TABBAR_H = 40
/** Extra chrome height when the active tab is a web page: its address bar. */
export const NAVBAR_H = 34
/** Split-view divider width. Also baked into splitOffsetX — keep them one. */
const DIVIDER_W = 8
const MIN_W = 900
const MIN_H = 600
const DARK_BG = '#101214'
const NEW_WINDOW_SIZE = { width: 1100, height: 720 }

export interface TabInfo {
  id: number
  title: string
  icon: string // page-type key the strip maps to an icon
  kind: 'app' | 'browser'
  url?: string
}

/** Platform page names that are NOT tickers — a data.gs tab must not offer
 *  "add DATA to chart". Backed by urls.ts rather than copied out of it: this
 *  used to be a hand-kept mirror, and the copy of gsRoute that sat beside it
 *  silently lost the `opt` case, so every `opt.gs?s=SPY` main opened landed
 *  on Home. Two decision procedures for one question is the bug. */
const PAGE_NAMES = { has: (n: string): boolean => isKnownPage(n) }

/**
 * The window / taskbar icon, generated from logo.svg by
 * tools/icons/make-icons.ps1 and committed under assets/branding.
 *
 * macOS is deliberately absent: a .app bundle takes its icon from its own
 * Info.plist, and passing one here does nothing there. A missing file must
 * never be fatal — a source tree that has not run the generator still boots,
 * just with Electron's default icon.
 */
function appIcon(): string | undefined {
  // __dirname is <repo>/code/app/out/main in both dev and preview builds.
  const name =
    process.platform === 'win32' ? 'app.ico'
    : process.platform === 'linux' ? 'icon-256.png'
    : ''
  if (!name) return undefined
  const p = path.join(__dirname, '../../../assets/branding', name)
  return existsSync(p) ? p : undefined
}

/**
 * F12 (Ctrl+Shift+I, Cmd+Opt+I) opens DevTools for the focused view.
 *
 * There was previously no way to open DevTools anywhere in this app. The
 * windows are frameless BaseWindows, which get no application menu, so
 * Electron's built-in accelerator never applied either — a page's console and
 * network tab were simply unreachable, which makes every "the site is blank"
 * report unfalsifiable.
 *
 * Detached on purpose: this app positions every WebContentsView by hand, and
 * docked DevTools resizes the page out from under that layout.
 */
function enableDevToolsShortcut(wc: Electron.WebContents): void {
  wc.on('before-input-event', (event, input) => {
    if (input.type !== 'keyDown') return
    const key = input.key.toLowerCase()
    const combo =
      input.key === 'F12' ||
      (input.control && input.shift && key === 'i') ||
      (input.meta && input.alt && key === 'i') // macOS
    if (!combo) return
    event.preventDefault()
    if (wc.isDevToolsOpened()) wc.closeDevTools()
    else wc.openDevTools({ mode: 'detach' })
  })
}

/** What the gesture wheel sees of a tab. */
export interface WheelTabInfo {
  id: number
  winId: number
  title: string
  icon: string
  kind: 'app' | 'browser'
  address?: string
}

interface Tab {
  id: number
  view: WebContentsView
  title: string
  icon: string
  kind: 'app' | 'browser'
  url?: string
  /** For app tabs: the .gs address of the page currently shown. */
  address?: string
  /** For browser tabs: the page's favicon URL. Main never fetches its bytes —
   *  the backend downloads it at star time (POST /api/favorites icon_url). */
  favicon?: string
}

interface Win {
  id: number
  win: BaseWindow
  chrome: WebContentsView
  tabs: Tab[]
  activeId: number | null
  /** The tab that was active before this one — the Previous-tab button's
   *  target (alt-tab for tabs: press it twice and you are back where you
   *  started). Cleared when that tab closes or leaves the window. */
  prevActiveId: number | null
  /** Chrome-style split view: aId LEFT, bId RIGHT. Persists while a tab
   *  outside the pair is active (the pair hides); dies with either member. */
  split: { aId: number; bId: number; ratio: number } | null
  /** The one divider view per window, created lazily on first split and
   *  attached only while the split is visible. Closed in onWindowClosed —
   *  Electron does not destroy a closed window's views. */
  divider: WebContentsView | null
}

type StripState = {
  tabs: TabInfo[]
  activeId: number | null
  maximized: boolean
  bounds: { x: number; y: number; width: number; height: number }
  canGoBack: boolean
  canGoForward: boolean
  activeKind: 'app' | 'browser' | null
  activeUrl: string
  /** The active BROWSER tab's favicon URL ('' otherwise) — what the star
   *  submits as icon_url when favoriting a web page. */
  activeFavicon: string
  loading: boolean
  draggingId: number | null
  split: { aId: number; bId: number; ratio: number } | null
  /** The Previous-tab button's target; null disables it. */
  prevId: number | null
}

export class TabManager {
  private wins: Win[] = []
  private nextTabId = 1
  private nextWinId = 1
  private preload: string
  private browserPreload: string
  private locked = true
  private drag: { tabId: number; overWinId: number | null } | null = null
  private quitting = false
  /** Content tabs navigate internally; they report their depth so the strip
   *  can enable Back for app pages the same way it does for web pages. */
  private appHistoryDepth = new Map<number, number>()
  onAllClosed: (() => void) | null = null
  /** The gesture wheel despawns when its host window disappears. */
  onWindowGone: ((winId: number) => void) | null = null

  constructor(preload: string, browserPreload?: string) {
    this.preload = preload
    this.browserPreload = browserPreload ?? ''
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
    enableDevToolsShortcut(view.webContents)
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
    const icon = appIcon()
    const win = new BaseWindow({
      ...NEW_WINDOW_SIZE,
      ...(at ? { x: at.x, y: at.y } : {}),
      ...(icon ? { icon } : {}),
      minWidth: MIN_W,
      minHeight: MIN_H,
      frame: false,
      backgroundColor: DARK_BG,
      title: 'Grindstone',
      show: false,
    })
    const chrome = this.makeView({ mode: this.locked ? 'auth' : 'chrome' })
    win.contentView.addChildView(chrome)

    const w: Win = {
      id: this.nextWinId++, win, chrome, tabs: [], activeId: null,
      prevActiveId: null, split: null, divider: null,
    }
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

  private forgetTab(id: number) {
    this.appHistoryDepth.delete(id)
  }

  private onWindowClosed(w: Win) {
    for (const t of w.tabs) this.forgetTab(t.id)
    // Electron does NOT destroy a window's WebContentsViews when the window
    // closes — they leak as orphaned renderers (observed: a closed window's
    // chrome view stayed alive as a live CDP target and answered IPC with a
    // window that no longer existed). Close every view we own, chrome
    // included. A user-closed window closes its tabs (Chrome parity).
    for (const t of w.tabs) t.view.webContents.close()
    w.tabs = []
    w.chrome.webContents.close()
    if (w.divider) w.divider.webContents.close()
    this.wins = this.wins.filter((x) => x !== w)
    log('window closed', w.id, 'remaining', this.wins.length)
    this.onWindowGone?.(w.id)
    if (this.wins.length === 0 && !this.quitting) this.onAllClosed?.()
  }

  /** The split pair resolved to live tabs, IF it is currently shown — i.e.
   *  the active tab is a member. A split whose pair is hidden behind another
   *  active tab still exists but does not lay out. */
  private visibleSplit(w: Win): { a: Tab; b: Tab; ratio: number } | null {
    if (!w.split) return null
    const a = w.tabs.find((t) => t.id === w.split!.aId)
    const b = w.tabs.find((t) => t.id === w.split!.bId)
    if (!a || !b) return null
    if (w.activeId !== a.id && w.activeId !== b.id) return null
    return { a, b, ratio: w.split.ratio }
  }

  private layout(w: Win) {
    const { width, height } = w.win.getContentBounds()
    if (this.locked) {
      w.chrome.setBounds({ x: 0, y: 0, width, height })
      return
    }
    // The address/search bar is ALWAYS present, like a browser's — it is how
    // you reach both the web and platform pages from anywhere.
    const chromeH = TABBAR_H + NAVBAR_H
    w.chrome.setBounds({ x: 0, y: 0, width, height: chromeH })
    const contentH = height - chromeH
    const split = this.visibleSplit(w)
    if (split) {
      const leftW = Math.round(split.ratio * width - DIVIDER_W / 2)
      split.a.view.setBounds({ x: 0, y: chromeH, width: leftW, height: contentH })
      w.divider?.setBounds({ x: leftW, y: chromeH, width: DIVIDER_W, height: contentH })
      split.b.view.setBounds({
        x: leftW + DIVIDER_W, y: chromeH,
        width: width - leftW - DIVIDER_W, height: contentH,
      })
      return
    }
    const active = w.tabs.find((t) => t.id === w.activeId)
    if (active) active.view.setBounds({ x: 0, y: chromeH, width, height: contentH })
  }

  /**
   * Make EXACTLY the views this window should show its children: the active
   * tab alone, or the split pair + divider when the split is visible. Only
   * tab views and the divider are touched — never the chrome, never the
   * wheel overlay that may sit attached above everything. addChildView on an
   * existing child RE-RAISES it (it would cover that overlay), so views
   * already in place are left alone.
   */
  private reconcile(w: Win) {
    if (w.win.isDestroyed()) return
    const wanted = new Set<WebContentsView>()
    const split = this.visibleSplit(w)
    if (split) {
      wanted.add(split.a.view)
      wanted.add(split.b.view)
      wanted.add(this.ensureDivider(w))
    } else {
      const active = w.tabs.find((t) => t.id === w.activeId)
      if (active) wanted.add(active.view)
    }
    const owned: WebContentsView[] = w.tabs.map((t) => t.view)
    if (w.divider) owned.push(w.divider)
    for (const v of owned) {
      if (!wanted.has(v) && w.win.contentView.children.includes(v)) {
        w.win.contentView.removeChildView(v)
      }
    }
    for (const v of wanted) {
      if (!w.win.contentView.children.includes(v)) w.win.contentView.addChildView(v)
    }
    // REVIEW 2026-08-02: a NEWLY attached view lands after the wheel overlay
    // and occludes it — the not-already-a-child guard above only protects
    // existing children. A popunder while a wheel was up left an invisible
    // live wheel eating right-clicks. If the overlay is attached here, it
    // must end up last again (re-adding an existing child re-raises it).
    if (this.attachedOverlay?.winId === w.id
        && w.win.contentView.children.includes(this.attachedOverlay.view)) {
      w.win.contentView.addChildView(this.attachedOverlay.view)
    }
    this.layout(w)
  }

  /** One divider view per window: same bundle, ?mode=divider, same preload
   *  and navigation hardening as every view we own (makeView). */
  private ensureDivider(w: Win): WebContentsView {
    if (w.divider && !w.divider.webContents.isDestroyed()) return w.divider
    w.divider = this.makeView({ mode: 'divider' })
    return w.divider
  }

  // ----------------------------------------------------------------- tabs
  newTab(w: Win, route = 'idle', activate = true): Tab {
    const tab: Tab = {
      id: this.nextTabId++,
      view: this.makeView({ mode: 'content', route }),
      title: 'New tab',
      icon: route === 'idle' ? 'home' : route,
      kind: 'app',
    }
    w.tabs.push(tab)
    if (activate) this.activate(w, tab.id)
    this.pushStrip(w)
    return tab
  }

  /** A real web page, in-app (FR-SHELL-6). Hardened and preload-free: this
   *  view can never reach the bridge that talks to the vault. */
  newBrowserTab(w: Win, url: string, activate = true): Tab | null {
    let parsed: URL
    try {
      parsed = new URL(url)
    } catch {
      return null
    }
    if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') return null
    // The third and last door: typing a sign-in URL into the omnibox, or a
    // page handing us one. Returning null here means no tab is created at all,
    // which is the honest outcome — the flow continues in the real browser.
    if (isIdentityUrl(url)) {
      handOffToBrowser(url, 'opened directly')
      return null
    }

    const view = new WebContentsView({
      webPreferences: {
        session: browsingSession(),
        contextIsolation: true,
        sandbox: true,
        webSecurity: true,
        // Every node door, explicitly shut — a renderer-sandbox escape in a
        // news tab lands in the same process tree as the credential vault.
        nodeIntegration: false,
        nodeIntegrationInWorker: false,
        nodeIntegrationInSubFrames: false,
        webviewTag: false,
        experimentalFeatures: false,
        allowRunningInsecureContent: false,
        // safeDialogs alone, NOT disableDialogs: the latter overrides it and
        // makes window.confirm() return false unconditionally, so every
        // confirm-gated button on every site silently does nothing. safeDialogs
        // is the setting that actually addresses the threat — it adds
        // Chromium's "prevent this page from creating additional dialogs"
        // checkbox, which defeats dialog-spam loops without lying to the page.
        safeDialogs: true,
        navigateOnDragDrop: false,
        autoplayPolicy: 'document-user-activation-required',
        // NOT the app bridge — browser tabs never see grindstone/*. The only
        // preload here is the one-way right-click forwarder for the gesture
        // wheel (src/preload/browser.ts): no contextBridge, isTrusted-gated,
        // sends three primitive fields on one fixed channel and nothing else.
        ...(this.browserPreload ? { preload: this.browserPreload } : {}),
      },
    })
    // Dark background while loading, so opening a link is not a white flash
    // in a dark app. Pages that honour prefers-color-scheme then render dark
    // (nativeTheme is forced dark in index.ts).
    view.setBackgroundColor('#101214')
    view.webContents.setUserAgent(browsingUserAgent())
    const tab: Tab = {
      id: this.nextTabId++,
      view,
      title: parsed.hostname,
      icon: 'browser',
      kind: 'browser',
      url: parsed.toString(),
    }

    const wc = view.webContents
    enableDevToolsShortcut(wc)
    wc.setWindowOpenHandler(({ url: target, disposition }) => {
      // "Sign in with Google" and friends open a popup. Re-hosting that popup
      // as another embedded tab is precisely what Google refuses to serve, so
      // this is the choke point where every third-party sign-in button was
      // dying, not just a visit to accounts.google.com.
      if (isIdentityUrl(target)) {
        handOffToBrowser(target, 'popup from ' + (parseUrl(tab.url ?? '')?.host ?? '?'))
        return { action: 'deny' }
      }
      // Popups become tabs, never new OS windows we do not control.
      const home = this.wins.find((x) => x.tabs.some((t) => t.id === tab.id))
      if (home && isWebUrl(target)) {
        // ctrl/middle-click asks for a background tab; honour that rather than
        // stealing focus on every popup.
        this.newBrowserTab(home, target, disposition !== 'background-tab')
      }
      return { action: 'deny' }
    })
    // A web page may only ever navigate to another web page: no file://,
    // no custom app schemes, nothing that could reach local resources.
    const guardNavigation = (event: Electron.Event, target: string): void => {
      if (isIdentityUrl(target)) {
        event.preventDefault()
        handOffToBrowser(target, 'navigation')
        return
      }
      if (isWebUrl(target)) return
      event.preventDefault()
      const proto = parseUrl(target)?.protocol ?? ''
      // mailto:/tel:/sms: are user-facing links, not an attack surface — a
      // browser hands them to the OS rather than swallowing them silently.
      if (/^(mailto|tel|sms):$/.test(proto)) shell.openExternal(target)
      else log('blocked navigation to non-web scheme', target.slice(0, 80))
    }
    wc.on('will-navigate', guardNavigation)
    // will-navigate is main-frame-only and does NOT fire for server-side
    // redirects — and a real Google sign-in is reached by redirect far more
    // often than by a typed URL, so without this the handoff would miss the
    // common case and the invariant in the comment above would be untrue.
    wc.on('will-redirect', guardNavigation)
    // Without this every failed navigation is an unexplained blank dark
    // rectangle: DNS failure, TLS interception and a blocked response all look
    // identical to "the app is broken".
    wc.on('did-fail-load', (_e, code, desc, url, isMainFrame) => {
      if (!isMainFrame || code === -3) return // -3 is ERR_ABORTED: user action
      log('navigation failed', code, desc, String(url).slice(0, 80))
    })

    // A page's own errors, in OUR log, with no debugger attached.
    //
    // This exists because opening DevTools is not a neutral act: bot-detection
    // bundles ship `debugger` statements on a timer specifically to punish it,
    // and each one pauses the renderer and yanks DevTools to the Sources panel
    // (measured on Cloudflare's login, 2026-08-04). Reading the console from
    // the main process attaches no debugger, so the traps never fire and the
    // page behaves exactly as it would unobserved — which is also the only way
    // to see what a page does when a debugger WOULD change its behaviour.
    // Warnings and errors only; a chatty SPA would otherwise bury the log.
    wc.on('console-message', (_event, level, message) => {
      if (level < 2) return // 0 verbose, 1 info, 2 warning, 3 error
      log(level >= 3 ? 'page error' : 'page warn', parsed.hostname,
          String(message).slice(0, 300))
    })
    const sync = () => {
      tab.title = wc.getTitle() || parsed.hostname
      tab.url = wc.getURL()
      const home = this.wins.find((x) => x.tabs.some((t) => t.id === tab.id))
      if (home) this.pushStrip(home)
    }
    // A new document invalidates the old page's icon; the new page's own
    // arrives via page-favicon-updated — or never, and the tab honestly has
    // none. Registered before sync's did-navigate so the strip push that
    // follows never ships a stale icon. Over-long entries (data: URIs) are
    // dropped, not truncated — the backend fetches this URL at star time and
    // a cut URL fetches nothing.
    wc.on('did-navigate', () => {
      tab.favicon = undefined
    })
    wc.on('page-favicon-updated', (_e, favicons) => {
      const first = favicons[0]
      tab.favicon = first && first.length <= 500 ? first : undefined
      sync()
    })
    wc.on('page-title-updated', sync)
    wc.on('did-navigate', sync)
    wc.on('did-navigate-in-page', sync)
    wc.on('did-start-loading', sync)
    wc.on('did-stop-loading', sync)
    wc.loadURL(parsed.toString())

    w.tabs.push(tab)
    if (activate) this.activate(w, tab.id)
    this.pushStrip(w)
    return tab
  }

  activate(w: Win, tabId: number) {
    const tab = w.tabs.find((t) => t.id === tabId)
    if (!tab) return
    if (w.activeId !== null && w.activeId !== tab.id) w.prevActiveId = w.activeId
    w.activeId = tab.id
    this.reconcile(w)
    this.pushStrip(w)
  }

  closeTab(w: Win, tabId: number) {
    const idx = w.tabs.findIndex((t) => t.id === tabId)
    if (idx < 0) return
    const [tab] = w.tabs.splice(idx, 1)
    // A split dies with either member. The tab may be attached even when NOT
    // active (the hidden member of a visible split), so detach by membership,
    // not by activeId.
    if (w.split && (w.split.aId === tab.id || w.split.bId === tab.id)) w.split = null
    if (w.win.contentView.children.includes(tab.view)) {
      w.win.contentView.removeChildView(tab.view)
    }
    if (w.activeId === tab.id) {
      const next = w.tabs[Math.min(idx, w.tabs.length - 1)]
      w.activeId = null
      if (next) this.activate(w, next.id)
    } else {
      this.reconcile(w) // the split may have dissolved: survivor goes full-width
    }
    this.forgetTab(tab.id)
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
    // Adoption breaks a split the same way closing does; same membership rule.
    if (from.split && (from.split.aId === tab.id || from.split.bId === tab.id)) {
      from.split = null
    }
    if (from.win.contentView.children.includes(tab.view)) {
      from.win.contentView.removeChildView(tab.view)
    }
    if (from.activeId === tab.id) {
      from.activeId = null
      const next = from.tabs[Math.min(idx, from.tabs.length - 1)]
      if (next) this.activate(from, next.id)
    } else {
      this.reconcile(from)
    }
    to.tabs.splice(Math.max(0, Math.min(index, to.tabs.length)), 0, tab)
    this.activate(to, tab.id)
    if (from.tabs.length === 0) from.win.close()
    else this.pushStrip(from)
    this.pushStrip(to)
  }

  /** Chrome-style split: tabId goes LEFT, withTabId RIGHT, divider at 50%. */
  setSplit(w: Win, tabId: number, withTabId: number) {
    if (this.locked || tabId === withTabId) return
    const a = w.tabs.find((t) => t.id === tabId)
    const b = w.tabs.find((t) => t.id === withTabId)
    if (!a || !b) return
    w.split = { aId: a.id, bId: b.id, ratio: 0.5 }
    this.activate(w, a.id) // reconciles both panes in and pushes the strip
  }

  unsplit(w: Win) {
    if (!w.split) return
    w.split = null
    this.reconcile(w) // detaches the divider and the non-active pane
    this.pushStrip(w)
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

  /** Fired on every lock-state flip. REVIEW 2026-08-02: an auto-lock (401,
   *  sidecar crash) used to leave a live wheel overlay ON TOP of the sign-in
   *  form — in hold mode the form was unclickable until the user stumbled on
   *  click-then-Escape. The WheelManager subscribes and despawns. */
  onLockChanged: ((locked: boolean) => void) | null = null

  // ------------------------------------------------------------- auth mode
  setLocked(locked: boolean) {
    if (this.locked === locked) return
    this.locked = locked
    this.onLockChanged?.(locked)
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
      w.split = null
      // The divider dies with the tabs it sat between; recreated on demand.
      if (w.divider) {
        if (w.win.contentView.children.includes(w.divider)) {
          w.win.contentView.removeChildView(w.divider)
        }
        w.divider.webContents.close()
        w.divider = null
      }
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
  /** Backtest-report URLs carry a live single-use key and the sidecar port in
   *  their query string. Renderers must never see either (the whole point of
   *  minting in main), so the ONE choke point where tab URLs leave this
   *  process strips the query from report URLs before shipping. */
  private shippableUrl(url: string | undefined): string | undefined {
    if (url && /^http:\/\/127\.0\.0\.1:\d+\/api\/backtests\/report\//.test(url)) {
      return url.split('?')[0]
    }
    return url
  }

  private stripState(w: Win): StripState {
    const active = w.tabs.find((t) => t.id === w.activeId)
    return {
      tabs: w.tabs.map((t) => ({
        id: t.id, title: t.title, icon: t.icon, kind: t.kind,
        url: this.shippableUrl(t.url),
      })),
      activeId: w.activeId,
      maximized: w.win.isMaximized(),
      bounds: w.win.getContentBounds(),
      canGoBack: active
        ? active.kind === 'browser'
          ? active.view.webContents.navigationHistory.canGoBack()
          : (this.appHistoryDepth.get(active.id) ?? 0) > 0
        : false,
      canGoForward: active?.kind === 'browser'
        ? active.view.webContents.navigationHistory.canGoForward()
        : false,
      activeKind: active?.kind ?? null,
      // The bar always shows an address: a URL for web tabs, a .gs address
      // for platform pages.
      activeUrl: active
        ? active.kind === 'browser'
          ? (this.shippableUrl(active.url) ?? '')
          : (active.address ?? 'home.gs')
        : '',
      activeFavicon: active?.kind === 'browser' ? (active.favicon ?? '') : '',
      loading: active?.kind === 'browser' ? active.view.webContents.isLoading() : false,
      draggingId: this.drag?.tabId ?? null,
      // A split survives while hidden, but never report a pair with a dead
      // member (dissolve paths clear it; this is the belt to their braces).
      split:
        w.split &&
        w.tabs.some((t) => t.id === w.split!.aId) &&
        w.tabs.some((t) => t.id === w.split!.bId)
          ? { ...w.split }
          : null,
      prevId:
        w.prevActiveId !== null && w.tabs.some((t) => t.id === w.prevActiveId)
          ? w.prevActiveId
          : null,
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
    // Alt-tab for tabs: jump to the previously active tab. activate() sets
    // prevActiveId to the tab being LEFT, so pressing this twice bounces
    // between the same two tabs (the useful behavior, not a history walk).
    ipcMain.on('tabs:prev', (e) => {
      const w = this.winFromSender(e.sender)
      if (!w || w.prevActiveId === null) return
      if (w.tabs.some((t) => t.id === w.prevActiveId)) this.activate(w, w.prevActiveId)
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

    // content tabs report their identity (title/icon/history depth)
    ipcMain.on('tab:meta', (e, meta: {
      title?: string; icon?: string; depth?: number; address?: string
    }) => {
      const w = this.winFromContent(e.sender)
      const tab = w?.tabs.find((t) => t.view.webContents.id === e.sender.id)
      if (!w || !tab) return
      if (typeof meta.title === 'string' && meta.title !== tab.title) {
        log('tab:meta title', tab.id, JSON.stringify(tab.title), '->',
            JSON.stringify(meta.title), 'from wc', e.sender.id)
      }
      if (typeof meta.title === 'string') tab.title = meta.title.slice(0, 80)
      if (typeof meta.icon === 'string') tab.icon = meta.icon.slice(0, 24)
      if (typeof meta.depth === 'number') this.appHistoryDepth.set(tab.id, meta.depth)
      if (typeof meta.address === 'string') tab.address = meta.address.slice(0, 300)
      this.pushStrip(w)
    })
    // content asks to open a route in a NEW tab (ctrl+click behavior)
    ipcMain.on('tab:open', (e, route: string) => {
      const w = this.winFromContent(e.sender)
      if (w && !this.locked && typeof route === 'string') this.newTab(w, route.slice(0, 200))
    })
    // content asks to open a URL as an in-app browser tab (news articles)
    ipcMain.on('tab:openUrl', (e, url: string) => {
      const w = this.winFromContent(e.sender)
      if (w && !this.locked && typeof url === 'string') {
        this.newBrowserTab(w, url.slice(0, 2000))
      }
    })
    // the backtest page asks for a run's report: main mints the single-use
    // key and builds the URL itself — no port, no key in any renderer
    ipcMain.on('backtest:openReport', async (e, runId: unknown, file?: unknown) => {
      const w = this.winFromContent(e.sender)
      if (!w || this.locked || typeof runId !== 'number') return
      const url = await backtestReportUrl(
        runId, typeof file === 'string' ? file.slice(0, 200) : undefined)
      if (url && !w.win.isDestroyed()) this.newBrowserTab(w, url)
    })

    // navigation: Back works for both tab kinds; Home returns to the idle page
    ipcMain.on('nav:back', (e) => {
      const w = this.winFromSender(e.sender)
      const tab = w?.tabs.find((t) => t.id === w.activeId)
      if (!w || !tab) return
      if (tab.kind === 'browser') {
        if (tab.view.webContents.navigationHistory.canGoBack()) {
          tab.view.webContents.navigationHistory.goBack()
        }
      } else {
        tab.view.webContents.send('nav:back')
      }
    })
    ipcMain.on('nav:forward', (e) => {
      const w = this.winFromSender(e.sender)
      const tab = w?.tabs.find((t) => t.id === w.activeId)
      if (tab?.kind === 'browser' && tab.view.webContents.navigationHistory.canGoForward()) {
        tab.view.webContents.navigationHistory.goForward()
      }
    })
    ipcMain.on('nav:reload', (e) => {
      const w = this.winFromSender(e.sender)
      const tab = w?.tabs.find((t) => t.id === w.activeId)
      if (!tab) return
      if (tab.kind === 'browser') tab.view.webContents.reload()
      else tab.view.webContents.reload()
    })
    /**
     * The omnibox, for every tab kind. The renderer has already classified
     * the input (it owns the .gs/URL rules); main only routes it:
     *   url    -> navigate a web tab in place, or open one from an app tab
     *   route  -> navigate an app tab in place, or open one from a web tab
     */
    ipcMain.on('nav:goto', (e, kind: 'url' | 'route', value: string) => {
      const w = this.winFromSender(e.sender)
      if (!w || typeof value !== 'string' || !value) return
      const tab = w.tabs.find((t) => t.id === w.activeId)

      if (kind === 'url') {
        if (tab?.kind === 'browser') tab.view.webContents.loadURL(value)
        else this.newBrowserTab(w, value)
        return
      }
      if (tab?.kind === 'app') tab.view.webContents.send('nav:route', value)
      else this.newTab(w, value)
    })

    ipcMain.on('nav:home', (e) => {
      const w = this.winFromSender(e.sender)
      if (!w) return
      const tab = w.tabs.find((t) => t.id === w.activeId)
      if (tab && tab.kind === 'app') tab.view.webContents.send('nav:home')
      else this.newTab(w, 'idle')
    })

    // ------------------------------------------------------------- dragging
    ipcMain.on('tabdrag:start', (e, tabId: number) => {
      const w = this.winFromSender(e.sender)
      if (w && w.tabs.some((t) => t.id === tabId)) {
        this.drag = { tabId, overWinId: null }
        this.pushStrip(w) // strip shows the tab as lifted the moment it moves
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
      for (const w of this.wins) this.pushStrip(w) // clear the lifted state
    })

    // ------------------------------------------------- split view + tab menu
    ipcMain.on('tabs:split', (e, tabId: number, withTabId: number) => {
      const w = this.winFromSender(e.sender)
      if (w && typeof tabId === 'number' && typeof withTabId === 'number') {
        this.setSplit(w, tabId, withTabId)
      }
    })
    ipcMain.on('tabs:unsplit', (e) => {
      const w = this.winFromSender(e.sender)
      if (w) this.unsplit(w)
    })
    // The divider reports raw screenX — its own client space is DIVIDER_W px
    // wide, useless for a ratio — and main owns the conversion against the
    // window's content bounds. Event-driven only: one message per real
    // pointermove, nothing polls.
    ipcMain.on('split:drag', (e, screenX: number) => {
      const w = this.wins.find((x) => x.divider?.webContents.id === e.sender.id)
      if (!w || !w.split || typeof screenX !== 'number') return
      const b = w.win.getContentBounds()
      if (b.width <= 0) return
      const ratio = Math.max(0.2, Math.min(0.8, (screenX - b.x) / b.width))
      if (ratio === w.split.ratio) return
      w.split.ratio = ratio
      this.layout(w) // bounds only — the children are already correct
      this.pushStrip(w)
    })
    // Right-clicking a TAB gets the native menu, never the gesture wheel
    // (wheelEvents.ts skips .strip-tab targets). x/y arrive as chrome-view
    // client coords, which ARE window coords — the chrome sits at (0,0).
    ipcMain.on('tabmenu:open', (e, tabId: number, x: number, y: number) => {
      const w = this.winFromSender(e.sender)
      if (!w || this.locked) return
      const tab = w.tabs.find((t) => t.id === tabId)
      if (!tab) return
      // Menu clicks fire after popup returns; the window can die in between.
      const alive = () => this.wins.includes(w) && !w.win.isDestroyed()
      const others = w.tabs.filter((t) => t.id !== tabId)
      const template: Electron.MenuItemConstructorOptions[] = []
      if (others.length === 0) {
        template.push({ label: 'Split with…', enabled: false })
      } else {
        for (const o of others) {
          template.push({
            label: `Split with ${o.title.slice(0, 40)}`,
            click: () => { if (alive()) this.setSplit(w, tabId, o.id) },
          })
        }
      }
      if (w.split) {
        template.push({ label: 'Close split', click: () => { if (alive()) this.unsplit(w) } })
      }
      template.push({ type: 'separator' })
      template.push({ label: 'New tab', click: () => { if (alive()) this.newTab(w, 'idle') } })
      template.push({ type: 'separator' })
      template.push({ label: 'Close tab', click: () => { if (alive()) this.closeTab(w, tabId) } })
      template.push({
        label: 'Close other tabs',
        enabled: others.length > 0,
        click: () => {
          if (!alive()) return
          for (const o of others) this.closeTab(w, o.id)
        },
      })
      template.push({
        label: 'Move to new window',
        // REVIEW 2026-08-02: on a single-tab window, detachToNewWindow's
        // one-tab branch (drag-parity) just MOVES the window — from a menu
        // that reads as a teleport bug. Chrome disables the item; so do we.
        enabled: w.tabs.length > 1,
        click: () => {
          if (!alive() || w.tabs.length <= 1) return
          const [px, py] = w.win.getPosition()
          this.detachToNewWindow(w, tabId, px + 80, py + 80)
        },
      })
      // BaseWindow is accepted by popup in Electron 43.
      Menu.buildFromTemplate(template).popup({
        window: w.win,
        x: Math.round(x),
        y: Math.round(y),
      })
    })
  }

  // ------------------------------------------------- gesture-wheel surface
  /** Everything the wheel needs from the tab system, as one narrow seam. */
  get isLocked(): boolean {
    return this.locked
  }

  /** Resolve any view's webContents to its window and view offset, so client
   *  coordinates can become window coordinates. Null for unknown senders.
   *  For tab views, also WHICH tab — the wheel targets chart actions at the
   *  view it was spawned over, not whatever is active by the time they fire.
   *  NOTE: offsets assume the sender spans the window's content area; in a
   *  split, the RIGHT pane's x offset is added by the caller from split
   *  state (splitOffsetX). */
  resolveSender(wc: Electron.WebContents):
    | { winId: number; offsetX: number; offsetY: number; tabId: number | null }
    | null {
    const asChrome = this.wins.find((w) => w.chrome.webContents.id === wc.id)
    if (asChrome) return { winId: asChrome.id, offsetX: 0, offsetY: 0, tabId: null }
    const asTab = this.winFromContent(wc)
    if (asTab) {
      const tab = asTab.tabs.find((t) => t.view.webContents.id === wc.id)
      return {
        winId: asTab.id,
        offsetX: this.splitOffsetX(asTab.id, tab?.id ?? null),
        offsetY: this.locked ? 0 : TABBAR_H + NAVBAR_H,
        tabId: tab?.id ?? null,
      }
    }
    return null
  }

  /** The x offset of this tab's view within its window: 0 unless the tab is
   *  the RIGHT pane of a visible split. Mirrors layout()'s math exactly —
   *  right pane x = round(ratio*W - DIV/2) + DIV — so the wheel's converted
   *  coordinates land on the same pixel the pane actually starts at. */
  splitOffsetX(winId: number, tabId: number | null): number {
    if (tabId === null) return 0
    const w = this.wins.find((x) => x.id === winId)
    if (!w || w.win.isDestroyed()) return 0
    const split = this.visibleSplit(w)
    if (!split || split.b.id !== tabId) return 0
    const b = w.win.getContentBounds()
    return Math.round(split.ratio * b.width - DIVIDER_W / 2) + DIVIDER_W
  }

  /** Open app tabs showing a plain ticker page, as {symbol, tabId} — the
   *  chart wheel's "add an open symbol to this chart" raw material. */
  symbolTabs(): { symbol: string; tabId: number }[] {
    const out: { symbol: string; tabId: number }[] = []
    for (const t of this.allTabs()) {
      const m = t.kind === 'app' && t.address ? /^([a-z0-9.]{1,8})\.gs$/.exec(t.address) : null
      if (m && !PAGE_NAMES.has(m[1])) {
        out.push({ symbol: m[1].toUpperCase(), tabId: t.id })
      }
    }
    return out
  }

  windowContentSize(winId: number): { width: number; height: number } | null {
    const w = this.wins.find((x) => x.id === winId)
    if (!w || w.win.isDestroyed()) return null
    const b = w.win.getContentBounds()
    return { width: b.width, height: b.height }
  }

  baseWindow(winId: number): BaseWindow | null {
    const w = this.wins.find((x) => x.id === winId)
    return w && !w.win.isDestroyed() ? w.win : null
  }

  /** The wheel overlay currently attached, if any — reconcile() re-raises it
   *  above newly attached views (a new child otherwise lands on top of it). */
  private attachedOverlay: { winId: number; view: WebContentsView } | null = null

  /** Attach a view above everything in this window (the wheel overlay). */
  attachOverlay(winId: number, view: WebContentsView): boolean {
    const w = this.wins.find((x) => x.id === winId)
    if (!w || w.win.isDestroyed()) return false
    const b = w.win.getContentBounds()
    view.setBounds({ x: 0, y: 0, width: b.width, height: b.height })
    w.win.contentView.addChildView(view) // last child renders topmost
    this.attachedOverlay = { winId, view }
    return true
  }

  detachOverlay(winId: number, view: WebContentsView): void {
    const w = this.wins.find((x) => x.id === winId)
    if (w && !w.win.isDestroyed()) w.win.contentView.removeChildView(view)
    if (this.attachedOverlay?.view === view) this.attachedOverlay = null
  }

  /** Every open tab across every window — the Tabs wheel's raw material. */
  allTabs(): WheelTabInfo[] {
    const out: WheelTabInfo[] = []
    for (const w of this.wins) {
      for (const t of w.tabs) {
        out.push({ id: t.id, winId: w.id, title: t.title, icon: t.icon,
                   kind: t.kind, address: t.address })
      }
    }
    return out
  }

  /** Activate a tab wherever it lives; focuses its window if it is another's. */
  activateTabGlobal(tabId: number): boolean {
    const w = this.wins.find((x) => x.tabs.some((t) => t.id === tabId))
    if (!w) return false
    this.activate(w, tabId)
    if (!w.win.isFocused()) w.win.focus()
    return true
  }

  /** Open a platform route in this window: the active app tab navigates in
   *  place; a browser tab (or nothing active) gets a fresh app tab. */
  gotoRoute(winId: number, route: string): void {
    const w = this.wins.find((x) => x.id === winId)
    if (!w || this.locked) return
    const tab = w.tabs.find((t) => t.id === w.activeId)
    if (tab?.kind === 'app') tab.view.webContents.send('nav:route', route)
    else this.newTab(w, route)
  }

  /** The ticker segments: focus the existing tab for this symbol anywhere,
   *  or open one here. */
  openTicker(winId: number, symbol: string): void {
    const addr = `${symbol.toLowerCase()}.gs`
    const existing = this.allTabs().find((t) => t.kind === 'app' && t.address === addr)
    if (existing) {
      this.activateTabGlobal(existing.id)
      return
    }
    const w = this.wins.find((x) => x.id === winId)
    if (w && !this.locked) this.newTab(w, `symbol:${symbol.toUpperCase()}`)
  }

  /** A favorited destination, main-side (the wheel's link segments): a .gs
   *  address routes in-app, an http(s) URL opens a browser tab. An existing
   *  app tab with that exact address is focused instead, the same rule
   *  openTicker follows. */
  openAddress(winId: number, address: string): void {
    const w = this.wins.find((x) => x.id === winId)
    if (!w || this.locked || typeof address !== 'string') return
    const text = address.trim()
    if (!text) return
    if (/^https?:\/\//i.test(text)) {
      this.newBrowserTab(w, text)
      return
    }
    const [head] = text.split('?')
    if (!/\.gs$/i.test(head)) return
    const existing = this.allTabs().find(
      (t) => t.kind === 'app' && t.address === text.toLowerCase()
    )
    if (existing) {
      this.activateTabGlobal(existing.id)
      return
    }
    // THE address→route translation, not a copy of it. urls.ts is
    // dependency-free string logic, so main imports the real thing; the
    // hand-rolled duplicate that used to live here is what dropped `opt`.
    const gs = asGs(text)
    if (!gs) return
    this.newTab(w, gsRoute(gs))
  }

  /** The window whose CHROME view this sender is — for chrome-originated
   *  ipc handled outside this class (the launcher toggle). */
  winIdFromSender(wc: Electron.WebContents): number | null {
    return this.winFromSender(wc)?.id ?? null
  }

  focusOmnibox(winId: number): void {
    const w = this.wins.find((x) => x.id === winId)
    if (w && !this.locked) w.chrome.webContents.send('omnibox:focus')
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
