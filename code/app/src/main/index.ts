import { app, ipcMain, nativeTheme } from 'electron'
import path from 'node:path'

// Websites opened in-app should arrive dark, not blind you. themeSource makes
// Chromium report prefers-color-scheme: dark to every page, and Chromium's
// auto-dark handles sites that have no dark theme of their own. The switch
// must be set before 'ready'.
app.commandLine.appendSwitch('enable-features', 'WebContentsForceDark')
import { announceUnlockedIfSignedIn, registerApiBridge, clearSession } from './api'
import { log } from './log'
import { Sidecar } from './sidecar'
import { TabManager } from './tabs'
import { WheelManager } from './wheel'

const sidecar = new Sidecar()
let tabs: TabManager | null = null

app.whenReady().then(async () => {
  nativeTheme.themeSource = 'dark'
  const preload = path.join(__dirname, '../preload/index.js')
  const browserPreload = path.join(__dirname, '../preload/browser.js')
  tabs = new TabManager(preload, browserPreload)
  tabs.onAllClosed = () => app.quit()
  // The gesture wheel: registers its IPC and lives for the app's lifetime.
  const wheel = new WheelManager(tabs)
  wheel.preloadPath = preload

  // Auth is WINDOW-level: main holds the session token (api.ts), so main is
  // the single source of truth for signed-in state. Sign-in unlocks the tab
  // UI; lock/logout/expiry collapses back to the lock screen.
  registerApiBridge(sidecar, (signedIn) => tabs?.setLocked(!signedIn))

  // The lock screen tells us when it has the sign-in answer in hand; only
  // then is it safe to reload that view into tab mode.
  ipcMain.on('auth:unlocked', () => {
    log('renderer signalled unlocked — swapping to tab mode')
    announceUnlockedIfSignedIn()
  })

  sidecar.onStatus((status, detail) => {
    if (status === 'crashed') {
      clearSession()
      tabs?.setLocked(true)
    }
    tabs?.broadcastToContent('sidecar:status', { status, detail })
  })

  tabs.bootstrap() // lock screen first; the auth view probes backend health
  try {
    const s = await sidecar.start()
    log('sidecar ready on port', s.port, 'version', s.version)
  } catch (e) {
    // Never swallow this: a silent start failure looks like "backend not
    // running" in the UI with no clue anywhere as to why.
    log('SIDECAR FAILED TO START:', String(e))
  }
})

app.on('window-all-closed', () => {
  app.quit()
})

app.on('before-quit', () => {
  tabs?.shutdown()
  sidecar.stop()
})
