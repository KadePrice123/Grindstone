/**
 * The radial face of a gesture wheel — used at full size by the overlay and
 * at preview size by the settings editor, so what you edit is what you get.
 * Geometry comes from src/shared/wheelGeometry.ts, the same module the main
 * process selects segments with.
 */
import {
  HUB_RADIUS,
  WHEEL_RADIUS,
  sectorAnchor,
  sectorPath,
} from '../../../shared/wheelGeometry'

export interface WheelSegment {
  type: 'wheel' | 'nav' | 'tool' | 'ticker' | 'chart' | 'placeholder' | 'empty' | 'tab' | 'page'
  label: string
  wheel?: string
  route?: string
  tool?: string
  ticker?: string
  tabId?: number
  dir?: number
  icon?: string
  symbol?: string
  disabled?: boolean
}

export interface WheelRender {
  id: string
  name: string
  symbol: string
  segments: WheelSegment[]
}

export interface WheelConfig {
  ticker_display: 'percent' | 'price'
  ticker_colors: boolean
  locked: string | null
}

export interface Quote {
  available: boolean
  price: number | null
  change_pct: number | null
}

/** A tiny glyph per segment type — text, so the SVG needs no icon sprite. */
function glyph(seg: WheelSegment): string {
  switch (seg.type) {
    case 'wheel':
      return seg.symbol ?? '◎'
    case 'nav':
      return { idle: '⌂', accounts: '⛁', data: '▤', settings: '⚙', news: '☰', charts: '📈' }[
        seg.route ?? ''
      ] ?? '▢'
    case 'tool':
      return seg.tool === 'search' ? '🔍' : '✦'
    case 'ticker':
      return '' // the ticker text IS the identity
    case 'chart': {
      const t = seg.tool ?? ''
      if (t.startsWith('ind:')) return '∿'
      if (t.startsWith('vis:')) return '◐'
      if (t.startsWith('tf:')) return '⏱'
      return { pointer: '➤', trend: '╱', hline: '━', vline: '┃', circle: '◯',
               select: '⬚', delete: '⌫', trim: '✂', clear: '⌫',
               measure: '⤢', inspect: '🔍', clearmeasure: '⌦',
               normalize: '%', add: '+', hide: '◑',
               isolate: '⦿', settings: '⚙' }[t] ?? '✦'
    }
    case 'tab':
      return '▣'
    case 'page':
      return (seg.dir ?? 1) > 0 ? '⯈' : '⯇'
    default:
      return '·'
  }
}

function tickerSub(seg: WheelSegment, config: WheelConfig, quotes: Record<string, Quote>): {
  text: string
  cls: string
} {
  const q = seg.ticker ? quotes[seg.ticker] : undefined
  if (!q) return { text: '', cls: '' }
  if (!q.available || q.price == null) return { text: '—', cls: '' }
  const text =
    config.ticker_display === 'price'
      ? q.price.toLocaleString(undefined, { maximumFractionDigits: 2 })
      : q.change_pct == null
        ? '—'
        : `${q.change_pct >= 0 ? '+' : ''}${q.change_pct.toFixed(2)}%`
  // Direction color is decided ONCE per open (quotes arrive once) — the wheel
  // must not flash red/green while it is up.
  const cls =
    config.ticker_colors && q.change_pct != null
      ? q.change_pct >= 0
        ? 'wf-up'
        : 'wf-down'
      : ''
  return { text, cls }
}

export function WheelFace({
  wheel,
  config,
  quotes,
  hover,
  mode,
  scale = 1,
  animate = false,
  onSegment,
  onLock,
}: {
  wheel: WheelRender
  config: WheelConfig
  quotes: Record<string, Quote>
  hover: number | null
  /** click shows the lock hub; hold and preview do not */
  mode: 'pending' | 'hold' | 'click' | 'preview'
  scale?: number
  animate?: boolean
  onSegment?: (index: number) => void
  onLock?: () => void
}) {
  const n = Math.max(wheel.segments.length, 1)
  const R = WHEEL_RADIUS + 4
  const size = R * 2 * scale
  const locked = config.locked === wheel.id
  const showLock = mode === 'click'

  return (
    <svg
      className={`wheel-face${animate ? ' spawn' : ''}`}
      data-wheel={wheel.id}
      data-mode={mode}
      width={size}
      height={size}
      viewBox={`${-R} ${-R} ${R * 2} ${R * 2}`}
      role="menu"
      aria-label={`${wheel.name} wheel`}
    >
      {wheel.segments.map((seg, i) => {
        const anchor = sectorAnchor(i, n)
        const sub =
          seg.type === 'ticker' ? tickerSub(seg, config, quotes) : { text: '', cls: '' }
        const g = glyph(seg)
        // Chart add/hide segments carry a ticker: it renders bold like a
        // ticker segment (the symbol IS the identity), glyph above, the
        // state label ('Add SPY' / '◑ hidden') as the sub line.
        const tickerChart = seg.type === 'chart' && !!seg.ticker
        const cls = [
          'wf-seg',
          hover === i ? 'hover' : '',
          seg.disabled ? 'dead' : '',
          sub.cls,
        ].filter(Boolean).join(' ')
        return (
          <g
            key={i}
            className={cls}
            data-seg={i}
            style={animate ? { animationDelay: `${i * 18}ms` } : undefined}
            onMouseDown={(e) => {
              // Segment clicks act on mousedown-left in click mode; stop the
              // root's close-on-outside-click from also firing.
              if (e.button === 0 && onSegment && !seg.disabled) {
                e.stopPropagation()
                onSegment(i)
              }
            }}
          >
            <path className="wf-sector" d={sectorPath(i, n)} />
            {g ? (
              <text
                className="wf-glyph"
                x={anchor.x}
                y={anchor.y - (tickerChart ? 16 : seg.label ? 8 : -4)}
              >
                {g}
              </text>
            ) : null}
            {seg.type === 'ticker' ? (
              <>
                <text className="wf-label wf-ticker" x={anchor.x} y={anchor.y - 2}>
                  {seg.ticker}
                </text>
                <text className={`wf-sub ${sub.cls}`} x={anchor.x} y={anchor.y + 14}>
                  {sub.text}
                </text>
              </>
            ) : tickerChart ? (
              <>
                <text className="wf-label wf-ticker" x={anchor.x} y={anchor.y - 2}>
                  {seg.ticker}
                </text>
                {seg.label ? (
                  <text className="wf-sub" x={anchor.x} y={anchor.y + 14}>
                    {seg.label}
                  </text>
                ) : null}
              </>
            ) : (
              <text className="wf-label" x={anchor.x} y={anchor.y + (g ? 12 : 4)}>
                {seg.label}
              </text>
            )}
          </g>
        )
      })}

      <g
        className={`wf-hub${showLock ? ' lockable' : ''}${locked ? ' locked' : ''}`}
        onMouseDown={(e) => {
          if (e.button === 0 && showLock && onLock) {
            e.stopPropagation()
            onLock()
          }
        }}
      >
        <circle className="wf-hub-disc" r={HUB_RADIUS} />
        {showLock ? (
          <>
            {/* padlock: body + shackle; open vs closed mirrors the state */}
            <rect className="wf-lock-body" x={-9} y={-2} width={18} height={14} rx={2.5} />
            <path
              className="wf-lock-shackle"
              d={locked ? 'M -5 -2 v -4 a 5 5 0 0 1 10 0 v 4' : 'M -5 -2 v -4 a 5 5 0 0 1 10 0'}
              fill="none"
            />
            <text className="wf-hub-name" y={26}>
              {locked ? 'default' : 'set default'}
            </text>
          </>
        ) : (
          <text className="wf-hub-symbol" y={6}>
            {wheel.symbol}
          </text>
        )}
      </g>
    </svg>
  )
}
