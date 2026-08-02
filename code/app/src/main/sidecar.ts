/**
 * Spawn and supervise the Python sidecar (REQUIREMENTS.md 6.9).
 *
 * Handshake: sidecar binds 127.0.0.1:<random>, prints one JSON line
 * {"event":"listening","port":N}; we health-check before publishing state.
 * The boot token goes over the environment, never argv.
 *
 * Two ordering rules, both learned from review:
 *  - `state` is published only AFTER the health check passes, so the proxy is
 *    never pointed at a port that never came up.
 *  - the exit handler that schedules restarts is armed BEFORE the health
 *    check, so a process that dies during that window still triggers one.
 */
import { spawn, ChildProcess, execFile } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { existsSync } from 'node:fs'
import path from 'node:path'
import { app } from 'electron'

export interface SidecarState {
  port: number
  bootToken: string
  version: string
}

type Status = 'starting' | 'ready' | 'crashed'
type StatusListener = (status: Status, detail?: string) => void

const MAX_RESTARTS = 3

export class Sidecar {
  private proc: ChildProcess | null = null
  private state: SidecarState | null = null
  private restarts = 0
  private quitting = false
  private listeners: StatusListener[] = []

  onStatus(fn: StatusListener): void {
    this.listeners.push(fn)
  }

  private emit(status: Status, detail?: string): void {
    for (const fn of this.listeners) fn(status, detail)
  }

  get current(): SidecarState | null {
    return this.state
  }

  /** code/ directory — parent of app/ in dev, resources/ when packaged. */
  private codeDir(): string {
    return path.resolve(app.getAppPath(), '..')
  }

  private pythonCommand(): { cmd: string; args: string[]; cwd: string } {
    if (app.isPackaged) {
      const exe = path.join(process.resourcesPath, 'backend', 'grindstone-backend.exe')
      return { cmd: exe, args: [], cwd: path.dirname(exe) }
    }
    const codeDir = this.codeDir()
    const venvPy = path.resolve(
      codeDir,
      '..',
      '..',
      '..',
      'venvs',
      'dashboard',
      'Scripts',
      'python.exe'
    )
    const cmd = existsSync(venvPy) ? venvPy : 'python'
    return { cmd, args: ['-m', 'backend.main'], cwd: codeDir }
  }

  /** Start, and keep restarting on failure up to MAX_RESTARTS. */
  async start(): Promise<SidecarState> {
    try {
      return await this.launch()
    } catch (e) {
      this.scheduleRestart(String(e))
      throw e
    }
  }

  private scheduleRestart(detail: string): void {
    if (this.quitting) return
    this.emit('crashed', detail)
    if (this.restarts >= MAX_RESTARTS) return
    this.restarts += 1
    const delay = 500 * 2 ** this.restarts
    setTimeout(() => {
      this.launch()
        .then(() => this.emit('ready'))
        .catch((e) => this.scheduleRestart(String(e)))
    }, delay)
  }

  private async launch(): Promise<SidecarState> {
    this.emit('starting')
    const bootToken = randomBytes(32).toString('base64url')
    const { cmd, args, cwd } = this.pythonCommand()

    const proc = spawn(cmd, args, {
      cwd,
      env: { ...process.env, GRINDSTONE_BOOT_TOKEN: bootToken },
      // stdin is a pipe we never write to: when this process dies, the pipe
      // closes and the sidecar's watchdog sees EOF and exits. Without it a
      // force-killed shell leaves an orphaned python.exe holding the DB.
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    })
    this.proc = proc

    let stderrTail = ''
    proc.stderr?.on('data', (d: Buffer) => {
      stderrTail = (stderrTail + d.toString()).slice(-4000)
    })

    const announced = await new Promise<{ port: number; version: string }>((resolve, reject) => {
      const timer = setTimeout(
        () => reject(new Error(`sidecar did not announce a port in 20s\n${stderrTail}`)),
        20_000
      )
      let buf = ''
      proc.stdout?.on('data', (d: Buffer) => {
        buf += d.toString()
        let nl: number
        while ((nl = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, nl).trim()
          buf = buf.slice(nl + 1)
          if (!line) continue
          try {
            const msg = JSON.parse(line)
            if (msg.event === 'listening') {
              clearTimeout(timer)
              resolve({ port: msg.port, version: msg.version ?? '?' })
            }
          } catch {
            /* non-JSON chatter is fine */
          }
        }
      })
      proc.once('exit', (code) => {
        clearTimeout(timer)
        reject(new Error(`sidecar exited with code ${code} before listening\n${stderrTail}`))
      })
    })

    // Armed before the health check: a death in that window still restarts.
    proc.on('exit', (code) => {
      if (this.proc !== proc) return // superseded by a newer process
      this.state = null
      this.scheduleRestart(`exit code ${code}`)
    })

    const health = await fetch(`http://127.0.0.1:${announced.port}/api/health`, {
      headers: { 'X-App-Token': bootToken },
    })
    if (!health.ok) throw new Error(`sidecar health check failed: HTTP ${health.status}`)

    // Only now is the backend safe to route requests to.
    this.state = { port: announced.port, bootToken, version: announced.version }
    this.restarts = 0
    this.emit('ready')
    return this.state
  }

  /** Kill the whole child tree — an orphaned python.exe is the classic bug here. */
  stop(): void {
    this.quitting = true
    const pid = this.proc?.pid
    this.proc = null
    this.state = null
    if (!pid) return
    if (process.platform === 'win32') {
      execFile('taskkill', ['/pid', String(pid), '/T', '/F'], () => {})
    } else {
      process.kill(pid, 'SIGTERM')
    }
  }
}
