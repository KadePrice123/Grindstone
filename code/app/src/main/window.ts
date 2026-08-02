/**
 * Window management. M1 is a single window, but built on BaseWindow +
 * WebContentsView from day one (REQUIREMENTS.md 6.1) — the M2 tab system
 * adds views to this exact structure instead of replacing it.
 */
import { BaseWindow, WebContentsView, shell } from 'electron'
import path from 'node:path'

const DARK_BG = '#101214'

export interface AppWindow {
  win: BaseWindow
  view: WebContentsView
}

export function createMainWindow(preloadPath: string): AppWindow {
  const win = new BaseWindow({
    width: 1280,
    height: 800,
    minWidth: 900,
    minHeight: 600,
    backgroundColor: DARK_BG,
    title: 'Grindstone',
    show: false,
  })

  const view = new WebContentsView({
    webPreferences: {
      preload: preloadPath,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  })
  view.setBackgroundColor(DARK_BG)
  win.contentView.addChildView(view)

  const fit = () => {
    const { width, height } = win.getContentBounds()
    view.setBounds({ x: 0, y: 0, width, height })
  }
  fit()
  win.on('resize', fit)

  // External links open in the OS browser, never inside the shell.
  view.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('https://')) shell.openExternal(url)
    return { action: 'deny' }
  })

  if (process.env['ELECTRON_RENDERER_URL']) {
    view.webContents.loadURL(process.env['ELECTRON_RENDERER_URL'])
  } else {
    view.webContents.loadFile(path.join(__dirname, '../renderer/index.html'))
  }

  view.webContents.once('did-finish-load', () => win.show())
  return { win, view }
}
