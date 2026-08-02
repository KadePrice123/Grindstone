/**
 * Main-process log -> data/logs/shell.log (rotating, tiny).
 *
 * The backend got logging after two incidents were diagnosed blind; the
 * shell needs the same. Tab/window/drag decisions are invisible otherwise —
 * they happen in main, where neither DevTools nor the backend log reaches.
 */
import { app } from 'electron'
import { appendFileSync, mkdirSync, renameSync, statSync } from 'node:fs'
import path from 'node:path'

const MAX_BYTES = 512 * 1024

function logDir(): string {
  // Mirror the backend's data dir so both logs sit together.
  const override = process.env['GRINDSTONE_DATA_DIR']
  const base = override ?? path.resolve(app.getAppPath(), '..', '..', 'data')
  const dir = path.join(base, 'logs')
  mkdirSync(dir, { recursive: true })
  return dir
}

let file: string | null = null

export function log(...parts: unknown[]): void {
  try {
    if (!file) file = path.join(logDir(), 'shell.log')
    try {
      if (statSync(file).size > MAX_BYTES) renameSync(file, file + '.1')
    } catch {
      /* first write */
    }
    const line =
      new Date().toISOString().replace('T', ' ').slice(0, 19) +
      ' ' +
      parts.map((p) => (typeof p === 'string' ? p : JSON.stringify(p))).join(' ') +
      '\n'
    appendFileSync(file, line, 'utf-8')
  } catch {
    /* logging must never break the app */
  }
}
