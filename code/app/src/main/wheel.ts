/**
 * Gesture wheels (FR-SHELL-4): SolidWorks-style right-click radial menus.
 *
 * Two ways in, exactly as specced:
 *   CLICK  — press and release without moving: the wheel stays up with the
 *            lock/unlock hub in the center; LEFT clicks interact; left click
 *            outside closes; right click moves the wheel.
 *   HOLD   — press, drag over a segment, release: the segment acts. Releasing
 *            over a wheel-nav segment switches wheels and KEEPS the wheel
 *            open in click mode (so the new wheel does not auto-despawn);
 *            releasing in the center dead-zone or over nothing despawns.
 *            No lock hub while holding.
 *
 * Architecture: ONE transparent WebContentsView overlay, attached to
 * whichever window spawned the wheel and removed on despawn. Right-button
 * events reach main from every view kind — app pages and the chrome via the
 * bridge, third-party pages via the minimal browser preload — because during
 * a hold the ORIGIN view has mouse capture and the overlay may never hear a
 * thing. Main is the single state machine; the overlay is the face.
 *
 * The APPS LAUNCHER (the chrome's apps button) rides the SAME overlay view:
 * main pushes a launcher payload instead of a wheel and the overlay renders
 * a top-right panel over any tab, browser tabs included. Wheel and launcher
 * are mutually exclusive — there is one overlay.
 */
import { WebContentsView, ipcMain, webContents } from 'electron'
import path from 'node:path'
import { isSignedIn, mainRequest } from './api'
import { log } from './log'
import { NAVBAR_H, TABBAR_H, TabManager, WheelTabInfo } from './tabs'

// Geometry lives in src/shared/wheelGeometry.ts, imported by BOTH sides.
// Main computes the selection ITSELF at release: an earlier design had the
// overlay report hover back over IPC, but the release event can beat the
// last hover report across the process boundary — acting on a stale segment.
import { WHEEL_RADIUS, segmentAt } from '../shared/wheelGeometry'
import { predictBest, type PadHint } from './predictCore'

const CLICK_MOVE_THRESHOLD = 10 // px of travel that turns a click into a hold
const EDGE_MARGIN = 12

interface Segment {
  type:
    | 'wheel' | 'nav' | 'tool' | 'ticker' | 'chart' | 'placeholder' | 'empty'
    | 'tab' | 'page' | 'link' | 'data'
  label: string
  // one of, depending on type:
  wheel?: string
  route?: string
  tool?: string
  ticker?: string
  tabId?: number
  dir?: number // page: -1 back / +1 forward
  address?: string // link: a favorited .gs address or http(s) URL
  icon?: string
  symbol?: string // the target wheel's symbol, for wheel segments
  entryId?: string // data: a notepad entry id (picker segments; transient,
                   // never stored -- wheels.py never sees these)
  /** What RIGHT-clicking this segment will do, given what sits under the
   *  spawn (docs/DATA_EXCHANGE.md DX-13/14). Computed at spawn and shown on
   *  the face: a predicted action the user cannot see before releasing is a
   *  misclick generator. Transient like entryId; never stored. */
  hint?: string
  hintKind?: string   // 'address' | 'contract' — what the quick variant does
  hintArg?: string    // the address or occ the prediction resolved
  disabled?: boolean
}

interface WheelDef {
  id: string
  name: string
  symbol: string
  dynamic?: string
  segments: Segment[]
}

interface WheelDoc {
  config: {
    ticker_display: 'percent' | 'price'
    ticker_colors: boolean
    locked: string | null
    /** DX-15: element class -> wheel id. Ships as { chart: 'chart' }, the
     *  binding this file used to hardcode. Optional in the type because a
     *  doc from an older build has no such key — the backend regenerates on
     *  version mismatch, but main must not crash in the window before it. */
    class_wheels?: Record<string, string>
  }
  wheels: WheelDef[]
}

/** A favorites row as /api/favorites returns it (backend/favorites.py). */
interface FavoriteRow {
  id: number
  kind: 'symbol' | 'page' | 'web'
  key: string
  label: string
  icon: string
  pos: number
}

/** A provider-app row as /api/pages returns it — the launcher's registry. */
interface LauncherPage {
  key: string
  title: string
  ready: boolean
}

/** What sat under the spawning right-click. Charts declare themselves (and
 *  their live state) via data attributes read by wheelEvents.ts; the browser
 *  preload sends no ctx at all — absence is the normal case, never an error. */
interface WheelCtx {
  context: string
  /** The OCC symbol under the spawn, when an enrolled element declared one
   *  (chain rows, heatmap cells). Carries the class prediction's target. */
  occ?: string
  symbols: string[]
  indicators: string[]
  hidden: string[]
  /** Current bar timeframe, one of TIMEFRAMES — null when the page sent
   *  something unrecognized (treated the same as absent: no mark). */
  timeframe: string | null
  /** Active global-visibility states: 'drawhidden' and/or 'indhidden'. */
  flags: string[]
  /** The soloed symbol while isolation is on, null otherwise. */
  isolated: string | null
}

// Mirrors backend/wheels.py TIMEFRAMES — the tf:* vocabulary CHART_TOOLS
// accepts. Order is the wheel order (12 o'clock clockwise).
const TIMEFRAMES = ['1Min', '5Min', '15Min', '1Hour', '1Day'] as const

/** The 4th wheel:evt arg crosses from ANY renderer, including hardened
 *  browser views — treat it as untrusted and keep only well-shaped strings. */
function sanitizeCtx(raw: unknown): WheelCtx | null {
  if (!raw || typeof raw !== 'object') return null
  const r = raw as Record<string, unknown>
  const context = r['context']
  if (typeof context !== 'string' || !context) return null
  const arr = (v: unknown): string[] =>
    Array.isArray(v)
      ? v.filter((x): x is string => typeof x === 'string' && x.length > 0 && x.length <= 12)
          .slice(0, 24)
      : []
  const tf = r['timeframe']
  const iso = r['isolated']
  const occ = r['occ']
  return {
    context: context.slice(0, 24),
    // An OCC symbol is 15-21 chars. Capped and charset-checked like every
    // other ctx field, because this value ends up inside an address the
    // shell will open — sanitizeCtx is the trust boundary, not a formality.
    occ: typeof occ === 'string' && /^[A-Z0-9.]{6,32}$/.test(occ) ? occ : undefined,
    symbols: arr(r['symbols']).map((s) => s.toUpperCase()),
    indicators: arr(r['indicators']),
    hidden: arr(r['hidden']).map((s) => s.toUpperCase()),
    // Strict allowlist, not just shape: an unknown timeframe would silently
    // mark nothing while looking valid downstream.
    timeframe:
      typeof tf === 'string' && (TIMEFRAMES as readonly string[]).includes(tf) ? tf : null,
    flags: arr(r['flags']),
    isolated:
      typeof iso === 'string' && iso.length > 0 && iso.length <= 12
        ? iso.toUpperCase()
        : null,
  }
}

interface Session {
  winId: number
  mode: 'pending' | 'hold' | 'click'
  center: { x: number; y: number }
  start: { x: number; y: number }
  moved: boolean
  wheelId: string
  /** Current page of the shown dynamic wheel (tabs and favorites paginate). */
  dynPage: number
  /** Last hold-mode pointer position, window coords — the release acts on
   *  segmentAt(center, THIS), never on renderer-reported hover. */
  lastPointer: { x: number; y: number }
  /** Chart context captured at the spawning 'down' (null off-chart). The
   *  dynamic chart wheels are built from it for the LIFE of the session —
   *  navigating wheels never re-reads the page. */
  ctx: WheelCtx | null
  /** The starred pages, fetched once at spawn (null = the fetch failed).
   *  One snapshot per session, the same rule quotes follow — the Favorites
   *  wheel must not reshuffle while it is open. */
  favorites: FavoriteRow[] | null
  /** The tab the 'down' came from, and that view's webContents id: chart
   *  actions go back THERE, never to whatever is active by the time the
   *  user releases. Null when the down came from chrome or the overlay. */
  originTabId: number | null
  originWcId: number | null
  doc: WheelDoc
  segments: Segment[]
  detachListeners: () => void
}

export class WheelManager {
  private tabs: TabManager
  private overlay: WebContentsView | null = null
  private session: Session | null = null
  /** spawn() awaits the config fetch; a release landing inside that window
   *  must not be dropped — a very fast click would leave the wheel stuck in
   *  pending, interacting with nothing. */
  private spawnSeq = 0
  private releaseDuringSpawn = false
  /** The apps launcher, when open. Never live alongside a wheel session —
   *  they share the one overlay view. */
  private launcherSession: {
    winId: number
    pages: LauncherPage[]
    detachListeners: () => void
  } | null = null
  /** openLauncher() awaits the registry fetch; a re-toggle in that window
   *  must supersede the stale open, not race it. */
  private launcherSeq = 0

  constructor(tabs: TabManager) {
    this.tabs = tabs
    tabs.onWindowGone = (winId) => {
      if (this.session?.winId === winId) this.despawn()
      if (this.launcherSession?.winId === winId) this.closeLauncher()
    }
    // REVIEW 2026-08-02: an auto-lock (renderer 401, sidecar crash) swapped
    // the window to the sign-in form UNDER a still-live wheel overlay — in
    // hold mode every input path to close it was gated off, leaving the
    // form unclickable. Lock kills the wheel (and the launcher, which sits
    // on the same overlay), unconditionally.
    tabs.onLockChanged = (locked) => {
      if (locked) {
        this.despawn()
        this.closeLauncher()
      }
    }
    this.registerIpc()
  }

  // ------------------------------------------------------------- the overlay
  /** Created once, loaded once, then attached/detached per spawn — a wheel
   *  must appear instantly, not after a renderer boots. */
  private ensureOverlay(preload: string): WebContentsView {
    if (this.overlay && !this.overlay.webContents.isDestroyed()) return this.overlay
    const view = new WebContentsView({
      webPreferences: {
        preload,
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
      },
    })
    view.setBackgroundColor('#00000000') // transparent: the page below stays visible
    const dev = process.env['ELECTRON_RENDERER_URL']
    const qs = 'mode=wheel'
    if (dev) view.webContents.loadURL(`${dev}/?${qs}`)
    else {
      view.webContents.loadFile(path.join(__dirname, '../renderer/index.html'), {
        search: `?${qs}`,
      })
    }
    this.overlay = view
    return view
  }

  preloadPath = ''

  // --------------------------------------------------------------- the doc
  private async loadDoc(): Promise<WheelDoc | null> {
    const res = await mainRequest<WheelDoc>('GET', '/api/wheels')
    return res.status === 200 && res.body ? res.body : null
  }

  private async loadFavorites(): Promise<FavoriteRow[] | null> {
    const res = await mainRequest<{ favorites: FavoriteRow[] }>('GET', '/api/favorites')
    const favs = res.body?.favorites
    return res.status === 200 && Array.isArray(favs) ? favs : null
  }

  /** Resolve a wheel id into renderable segments (dynamic wheels included). */
  private materialize(
    doc: WheelDoc, wheelId: string, dynPage: number,
    ctx: WheelCtx | null, favorites: FavoriteRow[] | null
  ): {
    def: WheelDef
    segments: Segment[]
    pages: number
  } {
    const def = doc.wheels.find((w) => w.id === wheelId) ?? doc.wheels[0]
    if (!def.dynamic) {
      const segments = def.segments.map((s) => this.predictFor(
        this.decorateChartState({
          ...s,
          disabled: s.type === 'placeholder' || s.type === 'empty',
          symbol: s.type === 'wheel'
            ? doc.wheels.find((w) => w.id === s.wheel)?.symbol
            : undefined,
          label: s.label || (s.type === 'ticker' ? (s.ticker ?? '') : s.label),
        }, ctx),
        ctx))
      return { def, segments, pages: 1 }
    }
    if (def.dynamic === 'favorites') {
      return { def, ...this.favoriteSegments(doc, favorites, dynPage) }
    }
    if (def.dynamic !== 'tabs') {
      return { def, segments: this.chartSegments(def.dynamic, doc, ctx), pages: 1 }
    }
    // The Tabs wheel is built from reality at spawn time. Eight tabs fit on
    // one wheel; more than eight paginate — 6 per page, next at E, prev at W,
    // wrapping — exactly the spec's "right goes to tab wheel 2, 3…".
    const all = this.tabs.allTabs()
    const tabSeg = (t: WheelTabInfo): Segment => ({
      type: 'tab', label: t.title.slice(0, 14), tabId: t.id, icon: t.icon,
    })
    if (all.length <= 8) {
      return { def, segments: all.map(tabSeg), pages: 1 }
    }
    const pages = Math.ceil(all.length / 6)
    const page = ((dynPage % pages) + pages) % pages
    const slice = all.slice(page * 6, page * 6 + 6).map(tabSeg)
    const segments: Segment[] = [
      ...slice.slice(0, 2),
      { type: 'page', dir: +1, label: `Tabs ${((page + 1) % pages) + 1}/${pages}` }, // E
      ...slice.slice(2, 5),
      { type: 'page', dir: -1, label: `Tabs ${((page - 1 + pages) % pages) + 1}/${pages}` }, // W
      ...slice.slice(5),
    ]
    return { def, segments, pages }
  }

  /** The Favorites wheel, from the session's starred-pages snapshot. Symbol
   *  favorites become TICKER segments, so the whole quote pipeline — price/%
   *  display, day-direction colors, frozen-while-open — applies unchanged;
   *  page/web favorites become link segments. Paginates past 8 the way the
   *  tabs wheel does, with the Main nav on every page. */
  private favoriteSegments(
    doc: WheelDoc, favs: FavoriteRow[] | null, favPage: number
  ): { segments: Segment[]; pages: number } {
    const mainNav: Segment = {
      type: 'wheel', wheel: 'main', label: 'Main',
      symbol: doc.wheels.find((w) => w.id === 'main')?.symbol,
    }
    if (favs === null) {
      // The favorites fetch failed while the wheels doc loaded — say so
      // rather than passing the outage off as "no favorites".
      return {
        segments: [
          { type: 'placeholder', label: 'Favorites unavailable', disabled: true },
          mainNav,
        ],
        pages: 1,
      }
    }
    if (favs.length === 0) {
      return {
        segments: [
          { type: 'placeholder', label: 'No favorites yet — star a page', disabled: true },
          mainNav,
        ],
        pages: 1,
      }
    }
    const favSeg = (f: FavoriteRow): Segment => f.kind === 'symbol'
      ? { type: 'ticker', ticker: f.key, label: f.key }
      : {
          type: 'link', address: f.key, label: f.label.slice(0, 14),
          // Only web favorites carry a captured tab image; page glyphs are
          // the renderer's own.
          ...(f.kind === 'web' && f.icon ? { icon: f.icon } : {}),
        }
    const all = favs.map(favSeg)
    if (all.length <= 8) {
      return { segments: [...all, mainNav], pages: 1 }
    }
    const pages = Math.ceil(all.length / 6)
    const page = ((favPage % pages) + pages) % pages
    const slice = all.slice(page * 6, page * 6 + 6)
    const segments: Segment[] = [
      ...slice.slice(0, 2),
      { type: 'page', dir: +1, label: `Favorites ${((page + 1) % pages) + 1}/${pages}` }, // E
      ...slice.slice(2, 5),
      { type: 'page', dir: -1, label: `Favorites ${((page - 1 + pages) % pages) + 1}/${pages}` }, // W
      ...slice.slice(5),
      mainNav,
    ]
    return { segments, pages }
  }

  /** Decorate STATIC-wheel chart segments whose meaning depends on live
   *  chart state (vis:* / tf:* / isolate — users can also place these on
   *  custom wheels via the catalog). With ctx, vis:* labels carry the
   *  current visibility so the segment reads as the toggle it is. With NO
   *  ctx the segments are DISABLED, labels left plain: the chart-ind review
   *  lesson — a state-claiming action that cannot land must not look live. */
  private decorateChartState(seg: Segment, ctx: WheelCtx | null): Segment {
    if (seg.type !== 'chart' || !seg.tool) return seg
    const stateful =
      seg.tool === 'vis:draw' || seg.tool === 'vis:ind' ||
      seg.tool === 'isolate' || seg.tool.startsWith('tf:')
    if (!stateful) return seg
    if (ctx === null) return { ...seg, disabled: true }
    const hiddenFlag =
      seg.tool === 'vis:draw' ? 'drawhidden' : seg.tool === 'vis:ind' ? 'indhidden' : null
    if (hiddenFlag && ctx.flags.includes(hiddenFlag)) {
      // Append, never replace — the base label may be the user's own edit.
      return { ...seg, label: `${seg.label} ◐ hidden` }
    }
    return seg
  }

  /** The chart-* dynamic wheels, built from the session's spawn ctx plus the
   *  open tabs. A null ctx (the user navigated here without a chart under
   *  the spawning click) builds the honest empty state — placeholders and
   *  all-off markers, never a guess about some chart somewhere. */
  /**
   * Can this wheel do anything useful over a chart?
   *
   * A locked chart-draw or chart-measure can — its segments ARE chart actions,
   * and honouring the lock there is the whole point. A locked Favorites cannot,
   * and honouring it there would strand the chart hub: the main wheel's
   * defaults carry no navigation to 'chart', so the only way back would be to
   * unlock and respawn. So context still wins for wheels that have nothing to
   * say about a chart.
   */
  private usableOverClass(doc: WheelDoc, id: string, classWheel: string): boolean {
    const w = doc.wheels.find((x) => x.id === id)
    if (!w) return false
    // The bound wheel itself, and its own dynamic children, always qualify.
    if (w.id === classWheel || (w.dynamic ?? '').startsWith(`${classWheel}-`)) return true
    // Otherwise the locked wheel must offer a way BACK to the bound one —
    // either by acting on that class directly or by naming it as a segment.
    return w.segments.some(
      (s) => s.type === classWheel || (s.type === 'wheel' && s.wheel === classWheel)
    )
  }

  private chartSegments(kind: string, doc: WheelDoc, ctx: WheelCtx | null): Segment[] {
    const symbols = ctx?.symbols ?? []
    const indicators = ctx?.indicators ?? []
    const hidden = ctx?.hidden ?? []
    let body: Segment[]
    if (kind === 'chart-add') {
      if (ctx === null) {
        // No chart under the spawn = nowhere for "Add SYM" to land.
        body = [{ type: 'placeholder', label: 'Right-click a chart', disabled: true }]
      } else {
        const have = new Set(symbols)
        const open = this.tabs.symbolTabs().filter((t) => !have.has(t.symbol))
        // 11 + the back segment = the 12-segment wheel cap; tabs past that
        // are simply not offered (no pagination here — open fewer tabs).
        body = open.slice(0, 11).map((t) => ({
          type: 'chart' as const, tool: 'add', ticker: t.symbol, label: `Add ${t.symbol}`,
        }))
        if (body.length === 0) {
          body = [{ type: 'placeholder', label: 'No open ticker tabs', disabled: true }]
        }
      }
    } else if (kind === 'chart-ind') {
      // Same five keys the Chart component computes (IndicatorKey) and
      // CHART_TOOLS validates — ctx.indicators carries the bare keys.
      //
      // REVIEW 2026-08-02: with NO ctx (spawned off-chart, e.g. via a locked
      // dynamic wheel) the markers showed every indicator as '○ off' while
      // the segments still fired TOGGLES — clicking '○ Volume' turned the
      // page's on-by-default Volume OFF, the opposite of what it advertised.
      // No context = no claims and no actions, like the other empty states.
      if (ctx === null) {
        body = [{ type: 'placeholder', label: 'Right-click a chart', disabled: true }]
      } else {
        const IND: [string, string][] = [
          ['vol', 'Volume'], ['sma20', 'SMA 20'], ['sma50', 'SMA 50'],
          ['ema20', 'EMA 20'], ['rsi14', 'RSI 14'],
        ]
        body = IND.map(([key, name]) => ({
          type: 'chart' as const,
          tool: `ind:${key}`,
          label: `${indicators.includes(key) ? '●' : '○'} ${name}`,
        }))
        // v3: the period editor rides this wheel — after the toggles,
        // before the back-nav.
        body.push({ type: 'chart', tool: 'settings', label: 'Settings…' })
      }
    } else if (kind === 'chart-tf') {
      // The timeframes, current one marked. tf: is a SWITCH, not a toggle —
      // clicking the marked one is a no-op on the page, which is fine.
      body = ctx === null
        ? [{ type: 'placeholder', label: 'Right-click a chart', disabled: true }]
        : TIMEFRAMES.map((t) => ({
            type: 'chart' as const,
            tool: `tf:${t}`,
            label: `${ctx.timeframe === t ? '● ' : ''}${t}`,
          }))
    } else {
      // chart-tickers v3: ONE node owns the symbol set — hide/show toggles,
      // isolation, and the nav to Add symbol (the chart wheel's NW points
      // here now). Hidden symbols stay listed — clicking one shows it again.
      if (ctx === null) {
        body = [{ type: 'placeholder', label: 'Right-click a chart', disabled: true }]
      } else {
        // Isolation leads when active (the way OUT must be the first thing
        // seen); offered last when inactive and there is something to solo.
        const isolateSeg: Segment | null = ctx.isolated !== null
          ? { type: 'chart', tool: 'isolate', label: `⦿ ${ctx.isolated} — off` }
          : symbols.length >= 2
            ? { type: 'chart', tool: 'isolate', label: 'Isolate top' }
            : null
        // 12-segment cap: symbols get the room left after [isolate?,
        // Add-nav, back]. Excess symbols are simply not offered.
        const room = 12 - 2 - (isolateSeg ? 1 : 0)
        body = symbols.length < 2
          ? [{ type: 'placeholder', label: 'Single-symbol chart', disabled: true }]
          : symbols.slice(0, room).map((sym) => ({
              type: 'chart' as const, tool: 'hide', ticker: sym,
              label: hidden.includes(sym) ? '◑ hidden' : '',
            }))
        if (isolateSeg) {
          if (ctx.isolated !== null) body.unshift(isolateSeg)
          else body.push(isolateSeg)
        }
        body.push({
          type: 'wheel', wheel: 'chart-add', label: 'Add symbol',
          symbol: doc.wheels.find((w) => w.id === 'chart-add')?.symbol,
        })
      }
    }
    body.push({
      type: 'wheel', wheel: 'chart', label: 'Chart',
      symbol: doc.wheels.find((w) => w.id === 'chart')?.symbol,
    })
    return body
  }

  // ------------------------------------------------------------ session ops
  private clamp(winId: number, x: number, y: number): { x: number; y: number } {
    const size = this.tabs.windowContentSize(winId)
    if (!size) return { x, y }
    const r = WHEEL_RADIUS + EDGE_MARGIN
    const topMin = this.tabs.isLocked ? r : TABBAR_H + NAVBAR_H + 4 + r
    return {
      x: Math.max(r, Math.min(size.width - r, x)),
      y: Math.max(Math.min(topMin, size.height - r), Math.min(size.height - r, y)),
    }
  }

  private async spawn(
    winId: number, x: number, y: number,
    ctx: WheelCtx | null, originTabId: number | null, originWcId: number | null
  ): Promise<void> {
    const seq = ++this.spawnSeq
    this.releaseDuringSpawn = false
    // Favorites ride along with the doc fetch (both loopback GETs) so the
    // Favorites wheel materializes synchronously mid-session, like tabs.
    const [doc, favorites] = await Promise.all([this.loadDoc(), this.loadFavorites()])
    if (seq !== this.spawnSeq) return // a newer press superseded this spawn
    if (!doc) {
      log('wheel: no config (backend down or locked) — not spawning')
      return
    }
    // A dead session from a race (window closed mid-await) must not leak;
    // an open launcher yields the overlay the same way.
    if (this.session) this.despawn()
    this.closeLauncher()

    const view = this.ensureOverlay(this.preloadPath)
    if (!this.tabs.attachOverlay(winId, view)) return

    // An explicit LOCK is a user decision and outranks chart context — but
    // only when the locked wheel can actually act on a chart. Context used to
    // win unconditionally, which meant locking the Draw wheel and then
    // right-clicking the very chart you were drawing on silently swapped it
    // for the chart hub: the lock appeared to work everywhere EXCEPT the one
    // surface it exists for.
    // The binding comes from config.class_wheels (DX-15), which ships with
    // exactly one entry — chart -> chart, the behaviour that was hardcoded
    // here before. A doc is user data, so the bound wheel is checked to
    // exist rather than assumed.
    const locked = doc.config.locked
    const bound = ctx?.context ? (doc.config.class_wheels?.[ctx.context] ?? null) : null
    const classWheel = bound !== null && doc.wheels.some((w) => w.id === bound) ? bound : null
    const wheelId =
      locked !== null && (classWheel === null || this.usableOverClass(doc, locked, classWheel))
        ? locked
        : classWheel !== null
          ? classWheel
          : 'main'
    // The pad snapshot the predictions are resolved from. Awaited BEFORE
    // materialize, so the face the user sees and the action a release fires
    // come from one snapshot — a live re-read mid-gesture is exactly the
    // stale-hover race this wheel already refuses to have.
    await this.refreshPadHint()
    const { def, segments } = this.materialize(doc, wheelId, 0, ctx, favorites)
    const center = this.clamp(winId, x, y)

    const win = this.tabs.baseWindow(winId)
    const onBlurOrResize = () => this.despawn()
    win?.on('blur', onBlurOrResize)
    win?.on('resize', onBlurOrResize)

    const sess: Session = {
      winId,
      mode: 'pending',
      center,
      start: { x, y },
      moved: false,
      wheelId: def.id,
      dynPage: 0,
      lastPointer: { x, y },
      ctx,
      favorites,
      originTabId,
      originWcId,
      doc,
      segments,
      detachListeners: () => {
        win?.off('blur', onBlurOrResize)
        win?.off('resize', onBlurOrResize)
      },
    }
    this.session = sess
    this.push('wheel:spawn', {
      center,
      mode: 'pending',
      wheel: { id: def.id, name: def.name, symbol: def.symbol, segments },
      config: doc.config,
    })
    this.fetchQuotes(segments, doc.config)

    if (this.releaseDuringSpawn) {
      // The button came back up while we were fetching config: that was a
      // click. Land in click mode instead of a pending state nobody holds.
      this.releaseDuringSpawn = false
      sess.mode = 'click'
      this.push('wheel:mode', 'click')
      this.overlay?.webContents.focus()
    }
  }

  private switchWheel(wheelId: string, dynPage = 0): void {
    const s = this.session
    if (!s) return
    const { def, segments } = this.materialize(s.doc, wheelId, dynPage, s.ctx, s.favorites)
    s.wheelId = def.id
    s.dynPage = dynPage
    s.segments = segments
    this.push('wheel:update', {
      wheel: { id: def.id, name: def.name, symbol: def.symbol, segments },
      config: s.doc.config,
    })
    this.fetchQuotes(segments, s.doc.config)
  }

  private despawn(): void {
    const s = this.session
    if (!s) return
    this.session = null
    s.detachListeners()
    this.push('wheel:despawn', null)
    if (this.overlay) this.tabs.detachOverlay(s.winId, this.overlay)
  }

  private push(channel: string, payload: unknown): void {
    if (this.overlay && !this.overlay.webContents.isDestroyed()) {
      this.overlay.webContents.send(channel, payload)
    }
  }

  // ---------------------------------------------------------- the launcher
  /** Open the apps launcher in a window: the same overlay attach/ready-replay
   *  machinery a wheel spawn uses, pushing a launcher payload instead of a
   *  wheel. The payload rides the wheel channels ('wheel:spawn'/'despawn') —
   *  the overlay's preload surface is fixed, and a parallel channel pair
   *  would duplicate the exact same lifecycle. */
  private async openLauncher(winId: number): Promise<void> {
    const seq = ++this.launcherSeq
    const res = await mainRequest<{ pages: LauncherPage[] }>('GET', '/api/pages')
    if (seq !== this.launcherSeq) return // a newer toggle superseded this open
    const pages = res.body?.pages
    if (res.status !== 200 || !Array.isArray(pages)) {
      log('launcher: no page registry (backend down or locked) — not opening')
      return
    }
    // One overlay: whatever else holds it lets go first.
    if (this.session) this.despawn()
    this.closeLauncher()

    const view = this.ensureOverlay(this.preloadPath)
    if (!this.tabs.attachOverlay(winId, view)) return
    const win = this.tabs.baseWindow(winId)
    const onBlurOrResize = () => this.closeLauncher()
    win?.on('blur', onBlurOrResize)
    win?.on('resize', onBlurOrResize)
    this.launcherSession = {
      winId,
      pages,
      detachListeners: () => {
        win?.off('blur', onBlurOrResize)
        win?.off('resize', onBlurOrResize)
      },
    }
    this.pushLauncher()
    view.webContents.focus() // Escape must close it, like a click-mode wheel
  }

  private pushLauncher(): void {
    const l = this.launcherSession
    if (!l) return
    // `top` is where the chrome ends — the panel anchors just below the
    // navbar's apps button, and only main knows the chrome heights.
    this.push('wheel:spawn', {
      launcher: { pages: l.pages, top: TABBAR_H + NAVBAR_H },
    })
  }

  private closeLauncher(): void {
    const l = this.launcherSession
    if (!l) return
    this.launcherSession = null
    l.detachListeners()
    this.push('wheel:despawn', null)
    if (this.overlay) this.tabs.detachOverlay(l.winId, this.overlay)
  }

  /** A launcher tile pick, by index into the pages pushed to the overlay.
   *  Ready is re-checked HERE (defense in depth): the overlay renders
   *  not-ready tiles inert, but main is the authority on what opens. */
  private launcherPick(index: number): void {
    const l = this.launcherSession
    if (!l) return
    const page = l.pages[index]
    if (!page || !page.ready) return
    // Every registry key is a page address ('home' -> home.gs, which
    // openAddress routes to idle the same way the omnibox does).
    this.tabs.openAddress(l.winId, `${page.key}.gs`)
    this.closeLauncher()
  }

  /** Ticker segments show a price/% snapshot taken at spawn. Deliberately
   *  fetched ONCE — the spec forbids colors flashing while the wheel is up;
   *  close and reopen to refresh. */
  private async fetchQuotes(segments: Segment[], _config: WheelDoc['config']): Promise<void> {
    const syms = [...new Set(segments.filter((s) => s.type === 'ticker' && s.ticker)
      .map((s) => s.ticker!))]
    if (syms.length === 0) return
    const mySession = this.session
    const res = await mainRequest<{ quotes: Record<string, unknown> }>(
      'GET', `/api/quotes?symbols=${encodeURIComponent(syms.join(','))}`)
    // Only deliver to the wheel that asked. Session identity is not enough:
    // paging the Favorites wheel (>8 stars) switches segments WITHIN one
    // session, so page 1's slower answer could paint page 2's symbols.
    // switchWheel replaces s.segments, making it a free generation token.
    if (res.status === 200 && res.body && this.session === mySession
        && mySession?.segments === segments) {
      this.push('wheel:quotes', res.body.quotes)
    }
  }

  // ---------------------------------------------------------------- actions
  /** Act on a segment. Returns 'stay' if the wheel remains open.
   *
   *  `intent` is the wheel's third classification axis (spawn context =
   *  WHERE, segment = WHAT, button = HOW): 'primary' is the deliberate
   *  variant, 'quick' the immediate one. A hold-release is a right-button
   *  gesture and therefore 'quick'; in click mode the button chooses. Tools
   *  with one behaviour ignore it — the axis costs nothing until a tool
   *  declares both. */
  private act(index: number | null,
              intent: 'primary' | 'quick' = 'primary'): 'stay' | 'close' {
    const s = this.session
    if (!s) return 'close'
    const seg = index !== null ? s.segments[index] : undefined
    if (!seg || seg.disabled) return 'close'
    // THE PREDICTION FIRES ON A RIGHT-CLICK IN CLICK MODE, and nowhere else.
    //
    // 'quick' alone is NOT the right condition, and reading it as such broke
    // the wheel's core gesture. A hold-release flick is also dispatched as
    // 'quick' (see the wheel:evt 'up' handler) -- and the flick is how you
    // NAVIGATE: hold, sweep onto Tabs, release, and the tabs wheel opens.
    // Predicting there swallowed that gesture whole; flicking onto Tabs
    // closed the wheel and did nothing anyone asked for.
    //
    // DX-14 settles it on its own terms. The prediction must be VISIBLE
    // before commit, and its hint is only readable when the face is sitting
    // still in click mode -- mid-flick nobody is reading anything. So the
    // shortcut belongs to the deliberate right-click on a face you can see,
    // and every gesture a user already learned keeps its declared behaviour.
    if (intent === 'quick' && s.mode === 'click'
        && seg.hintKind === 'address' && seg.hintArg) {
      this.tabs.openAddress(s.winId, seg.hintArg)
      return 'close'
    }
    switch (seg.type) {
      case 'wheel':
        this.switchWheel(seg.wheel ?? 'main')
        return 'stay'
      case 'page':
        // Page navs paginate the wheel they sit on (tabs or favorites).
        this.switchWheel(s.wheelId, s.dynPage + (seg.dir ?? 1))
        return 'stay'
      case 'nav':
        this.tabs.gotoRoute(s.winId, seg.route ?? 'idle')
        return 'close'
      case 'tool':
        if (seg.tool === 'search') this.tabs.focusOmnibox(s.winId)
        return 'close'
      case 'ticker':
        if (seg.ticker) this.tabs.openTicker(s.winId, seg.ticker)
        return 'close'
      case 'link':
        if (seg.address) this.tabs.openAddress(s.winId, seg.address)
        return 'close'
      case 'tab':
        if (seg.tabId !== undefined) this.tabs.activateTabGlobal(seg.tabId)
        return 'close'
      case 'chart':
        this.sendChartAction(seg)
        return 'close'
      case 'data':
        // Post's PRIMARY intent is the deliberate variant: the picker wheel,
        // "Open notepad" pinned top, entries below (DX-6). Everything else --
        // get, quick post, a picker choice -- delivers to the origin view.
        if (seg.tool === 'data:post' && intent === 'primary' && !seg.entryId) {
          void this.openPostPicker()
          return 'stay'
        }
        this.sendDataAction(seg, intent)
        return 'close'
      default:
        return 'close'
    }
  }

  /** Deliver a chart segment to the view the wheel was spawned over. The
   *  PAGE decides what the tool means — add, hide, the ind: toggles,
   *  normalize, pointer, trend, hline, clear all flow the same way; views
   *  without a chart handler simply ignore the event. If the origin tab
   *  closed while the wheel was up, DROP the action: it must never land on
   *  whatever view exists now. */
  private sendChartAction(seg: Segment): void {
    const s = this.session
    if (!s || !seg.tool || s.originTabId === null || s.originWcId === null) return
    if (!this.tabs.allTabs().some((t) => t.id === s.originTabId)) return
    const wc = webContents.fromId(s.originWcId)
    if (!wc || wc.isDestroyed()) return
    wc.send('chart:action', seg.ticker
      ? { tool: seg.tool, symbol: seg.ticker }
      : { tool: seg.tool })
  }

  /** Deliver a data segment to the view the wheel was spawned over — the
   *  same origin discipline as sendChartAction — plus the two things chart
   *  actions deliberately drop, because Get/Post need them:
   *
   *  - the SPAWN COORDINATES, so the page can resolve which enrolled element
   *    sat under the right-click (chart actions act on selection instead);
   *  - the INTENT, so Post can distinguish the picker (primary) from
   *    quick-post-most-recent-compatible (quick).
   */
  private sendDataAction(seg: Segment, intent: 'primary' | 'quick'): void {
    const s = this.session
    if (!s || !seg.tool || s.originTabId === null || s.originWcId === null) return
    if (!this.tabs.allTabs().some((t) => t.id === s.originTabId)) return
    const wc = webContents.fromId(s.originWcId)
    if (!wc || wc.isDestroyed()) return
    wc.send('data:action', {
      tool: seg.tool,
      intent,
      spawn: { x: s.start.x, y: s.start.y },
      ...(seg.entryId ? { entryId: seg.entryId } : {}),
    })
  }

  /** Attach the predicted quick intent to a segment, if it has one.
   *
   *  The table lives in predictCore.ts (import-free, so the gate can run the
   *  real priority order under node). This method's only job is to name the
   *  tool the way the table is keyed and to copy the answer onto the segment
   *  — the prediction itself is resolved ONCE at spawn from the frozen ctx,
   *  so it cannot flicker to a different intent mid-gesture.
   *
   *  Nothing here changes the LAYOUT. A prediction only ever adds a hint to
   *  the segment already at that angle. */
  private predictFor(seg: Segment, ctx: WheelCtx | null): Segment {
    const tool =
      seg.type === 'wheel' ? `wheel:${seg.wheel}`
      : seg.type === 'tool' ? `tool:${seg.tool}`
      : seg.type === 'data' ? `data:${seg.tool}`
      : seg.type === 'nav'  ? `nav:${seg.route}`
      : ''
    if (!tool) return seg
    // The open-tab veto (DX-16). Reaching a tab you already have is what
    // ordinary tab navigation is FOR, so a prediction that offers one has
    // spent the gesture on nothing. Answered from the real tab list, at
    // spawn, alongside every other frozen input.
    const p = predictBest(tool, ctx, this.padHint, (addr) => this.addressIsOpen(addr))
    return p ? { ...seg, hint: p.hint, hintKind: p.kind, hintArg: p.arg } : seg
  }

  /** Is this .gs address already on screen — the page under the wheel, or a
   *  tab in any window? Compared as ADDRESSES, which is why the Opt page had
   *  to start reporting its symbol: every Opt tab used to answer 'opt.gs',
   *  so they were all indistinguishable and this could never be right. */
  private addressIsOpen(address: string): boolean {
    const want = address.trim().toLowerCase()
    if (!want) return false
    return this.tabs.allTabs().some(
      (t) => t.kind === 'app' && (t.address ?? '').toLowerCase() === want
    )
  }

  /** The newest notepad entry that carries a routable address, refreshed
   *  when the wheel spawns. Cached because prediction must be synchronous --
   *  the face is drawn from the same snapshot the release acts on. */
  private padHint: PadHint | null = null

  private async refreshPadHint(): Promise<void> {
    try {
      // The PICKER's endpoint, deliberately: it applies the one default-label
      // rule, so the hint and the picker segment for the same entry always
      // read the same. It carries no payloads, only what a wheel needs.
      const r = await mainRequest<
        Array<{ label: string; address?: string; destination?: string }>
      >('GET', '/api/notepad/summaries')
      const rows = r.status === 200 && Array.isArray(r.body) ? r.body : []
      // Newest first (notepad.py orders added_at DESC), and only entries whose
      // provenance is actually routable — reconstructing an address from a
      // symbol would drop every non-symbol source and produce a string
      // openAddress silently refuses.
      // Routable means EITHER endpoint routes: a payload whose kind has a
      // natural home is useful even if its provenance was never recorded.
      const routable = (v?: string): boolean => /\.gs(\?|$)/i.test(v ?? '')
      const hit = rows.find((e) => routable(e.destination) || routable(e.address))
      this.padHint = hit
        ? {
            label: hit.label,
            address: routable(hit.address) ? hit.address! : '',
            destination: routable(hit.destination) ? hit.destination : undefined,
          }
        : null
    } catch {
      this.padHint = null
    }
  }

  /** Main's mirror of the renderer's ACCEPTS registry, for greying picker
   *  entries by the SPAWN CONTEXT class. A static copy, not an import -- main
   *  and renderer are different bundles -- so the gate pins the two against
   *  each other by source. */
  private static DATA_ACCEPTS: Record<string, string[]> = {
    chart: ['contract', 'chain', 'drawing', 'chart-doc'],
    'backtest-form': ['contract', 'chain', 'backtest-spec'],
  }

  /** The post picker: a TRANSIENT wheel built from notepad summaries. Its
   *  segments carry entry ids only -- payloads stay in the pad. Compatible
   *  entries are lit, incompatible greyed; the top segment is always the
   *  notepad itself. */
  private async openPostPicker(): Promise<void> {
    const s = this.session
    if (!s) return
    let summaries: Array<{ id: string; kind: string; label: string }> = []
    try {
      const r = await mainRequest<Array<{ id: string; kind: string; label: string }>>(
        'GET', '/api/notepad/summaries')
      if (r.status === 200 && Array.isArray(r.body)) summaries = r.body
    } catch {
      /* an unreachable pad shows as empty, which the wheel says honestly */
    }
    if (this.session !== s) return // the wheel closed while we fetched
    const cls = s.ctx?.context ?? ''
    const ok = new Set(WheelManager.DATA_ACCEPTS[cls] ?? [])
    const segs: Segment[] = [{
      // DX-6: the top segment is ALWAYS the notepad -- the way to see and
      // edit what you hold, at the moment you are choosing what to post.
      type: 'link', label: 'Notepad', address: 'notepad.gs',
    }]
    for (const e of summaries.slice(0, 11)) {
      segs.push({
        type: 'data', tool: 'data:apply', entryId: e.id,
        label: e.label || e.kind,
        // No class under the spawn -> nothing is compatible; every entry
        // greys and the picker is a read-only view of what you hold.
        disabled: !ok.has(e.kind),
      })
    }
    if (summaries.length === 0) {
      segs.push({ type: 'placeholder', label: 'notepad is empty', disabled: true })
    }
    s.segments = segs
    this.push('wheel:update', {
      wheel: { id: '__datapad', name: 'Post', symbol: '\ud83d\udccb', segments: segs },
      config: s.doc.config,
    })
  }

  private async toggleLock(): Promise<void> {
    const s = this.session
    if (!s) return
    const doc = s.doc
    doc.config.locked = doc.config.locked === s.wheelId ? null : s.wheelId
    const res = await mainRequest('PUT', '/api/wheels', doc)
    if (res.status !== 200) {
      log('wheel: lock persist failed', res.status)
      return
    }
    this.push('wheel:update', {
      wheel: {
        id: s.wheelId,
        name: doc.wheels.find((w) => w.id === s.wheelId)?.name ?? s.wheelId,
        symbol: doc.wheels.find((w) => w.id === s.wheelId)?.symbol ?? '',
        segments: s.segments,
      },
      config: doc.config,
    })
  }

  // -------------------------------------------------------------------- ipc
  private registerIpc(): void {
    // Right-button stream, from ANY view: app pages and chrome via the
    // bridge, browser pages via the minimal preload, and the overlay itself
    // (during a hold, Chromium routes events to whoever has capture — we
    // listen everywhere so it does not matter who that is).
    ipcMain.on('wheel:evt', (e, kind: string, cx: number, cy: number, rawCtx?: unknown) => {
      if (this.tabs.isLocked || !isSignedIn()) return
      if (typeof cx !== 'number' || typeof cy !== 'number') return

      const fromOverlay = this.overlay?.webContents.id === e.sender.id
      let winId: number
      let x = cx
      let y = cy
      let originTabId: number | null = null
      if (fromOverlay) {
        if (!this.session) return
        winId = this.session.winId
      } else {
        const src = this.tabs.resolveSender(e.sender)
        if (!src) return
        winId = src.winId
        x = cx + src.offsetX
        y = cy + src.offsetY
        originTabId = src.tabId
      }

      const s = this.session
      if (kind === 'down') {
        // A fresh right-press while a wheel is up: reposition if it is this
        // window's click-mode wheel; otherwise start over where the user is.
        if (s && s.mode === 'click' && s.winId === winId) {
          s.center = this.clamp(winId, x, y)
          this.push('wheel:update', { center: s.center })
          return
        }
        if (s) this.despawn()
        // ctx rides only on 'down' (wheelEvents.ts); the overlay never
        // sends downs, so a non-overlay sender is the true origin view.
        void this.spawn(winId, x, y, sanitizeCtx(rawCtx),
                        originTabId, fromOverlay ? null : e.sender.id)
        return
      }
      if (!s) {
        // No session yet: a spawn may be mid-fetch — remember the release.
        if (kind === 'up') this.releaseDuringSpawn = true
        return
      }
      if (s.winId !== winId) return

      if (kind === 'move') {
        if (s.mode === 'click') return // click mode: the overlay owns hover
        if (!s.moved
            && Math.hypot(x - s.start.x, y - s.start.y) >= CLICK_MOVE_THRESHOLD) {
          s.moved = true
          s.mode = 'hold'
          this.push('wheel:mode', 'hold')
        }
        if (s.mode === 'hold') {
          s.lastPointer = { x, y }
          if (this.overlay && !this.overlay.webContents.isDestroyed()) {
            this.overlay.webContents.send('wheel:pointer', x, y)
          }
        }
        return
      }
      if (kind === 'up') {
        if (s.mode === 'pending') {
          // No travel: this was a CLICK. The wheel stays, grows its lock
          // hub, and left clicks take over.
          s.mode = 'click'
          this.push('wheel:mode', 'click')
          this.overlay?.webContents.focus() // so Escape closes it
          return
        }
        if (s.mode === 'hold') {
          const index = segmentAt(s.center.x, s.center.y,
                                  s.lastPointer.x, s.lastPointer.y,
                                  s.segments.length)
          // A hold-release IS a right-button gesture: the flick maps to
          // the quick intent by the one grammar (left deliberate, right
          // quick). The deliberate variant lives in click mode's left click.
          const result = this.act(index, 'quick')
          if (result === 'stay') {
            // Spec: after a wheel-nav during a hold, enter left-click mode —
            // the freshly opened wheel must not vanish under the cursor.
            s.mode = 'click'
            this.push('wheel:mode', 'click')
            this.overlay?.webContents.focus()
          } else {
            this.despawn()
          }
        }
      }
    })
    // The chrome's apps button. Toggle: an overlay already up in this window
    // closes; otherwise the launcher opens here (closing whichever window's
    // overlay held the view — there is only one).
    ipcMain.on('launcher:toggle', (e) => {
      if (this.tabs.isLocked || !isSignedIn()) return
      const winId = this.tabs.winIdFromSender(e.sender)
      if (winId === null) return
      if (this.launcherSession?.winId === winId) {
        this.closeLauncher()
        return
      }
      if (this.session?.winId === winId) {
        this.despawn()
        return
      }
      void this.openLauncher(winId)
    })
    ipcMain.on('wheelui:ready', (e) => {
      if (this.overlay?.webContents.id !== e.sender.id) return
      // The first spawn of an app run raced the overlay's page load and its
      // payload evaporated — replay the LIVE session state now that someone
      // is listening (the launcher replays the same way). Nothing live =
      // nothing to replay.
      if (this.launcherSession) {
        this.pushLauncher()
        this.overlay?.webContents.focus()
        return
      }
      const s = this.session
      if (!s) return
      const def = s.doc.wheels.find((w) => w.id === s.wheelId)
      this.push('wheel:spawn', {
        center: s.center,
        mode: s.mode,
        wheel: {
          id: s.wheelId,
          name: def?.name ?? s.wheelId,
          symbol: def?.symbol ?? '',
          segments: s.segments,
        },
        config: s.doc.config,
      })
      this.fetchQuotes(s.segments, s.doc.config)
      if (s.mode === 'click') this.overlay?.webContents.focus()
    })
    ipcMain.on('wheelui:act', (e, index: number, button?: string) => {
      if (this.overlay?.webContents.id !== e.sender.id) return
      // Launcher mode: the index picks a page tile, not a wheel segment.
      if (this.launcherSession) {
        this.launcherPick(typeof index === 'number' ? index : -1)
        return
      }
      if (!this.session || this.session.mode !== 'click') return
      // The button is the intent axis: left = deliberate, right = quick.
      // Untrusted input from the overlay — anything not exactly 'right'
      // is primary, so a garbled value degrades to the safe variant.
      const intent = button === 'right' ? 'quick' : 'primary'
      if (this.act(typeof index === 'number' ? index : null, intent) === 'close') this.despawn()
    })
    ipcMain.on('wheelui:lock', (e) => {
      if (this.overlay?.webContents.id !== e.sender.id) return
      if (this.session?.mode === 'click') void this.toggleLock()
    })
    ipcMain.on('wheelui:close', (e) => {
      if (this.overlay?.webContents.id !== e.sender.id) return
      // Outside-click and Escape, for whichever occupant the overlay has.
      this.despawn()
      this.closeLauncher()
    })
    ipcMain.on('wheelui:move', (e, x: number, y: number) => {
      if (this.overlay?.webContents.id !== e.sender.id) return
      const s = this.session
      if (!s || s.mode !== 'click') return
      if (typeof x !== 'number' || typeof y !== 'number') return
      s.center = this.clamp(s.winId, x, y)
      this.push('wheel:update', { center: s.center })
    })
  }
}
