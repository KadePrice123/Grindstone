import { app, BaseWindow } from 'electron'
import path from 'node:path'
import { registerApiBridge, clearSession } from './api'
import { Sidecar } from './sidecar'
import { createMainWindow, AppWindow } from './window'

const sidecar = new Sidecar()
let mainWindow: AppWindow | null = null

function broadcastSidecarStatus(status: string, detail?: string): void {
  mainWindow?.view.webContents.send('sidecar:status', { status, detail })
}

app.whenReady().then(async () => {
  registerApiBridge(sidecar)
  sidecar.onStatus((status, detail) => {
    if (status === 'crashed') clearSession() // sessions died with the process
    broadcastSidecarStatus(status, detail)
  })

  const preload = path.join(__dirname, '../preload/index.js')
  mainWindow = createMainWindow(preload)

  try {
    await sidecar.start()
    broadcastSidecarStatus('ready')
  } catch (e) {
    broadcastSidecarStatus('crashed', String(e))
  }
})

app.on('window-all-closed', () => {
  app.quit()
})

app.on('before-quit', () => {
  sidecar.stop()
})

app.on('activate', () => {
  if (BaseWindow.getAllWindows().length === 0 && app.isReady()) {
    mainWindow = createMainWindow(path.join(__dirname, '../preload/index.js'))
  }
})
