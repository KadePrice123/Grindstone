/**
 * The live omnibox: debounced backend search, keyboard navigation, type
 * icons. Selection routing is the caller's job via onOpen — the omnibox
 * doesn't know what pages exist (page registry seam, REQUIREMENTS.md 6.11).
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { api, SearchResult } from '../api'
import { AccountsIcon, AiIcon, ApisIcon, ChartMiniIcon, NewsMiniIcon, PageMiniIcon, PositionsIcon } from './icons'

const DEBOUNCE_MS = 120

function RowIcon({ r }: { r: SearchResult }) {
  if (r.type === 'symbol') return <ChartMiniIcon />
  if (r.type === 'news') return <NewsMiniIcon />
  if (r.type === 'action') return <NewsMiniIcon />
  switch (r.page) {
    case 'accounts':
      return <AccountsIcon />
    case 'apis':
      return <ApisIcon />
    case 'ai':
      return <AiIcon />
    case 'positions':
      return <PositionsIcon />
    default:
      return <PageMiniIcon />
  }
}

export function Omnibox({
  onOpen,
  autoFocus,
}: {
  onOpen: (r: SearchResult) => void
  autoFocus?: boolean
}) {
  const [q, setQ] = useState('')
  const [results, setResults] = useState<SearchResult[]>([])
  const [notice, setNotice] = useState<string | null>(null)
  const [open, setOpen] = useState(false)
  const [sel, setSel] = useState(0)
  const timer = useRef<number | null>(null)
  const seq = useRef(0)
  const boxRef = useRef<HTMLDivElement>(null)

  // The dropdown must always answer a non-empty query — silence reads as
  // "search is broken" (it did, on a fresh install with no synced universe).
  const run = useCallback(async (text: string) => {
    const mine = ++seq.current
    if (!text.trim()) {
      setResults([])
      setNotice(null)
      setOpen(false)
      return
    }
    try {
      const res = await api<{ results: SearchResult[]; empty?: boolean }>(
        'GET',
        `/api/search?q=${encodeURIComponent(text)}`
      )
      if (mine !== seq.current) return // a newer keystroke superseded us
      setResults(res.results)
      setNotice(res.results.length === 0 ? `No matches for “${text.trim()}”` : null)
      setSel(0)
      setOpen(true)
    } catch (e) {
      if (mine !== seq.current) return
      setResults([])
      setNotice('Search is unavailable — backend error. Try again in a moment.')
      setOpen(true)
    }
  }, [])

  const onChange = (text: string) => {
    setQ(text)
    if (timer.current) window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => run(text), DEBOUNCE_MS)
  }

  const choose = (r: SearchResult | undefined) => {
    if (!r) return
    setOpen(false)
    onOpen(r)
  }

  const onKey = (e: React.KeyboardEvent) => {
    if (!open && e.key === 'Enter') {
      run(q)
      return
    }
    if (!open) return
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setSel((s) => Math.min(s + 1, results.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setSel((s) => Math.max(s - 1, 0))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      choose(results[sel])
    } else if (e.key === 'Escape') {
      setOpen(false)
    }
  }

  useEffect(() => {
    const away = (ev: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(ev.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', away)
    return () => {
      document.removeEventListener('mousedown', away)
      // A pending debounce firing after unmount would setState on a dead
      // component and waste a backend call.
      if (timer.current) window.clearTimeout(timer.current)
      seq.current += 1
    }
  }, [])

  return (
    <div className="omni-wrap" ref={boxRef}>
      <input
        className="omnibox"
        placeholder="Search tickers, news, pages…  (try: SPY news)"
        value={q}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={onKey}
        onFocus={() => results.length && setOpen(true)}
        autoFocus={autoFocus}
        spellCheck={false}
      />
      {open ? (
        <div className="omni-drop">
          {notice ? <div className="omni-notice">{notice}</div> : null}
          {results.map((r, i) => (
            <div
              key={`${r.type}:${r.symbol ?? r.id ?? r.page ?? r.action}:${i}`}
              className={`omni-row${i === sel ? ' selected' : ''}`}
              onMouseEnter={() => setSel(i)}
              onMouseDown={(e) => {
                e.preventDefault()
                choose(r)
              }}
            >
              <span className="omni-icon">
                <RowIcon r={r} />
              </span>
              <span className="omni-title">{r.title}</span>
              {r.subtitle ? <span className="omni-sub">{r.subtitle}</span> : null}
              {r.type === 'symbol' && r.asset_class && r.asset_class !== 'us_equity' ? (
                <span className="omni-tag">{r.asset_class}</span>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  )
}
