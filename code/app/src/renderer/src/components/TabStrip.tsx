/**
 * The Chrome-style chrome, in two rows:
 *   [△ home] [tabs…] [+] [drag] [win btns]
 *   [← → ⟳] [address ★] [⋮⋮⋮ apps]
 *
 * The star (end of the address bar, Chrome-style) adds/removes the current
 * location from favorites. No optimistic state: a mutation makes main
 * broadcast favorites:changed to every view, and the refetch is the truth.
 *
 * Back lives in the nav row ONLY. It used to sit in the tab row as well, and
 * once the nav row became permanent that was two identical arrows stacked on
 * top of each other — plus a third inside each page's own header.
 *
 * Drag uses pointer capture, not HTML5 DnD (DnD cannot cross OS windows).
 * The dragged tab visibly lifts and follows the pointer horizontally so the
 * grab reads immediately; main does the cross-window hit-testing.
 */
import { useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../api'
import { favoriteIdentity, favoriteLabel, sameFavorite, type Favorite } from '../favorites'
import { classify } from '../urls'
import '../split.css'
import { Logo } from './Logo'
import {
  AccountsIcon,
  BacktestIcon,
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
  activeFavicon: string
  loading: boolean
  draggingId: number | null
  split: { aId: number; bId: number; ratio: number } | null
  prevId: number | null
}

declare global {
  interface Window {
    grindstoneTabs: {
      getState: () => Promise<StripState | null>
      onState: (cb: (s: StripState) => void) => () => void
      newTab: () => void
      prevTab: () => void
      activate: (id: number) => void
      close: (id: number) => void
      reorder: (id: number, toIndex: number) => void
      dragStart: (id: number) => void
      dragMove: (sx: number, sy: number) => void
      dragEnd: (sx: number, sy: number) => void
      back: () => void
      forward: () => void
      reload: () => void
      goto: (kind: 'url' | 'route', value: string) => void
      home: () => void
      onFocusOmnibox: (cb: () => void) => () => void
      launcherToggle: () => void
      minimize: () => void
      maximizeToggle: () => void
      closeWindow: () => void
      tabMenu: (tabId: number, x: number, y: number) => void
      splitWith: (tabId: number, withTabId: number) => void
      closeSplit: () => void
    }
  }
}

function TabIcon({ icon }: { icon: string }) {
  switch (icon) {
    case 'accounts':
      return <AccountsIcon />
    case 'data':
      return <DataIcon />
    case 'backtest':
      return <BacktestIcon />
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
    activeFavicon: '',
    loading: false,
    draggingId: null,
    split: null,
    prevId: null,
  })
  const [dragDx, setDragDx] = useState(0)
  const [addr, setAddr] = useState('')
  const [editing, setEditing] = useState(false)
  const [favorites, setFavorites] = useState<Favorite[]>([])
  const drag = useRef<{ id: number; startX: number; startY: number; live: boolean } | null>(null)
  const addrRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    window.grindstoneTabs.getState().then((s) => s && setState(s))
    const off = window.grindstoneTabs.onState(setState)
    return () => off()
  }, [])

  // The star's truth: fetched on mount and after ANY view mutates favorites
  // (main broadcasts). A failed fetch leaves the last known list — the next
  // broadcast retries, and POST is an idempotent upsert either way.
  useEffect(() => {
    let alive = true
    const refetch = () => {
      api<{ favorites: Favorite[] }>('GET', '/api/favorites')
        .then((r) => alive && setFavorites(r.favorites))
        .catch(() => {})
    }
    refetch()
    const off = window.grindstone.onFavoritesChanged(refetch)
    return () => {
      alive = false
      off()
    }
  }, [])

  // The gesture wheel's Search tool lands here: focus the bar, ready to type.
  useEffect(() => {
    const off = window.grindstoneTabs.onFocusOmnibox(() => {
      addrRef.current?.focus()
      addrRef.current?.select()
    })
    return off
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

  /** Omnibox submit: web address, .gs platform address, or a search. */
  const submitAddress = () => {
    const text = addr.trim()
    if (!text) return
    setEditing(false)
    const dest = classify(text)
    if (dest.kind === 'url') window.grindstoneTabs.goto('url', dest.url)
    else if (dest.kind === 'route') window.grindstoneTabs.goto('route', dest.route)
    else window.grindstoneTabs.goto('route', `search:${dest.query}`)
  }

  // What starring the current tab MEANS — null while a tab has no starrable
  // identity yet (still loading), which hides the star instead of guessing.
  const identity = favoriteIdentity(state.activeKind, state.activeUrl)
  const starred = identity ? favorites.find((f) => sameFavorite(f, identity)) : undefined
  /** A refused star used to do NOTHING — at 48 favorites the button was
   *  simply dead. The reason the backend gives is shown on the control
   *  itself; it clears on the next successful toggle or tab change. */
  const [starErr, setStarErr] = useState('')
  useEffect(() => setStarErr(''), [state.activeId, state.activeUrl])

  const toggleStar = () => {
    if (!identity) return
    setStarErr('')
    if (starred) {
      api('DELETE', `/api/favorites/${starred.id}`).catch((e) =>
        setStarErr(e instanceof ApiError ? e.message : 'could not remove this favorite')
      )
      return
    }
    const title = state.tabs.find((t) => t.id === state.activeId)?.title ?? ''
    // A site's favicon often lands a beat after the page does. Guessing the
    // conventional path costs nothing (fetch_icon degrades to '' on a miss)
    // and is the difference between a real tab image and a letter tile.
    let iconUrl: string | undefined
    if (identity.kind === 'web') {
      iconUrl = state.activeFavicon || undefined
      if (!iconUrl) {
        try {
          iconUrl = `${new URL(identity.key).origin}/favicon.ico`
        } catch {
          iconUrl = undefined
        }
      }
    }
    api('POST', '/api/favorites', {
      kind: identity.kind,
      key: identity.key,
      label: favoriteLabel(identity, title),
      icon_url: iconUrl,
    }).catch((e) =>
      setStarErr(e instanceof ApiError ? e.message : 'could not save this favorite')
    )
  }

  const navBar =
    state.activeKind !== null ? (
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
        <div className="addr-wrap">
          <input
            ref={addrRef}
            className={state.activeKind === 'browser' ? 'addr' : 'addr gs'}
            value={addr}
            spellCheck={false}
            placeholder="Search, or type an address — google.com, accounts.gs, SPY"
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
                submitAddress()
                ;(e.target as HTMLInputElement).blur()
              } else if (e.key === 'Escape') {
                setAddr(state.activeUrl)
                setEditing(false)
                ;(e.target as HTMLInputElement).blur()
              }
            }}
          />
          {identity && (
            <button
              className={`addr-star${starred ? ' on' : ''}${starErr ? ' err' : ''}`}
              title={
                starErr
                  ? `Not saved — ${starErr}`
                  : starred
                    ? 'Remove from favorites'
                    : 'Add to favorites'
              }
              onClick={toggleStar}
            >
              {starErr ? '!' : starred ? '★' : '☆'}
            </button>
          )}
        </div>
        <button
          className="apps-btn"
          title="Apps"
          onClick={() => window.grindstoneTabs.launcherToggle()}
        >
          {/* 3x3 dot grid, inline rather than icons.tsx: its Icon wrapper is
              stroke-only (fill none), which would render these dots as rings */}
          <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor" aria-label="Apps">
            <circle cx="5" cy="5" r="2" />
            <circle cx="12" cy="5" r="2" />
            <circle cx="19" cy="5" r="2" />
            <circle cx="5" cy="12" r="2" />
            <circle cx="12" cy="12" r="2" />
            <circle cx="19" cy="12" r="2" />
            <circle cx="5" cy="19" r="2" />
            <circle cx="12" cy="19" r="2" />
            <circle cx="19" cy="19" r="2" />
          </svg>
        </button>
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

      <div className="strip-tabs">
        {state.tabs.map((t) => {
          const dragging = state.draggingId === t.id
          // Paired even while hidden: the split outlives activating a third
          // tab, and the strip is where that lingering link stays legible.
          const mate =
            state.split !== null && (t.id === state.split.aId || t.id === state.split.bId)
          return (
            <div
              key={t.id}
              className={`strip-tab${t.id === state.activeId ? ' active' : ''}${dragging ? ' dragging' : ''}${mate ? ' split-mate' : ''}`}
              style={dragging ? { transform: `translateX(${dragDx}px)` } : undefined}
              title={t.url ?? t.title}
              onPointerDown={(e) => onPointerDown(e, t.id)}
              onPointerMove={onPointerMove}
              onPointerUp={endDrag}
              onPointerCancel={endDrag}
              onContextMenu={(e) => {
                e.preventDefault()
                window.grindstoneTabs.tabMenu(t.id, Math.round(e.clientX), Math.round(e.clientY))
              }}
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
      </div>

      {/* FIXED tab actions — outside the scroller, so a strip full of tabs
          can never push New-tab out of reach (it used to sit inline after
          the last tab and scrolled away with them). */}
      <div className="strip-actions">
        <button
          className="strip-btn"
          title="New tab"
          onClick={() => window.grindstoneTabs.newTab()}
        >
          +
        </button>
        <button
          className="strip-btn"
          title="Previous tab — jump back to the last tab you were on"
          disabled={state.prevId === null}
          onClick={() => window.grindstoneTabs.prevTab()}
        >
          ⇄
        </button>
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
