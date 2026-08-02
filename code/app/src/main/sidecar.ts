/**
 * Spawn and supervise the Python sidecar (REQUIREMENTS.md 6.9).
 *
 * Handshake: sidecar binds 127.0.0.1:<random>, prints one JSON line
 * {"event":"listening","port":N} and we health-check it before use.
 * The boot token goes over the environment, never argv.
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

type StatusListener = (status: 'starting' | 'ready' | 'crashed', detail?: string) => void

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

  private emit(status: 'starting' | 'ready' | 'crashed', detail?: string): void {
    for (const fn of this.listeners) fn(status, detail)
  }

  get current(): SidecarState | null {
    return this.state
  }

  /** code/ directory — parent of app/ in dev, resources/ when packaged. */
  private codeDir(): string {
    // app.getAppPath() in dev = code/app; packaged = resources/app.asar
    return path.resolve(app.getAppPath(), '..')
  }

  private pythonCommand(): { cmd: string; args: string[]; cwd: string } {
    if (app.isPackaged) {
      const exe = path.join(process.resourcesPath, 'backend', 'grindstone-backend.exe')
      return { cmd: exe, args: [], cwd: path.dirname(exe) }
    }
    const codeDir = this.codeDir()
    // Claude/venvs/dashboard relative to Claude/projects/dashboard/code
    const venvPy = path.resolve(codeDir, '..', '..', '..', 'venvs', 'dashboard', 'Scripts', 'python.exe')
    const cmd = existsSync(venvPy) ? venvPy : 'python'
    return { cmd, args: ['-m', 'backend.main'], cwd: codeDir }
  }

  async start(): Promise<SidecarState> {
    this.emit('starting')
    const bootToken = randomBytes(32).toString('base64url')
    const { cmd, args, cwd } = this.pythonCommand()

    const proc = spawn(cmd, args, {
      cwd,
      env: { ...process.env, GRINDSTONE_BOOT_TOKEN: bootToken },
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    })
    this.proc = proc

    let stderrTail = ''
    proc.stderr?.on('data', (d: Buffer) => {
      stderrTail = (stderrTail + d.toString()).slice(-4000)
    })

    const port = await new Promise<number>((resolve, reject) => {
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
              this.state = { port: msg.port, bootToken, version: msg.version ?? '?' }
              resolve(msg.port)
            }
          } catch {
            /* non-JSON chatter is fine */
          }
        }
      })
      proc.on('exit', (code) => {
        clearTimeout(timer)
        reject(new Error(`sidecar exited with code ${code} before listening\n${stderrTail}`))
      })
    })

    // Health check before declaring ready.
    const health = await fetch(`http://127.0.0.1:${port}/api/health`, {
      headers: { 'X-App-Token': bootToken },
    })
    if (!health.ok) throw new Error(`sidecar health check failed: HTTP ${health.status}`)

    proc.on('exit', (code) => {
      this.state = null
      if (this.quitting) return
      this.emit('crashed', `exit code ${code}`)
      if (this.restarts < MAX_RESTARTS) {
        this.restarts += 1
        const delay = 500 * 2 ** this.restarts
        setTimeout(() => {
          this.start().catch((e) => this.emit('crashed', String(e)))
        }, delay)
      }
    })

    this.restarts = 0
    this.emit('ready')
    return this.state!
  }

  /** Kill the whole child tree — an orphaned python.exe is the classic bug here. */
  stop(): void {
    this.quitting = true
    const pid = this.proc?.pid
    if (!pid) return
    if (process.platform === 'win32') {
      execFile('taskkill', ['/pid', String(pid), '/T', '/F'], () => {})
    } else {
      this.proc?.kill('SIGTERM')
    }
    this.proc = null
    this.state = null
  }
}
