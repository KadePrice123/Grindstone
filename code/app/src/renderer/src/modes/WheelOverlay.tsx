/**
 * The gesture-wheel overlay (mode=wheel): a transparent full-window view that
 * main attaches above everything while a wheel is up.
 *
 * Division of labor: MAIN is the state machine and decides what a release
 * selects (src/shared/wheelGeometry.segmentAt — the same formula highlights
 * here). This component is the face: sectors, hover, the lock hub, the
 * spawn animation, and click-mode input.
 *
 * Hold-mode input usually reaches main from the view that owns mouse capture
 * (wherever the right press landed); if Chromium routes it here instead, the
 * listeners below forward it — main hears the drag either way.
 *
 * The APPS LAUNCHER shares this view: main pushes a launcher payload on the
 * same channels (a wheel and the launcher are never up together), and this
 * component renders the top-right apps panel instead of a wheel. Picks go
 * back as act(index) into the pushed pages; main re-checks `ready` itself.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { segmentAt } from '../../../shared/wheelGeometry'
import { pageIcon } from '../components/icons'
import {
  Quote,
  WheelConfig,
  WheelFace,
  WheelRender,
} from '../components/WheelFace'
import '../gestures.css' // launcher-* and wf-link-* styles live there

/** An app's real page glyph — the whole point of an icon launcher. First
 *  letters put 'A' on Accounts, APIs AND AI, which is unpickable. */
function glyph(key: string): React.JSX.Element {
  const Icon = pageIcon(key)
  return <Icon />
}

interface SpawnPayload {
  center: { x: number; y: number }
  mode: 'pending' | 'hold' | 'click'
  wheel: WheelRender
  config: WheelConfig
}

interface UpdatePayload {
  center?: { x: number; y: number }
  wheel?: WheelRender
  config?: WheelConfig
}

interface LauncherPage {
  key: string
  title: string
  ready: boolean
}

/** The apps-launcher payload (rides the wheel channels; see main/wheel.ts). */
interface LauncherPayload {
  pages: LauncherPage[]
  /** Window-Y where the chrome ends — the panel anchors just below it. */
  top: number
}

export function WheelOverlay() {
  const [state, setState] = useState<SpawnPayload | null>(null)
  const [launcher, setLauncher] = useState<LauncherPayload | null>(null)
  const [quotes, setQuotes] = useState<Record<string, Quote>>({})
  const [hover, setHover] = useState<number | null>(null)
  const stateRef = useRef(state)
  stateRef.current = state
  const launcherRef = useRef(launcher)
  launcherRef.current = launcher

  useEffect(() => {
    const offs = [
      window.grindstoneWheel.onSpawn((p) => {
        setQuotes({})
        setHover(null)
        const msg = p as SpawnPayload & { launcher?: LauncherPayload }
        if (msg.launcher) {
          setLauncher(msg.launcher)
          setState(null)
        } else {
          setLauncher(null)
          setState(msg)
        }
      }),
      window.grindstoneWheel.onUpdate((p) => {
        const u = p as UpdatePayload
        setState((s) =>
          s
            ? {
                ...s,
                ...(u.center ? { center: u.center } : {}),
                ...(u.wheel ? { wheel: u.wheel } : {}),
                ...(u.config ? { config: u.config } : {}),
              }
            : s
        )
        if (u.wheel) setHover(null)
      }),
      window.grindstoneWheel.onMode((m) => {
        setState((s) => (s ? { ...s, mode: m as SpawnPayload['mode'] } : s))
        if (m === 'click') setHover(null)
      }),
      window.grindstoneWheel.onPointer((x, y) => {
        const s = stateRef.current
        if (!s) return
        setHover(segmentAt(s.center.x, s.center.y, x, y, s.wheel.segments.length))
      }),
      window.grindstoneWheel.onQuotes((q) => setQuotes(q as Record<string, Quote>)),
      window.grindstoneWheel.onDespawn(() => {
        setState(null)
        setLauncher(null)
        setHover(null)
      }),
    ]
    // The mount handshake: the very first spawn is pushed while this page is
    // still loading — tell main we are listening NOW so it replays the live
    // session (without this, the first wheel of an app run rendered nothing).
    window.grindstoneWheel.ready()
    return () => offs.forEach((off) => off())
  }, [])

  // Escape closes a click-mode wheel or the launcher (main focuses this view
  // in both cases).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && (stateRef.current || launcherRef.current)) {
        window.grindstoneWheel.close()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  // If hold-mode events land HERE (capture fell to us), forward them to main.
  useEffect(() => {
    const move = (e: MouseEvent) => {
      const s = stateRef.current
      if (s && s.mode !== 'click' && (e.buttons & 2) !== 0) {
        window.grindstoneWheel.evt('move', Math.round(e.clientX), Math.round(e.clientY))
      }
    }
    const up = (e: MouseEvent) => {
      if (e.button === 2 && stateRef.current && stateRef.current.mode !== 'click') {
        window.grindstoneWheel.evt('up', Math.round(e.clientX), Math.round(e.clientY))
      }
    }
    window.addEventListener('mousemove', move, true)
    window.addEventListener('mouseup', up, true)
    return () => {
      window.removeEventListener('mousemove', move, true)
      window.removeEventListener('mouseup', up, true)
    }
  }, [])

  const onRootMouseDown = useCallback((e: React.MouseEvent) => {
    if (launcherRef.current) {
      // Launcher: ANY button on the stage closes it (the panel stops
      // propagation) — right click must not reposition like a wheel.
      window.grindstoneWheel.close()
      return
    }
    const s = stateRef.current
    if (!s) return
    if (e.button === 2) {
      // Right click MOVES the wheel (spec) — main clamps and echoes back.
      window.grindstoneWheel.move(Math.round(e.clientX), Math.round(e.clientY))
      return
    }
    if (e.button === 0 && s.mode === 'click') {
      // Left click outside the disc closes; segments/hub stopPropagation.
      window.grindstoneWheel.close()
    }
  }, [])

  if (!state && !launcher) return null

  return (
    <div
      className="wheel-stage"
      onMouseDown={onRootMouseDown}
      onContextMenu={(e) => e.preventDefault()}
    >
      {launcher ? (
        <div
          className="launcher-panel"
          style={{ top: launcher.top + 6 }}
          role="menu"
          aria-label="Apps"
          onMouseDown={(e) => e.stopPropagation()}
        >
          {launcher.pages.map((p, i) =>
            p.ready ? (
              <button
                key={p.key}
                className="launcher-tile"
                title={p.title}
                onClick={() => window.grindstoneWheel.act(i)}
              >
                <span className="launcher-glyph">{glyph(p.key)}</span>
                <span className="launcher-title">{p.title}</span>
              </button>
            ) : (
              // Announced, honestly dead — same rule as the search results.
              <div
                key={p.key}
                className="launcher-tile dead"
                title={`${p.title} — arrives in a later milestone`}
              >
                <span className="launcher-glyph">{glyph(p.key)}</span>
                <span className="launcher-title">{p.title}</span>
              </div>
            )
          )}
        </div>
      ) : state ? (
        <div
          className="wheel-holder"
          style={{ left: state.center.x, top: state.center.y }}
        >
          <WheelFace
            wheel={state.wheel}
            config={state.config}
            quotes={quotes}
            hover={hover}
            mode={state.mode}
            animate
            onSegment={(i) => state.mode === 'click' && window.grindstoneWheel.act(i)}
            onLock={() => window.grindstoneWheel.lockToggle()}
          />
        </div>
      ) : null}
    </div>
  )
}
