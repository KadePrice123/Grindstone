/**
 * The Chrome-style tab strip: [△ home] [← back] [tabs…] [+] [drag] [win btns]
 *
 * Drag uses pointer capture, not HTML5 DnD (DnD cannot cross OS windows).
 * The dragged tab visibly lifts and follows the pointer horizontally so the
 * grab reads immediately; main does the cross-window hit-testing.
 */
import { useEffect, useRef, useState } from 'react'
import { Logo } from './Logo'
import {
  AccountsIcon,
  BrowserMiniIcon,
  ChartMiniIcon,
  DataIcon,
  NewsMiniIcon,
  PageMiniIcon,
  SearchMiniIcon,
} from './icons'

interface StripTab {
  id: number
  title: string
  icon: string
  kind: 'app' | 'browser'
  url?: string
}
interface StripState {
  tabs: StripTab[]
  activeId: number | null
  maximized: boolean
  bounds: { x: number; y: number; width: number; height: number }
  canGoBack: boolean
  canGoForward: boolean
  activeKind: 'app' | 'browser' | null
  activeUrl: string
  loading: boolean
  draggingId: number | null
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
      back: () => void
      forward: () => void
      reload: () => void
      goto: (url: string) => void
      home: () => void
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
    case 'search':
      return <SearchMiniIcon />
    case 'browser':
      return <BrowserMiniIcon />
    case 'home':
      return <Logo size={14} />
    default:
      return <PageMiniIcon />
  }
}

const DRAG_THRESHOLD = 6

export function TabStrip() {
  const [state, setState] = useState<StripState>({
    tabs: [],
    activeId: null,
    maximized: false,
    bounds: { x: 0, y: 0, width: 0, height: 0 },
    canGoBack: false,
    canGoForward: false,
    activeKind: null,
    activeUrl: '',
    loading: false,
    draggingId: null,
  })
  const [dragDx, setDragDx] = useState(0)
  const [addr, setAddr] = useState('')
  const [editing, setEditing] = useState(false)
  const drag = useRef<{ id: number; startX: number; startY: number; live: boolean } | null>(null)

  useEffect(() => {
    window.grindstoneTabs.getState().then((s) => s && setState(s))
    const off = window.grindstoneTabs.onState(setState)
    return () => off()
  }, [])

  // Follow the page's URL unless the user is mid-edit in the address bar.
  useEffect(() => {
    if (!editing) setAddr(state.activeUrl)
  }, [state.activeUrl, state.activeId, editing])

  const onPointerDown = (e: React.PointerEvent, id: number) => {
    if (e.button !== 0) return
    window.grindstoneTabs.activate(id)
    drag.current = { id, startX: e.screenX, startY: e.screenY, live: false }
    ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
  }
  const onPointerMove = (e: React.PointerEvent) => {
    const d = drag.current
    if (!d) return
    const dx = e.screenX - d.startX
    if (!d.live) {
      if (Math.hypot(dx, e.screenY - d.startY) < DRAG_THRESHOLD) return
      d.live = true
      window.grindstoneTabs.dragStart(d.id)
    }
    setDragDx(dx)
    window.grindstoneTabs.dragMove(e.screenX, e.screenY)
  }
  const endDrag = (e: React.PointerEvent) => {
    const d = drag.current
    drag.current = null
    setDragDx(0)
    if (d?.live) window.grindstoneTabs.dragEnd(e.screenX, e.screenY)
  }

  const navBar =
    state.activeKind === 'browser' ? (
      <div className="navbar">
        <button
          className="strip-btn"
          title="Back"
          disabled={!state.canGoBack}
          onClick={() => window.grindstoneTabs.back()}
        >
          ←
        </button>
        <button
          className="strip-btn"
          title="Forward"
          disabled={!state.canGoForward}
          onClick={() => window.grindstoneTabs.forward()}
        >
          →
        </button>
        <button
          className="strip-btn"
          title="Reload"
          onClick={() => window.grindstoneTabs.reload()}
        >
          {state.loading ? '×' : '⟳'}
        </button>
        <input
          className="addr"
          value={addr}
          spellCheck={false}
          placeholder="Search or enter address"
          onChange={(e) => {
            setEditing(true)
            setAddr(e.target.value)
          }}
          onFocus={(e) => {
            setEditing(true)
            e.currentTarget.select()
          }}
          onBlur={() => setEditing(false)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              window.grindstoneTabs.goto(addr)
              setEditing(false)
              ;(e.target as HTMLInputElement).blur()
            } else if (e.key === 'Escape') {
              setAddr(state.activeUrl)
              setEditing(false)
              ;(e.target as HTMLInputElement).blur()
            }
          }}
        />
      </div>
    ) : null

  return (
    <div className={navBar ? 'chrome with-nav' : 'chrome'}>
      <div className="strip">
      <button
        className="strip-btn home"
        title="Home — Grindstone"
        onClick={() => window.grindstoneTabs.home()}
      >
        <Logo size={18} />
      </button>
      <button
        className="strip-btn"
        title="Back"
        disabled={!state.canGoBack}
        onClick={() => window.grindstoneTabs.back()}
      >
        ←
      </button>

      <div className="strip-tabs">
        {state.tabs.map((t) => {
          const dragging = state.draggingId === t.id
          return (
            <div
              key={t.id}
              className={`strip-tab${t.id === state.activeId ? ' active' : ''}${dragging ? ' dragging' : ''}`}
              style={dragging ? { transform: `translateX(${dragDx}px)` } : undefined}
              title={t.url ?? t.title}
              onPointerDown={(e) => onPointerDown(e, t.id)}
              onPointerMove={onPointerMove}
              onPointerUp={endDrag}
              onPointerCancel={endDrag}
              onAuxClick={(e) => {
                if (e.button === 1) window.grindstoneTabs.close(t.id)
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
          )
        })}
        <div className="strip-new" title="New tab" onClick={() => window.grindstoneTabs.newTab()}>
          +
        </div>
      </div>

      <div className="strip-spacer" onDoubleClick={() => window.grindstoneTabs.maximizeToggle()} />
      <div className="strip-winbtns">
        <button title="Minimize" onClick={() => window.grindstoneTabs.minimize()}>
          &#x2013;
        </button>
        <button
          title={state.maximized ? 'Restore' : 'Maximize'}
          onClick={() => window.grindstoneTabs.maximizeToggle()}
        >
          {state.maximized ? '❐' : '☐'}
        </button>
        <button className="close" title="Close window" onClick={() => window.grindstoneTabs.closeWindow()}>
          ×
        </button>
        </div>
      </div>
      {navBar}
    </div>
  )
}
