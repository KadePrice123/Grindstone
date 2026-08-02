/**
 * The ONLY surface the renderer gets. No tokens, no Node, no direct network —
 * just a typed request bridge and a sidecar-status subscription.
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
}

contextBridge.exposeInMainWorld('grindstone', grindstone)

export type GrindstoneBridge = typeof grindstone
