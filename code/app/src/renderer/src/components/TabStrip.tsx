/**
 * The Chrome-style tab strip — a separate renderer (mode=chrome) that owns
 * the top TABBAR_H pixels of every window. Drag uses pointer capture, not
 * HTML5 DnD: DnD cannot cross OS-window boundaries, so the strip streams
 * screen coordinates to main, which does hit-testing across ALL windows and
 * re-parents live views (tear-off / adopt) on release.
 */
import { useEffect, useRef, useState } from 'react'
import { Logo } from './Logo'
import {
  AccountsIcon,
  ChartMiniIcon,
  DataIcon,
  NewsMiniIcon,
  PageMiniIcon,
} from './icons'

interface StripTab {
  id: number
  title: string
  icon: string
}
interface StripState {
  tabs: StripTab[]
  activeId: number | null
  maximized: boolean
}

declare global {
  interface Window {
    grindstoneTabs: {
      getState: () => Promise<StripState | null>
      onState: (cb: (s: StripState) => void) => () => void
      newTab: () => void
      activate: (id: number) => void
      close: (id: number) => void
      reorder: (id: number, toIndex: number) => void
      dragStart: (id: number) => void
      dragMove: (sx: number, sy: number) => void
      dragEnd: (sx: number, sy: number) => void
      minimize: () => void
      maximizeToggle: () => void
      closeWindow: () => void
    }
  }
}

function TabIcon({ icon }: { icon: string }) {
  switch (icon) {
    case 'accounts':
      return <AccountsIcon />
    case 'data':
      return <DataIcon />
    case 'chart':
      return <ChartMiniIcon />
    case 'news':
      return <NewsMiniIcon />
    case 'home':
      return <Logo size={14} />
    default:
      return <PageMiniIcon />
  }
}

const DRAG_THRESHOLD = 6

export function TabStrip() {
  const [state, setState] = useState<StripState>({ tabs: [], activeId: null, maximized: false })
  const drag = useRef<{ id: number; startX: number; startY: number; live: boolean } | null>(null)

  useEffect(() => {
    let off: (() => void) | null = null
    window.grindstoneTabs.getState().then((s) => s && setState(s))
    off = window.grindstoneTabs.onState(setState)
    return () => off?.()
  }, [])

  const onPointerDown = (e: React.PointerEvent, id: number) => {
    if (e.button !== 0) return
    window.grindstoneTabs.activate(id)
    drag.current = { id, startX: e.screenX, startY: e.screenY, live: false }
    ;(e.target as HTMLElement).setPointerCapture(e.pointerId)
  }
  const onPointerMove = (e: React.PointerEvent) => {
    const d = drag.current
    if (!d) return
    if (!d.live) {
      const dist = Math.hypot(e.screenX - d.startX, e.screenY - d.startY)
      if (dist < DRAG_THRESHOLD) return
      d.live = true
      window.grindstoneTabs.dragStart(d.id)
    }
    window.grindstoneTabs.dragMove(e.screenX, e.screenY)
  }
  const onPointerUp = (e: React.PointerEvent) => {
    const d = drag.current
    drag.current = null
    if (d?.live) window.grindstoneTabs.dragEnd(e.screenX, e.screenY)
  }

  return (
    <div className="strip">
      <div className="strip-tabs">
        {state.tabs.map((t) => (
          <div
            key={t.id}
            className={`strip-tab${t.id === state.activeId ? ' active' : ''}`}
            title={t.title}
            onPointerDown={(e) => onPointerDown(e, t.id)}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onAuxClick={(e) => {
              if (e.button === 1) window.grindstoneTabs.close(t.id) // middle click
            }}
          >
            <span className="strip-ico">
              <TabIcon icon={t.icon} />
            </span>
            <span className="strip-title">{t.title}</span>
            <span
              className="strip-x"
              title="Close tab"
              onPointerDown={(e) => e.stopPropagation()}
              onClick={(e) => {
                e.stopPropagation()
                window.grindstoneTabs.close(t.id)
              }}
            >
              ×
            </span>
          </div>
        ))}
        <div className="strip-new" title="New tab (Ctrl+T)" onClick={() => window.grindstoneTabs.newTab()}>
          +
        </div>
      </div>
      <div className="strip-spacer" onDoubleClick={() => window.grindstoneTabs.maximizeToggle()} />
      <div className="strip-winbtns">
        <button title="Minimize" onClick={() => window.grindstoneTabs.minimize()}>
          &#x2013;
        </button>
        <button title={state.maximized ? 'Restore' : 'Maximize'} onClick={() => window.grindstoneTabs.maximizeToggle()}>
          {state.maximized ? '❐' : '☐'}
        </button>
        <button className="close" title="Close window" onClick={() => window.grindstoneTabs.closeWindow()}>
          ×
        </button>
      </div>
    </div>
  )
}
