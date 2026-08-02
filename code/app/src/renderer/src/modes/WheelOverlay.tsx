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
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { segmentAt } from '../../../shared/wheelGeometry'
import {
  Quote,
  WheelConfig,
  WheelFace,
  WheelRender,
} from '../components/WheelFace'

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

export function WheelOverlay() {
  const [state, setState] = useState<SpawnPayload | null>(null)
  const [quotes, setQuotes] = useState<Record<string, Quote>>({})
  const [hover, setHover] = useState<number | null>(null)
  const stateRef = useRef(state)
  stateRef.current = state

  useEffect(() => {
    const offs = [
      window.grindstoneWheel.onSpawn((p) => {
        setQuotes({})
        setHover(null)
        setState(p as SpawnPayload)
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
        setHover(null)
      }),
    ]
    // The mount handshake: the very first spawn is pushed while this page is
    // still loading — tell main we are listening NOW so it replays the live
    // session (without this, the first wheel of an app run rendered nothing).
    window.grindstoneWheel.ready()
    return () => offs.forEach((off) => off())
  }, [])

  // Escape closes a click-mode wheel (main focuses this view on that switch).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && stateRef.current) window.grindstoneWheel.close()
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

  if (!state) return null

  return (
    <div
      className="wheel-stage"
      onMouseDown={onRootMouseDown}
      onContextMenu={(e) => e.preventDefault()}
    >
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
    </div>
  )
}
