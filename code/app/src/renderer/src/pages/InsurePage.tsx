/** Insure — selling puts, priced like insurance (docs/INSURE.md).
 *
 *  No symbol argument: the favorites list IS the argument. The page mounts on
 *  /api/insure/status, then fans out ONE scan per favorite, staggered, so
 *  dots land symbol by symbol and a slow or failed symbol fails alone with
 *  its reason. The measured side (the archive) and the offered side (the live
 *  chain) degrade independently — the page stays useful with either half.
 *
 *  Every %/yr on this page derives client-side through optgrid's own
 *  annualise/tradingDaysTo (insurePoint), so this page, the heatmap and the
 *  Opt history chart can never quote two conventions.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import { InsureScatter, SYMBOL_INK, type InsureDot } from '../components/InsureScatter'
import { InsureIcon } from '../components/icons'
import {
  insurePoint, confidenceOf, requiredCreditPct,
  type MeasuredClass,
} from '../optgrid'

interface StatusResponse {
  available: boolean
  reason?: string
  archive?: {
    source: string
    built_at: string
    months: string
    underlyings: Record<string, { first: string; last: string }>
  }
  favorites: { symbol: string; in_archive: boolean }[]
  measuring: string[]
}

interface Candidate {
  occ: string | null
  expiration: string
  dte: number
  strike: number
  bid: number | null
  ask: number | null
  mid: number | null
  delta: number | null
  otm_pct: number
  offered_pct: number | null
  class: { mode: string; dte_band: number[]; band: number[] } | null
  class_mode: 'delta' | 'otm'
  measured: (MeasuredClass & { available?: boolean; reason?: string }) | null
  edge_pct: number | null
  tier: 'solid' | 'thin' | 'none' | 'unmeasured'
}

interface ScanResponse {
  symbol: string
  available: boolean
  reason?: string
  spot?: { price: number; date: string }
  chain?: { source: string; age_seconds?: number; reason?: string }
  expectancy: { status: string; computed_at?: string; reason?: string }
  /** The measuring year's own confession: its return and worst drawdown. */
  window_character?: {
    return_pct: number; max_drawdown_pct: number; first: string; last: string
  } | null
  candidates?: Candidate[]
  excluded?: { no_bid: number; zero_claim: number; thin: number }
}

const fmtK = (k: number): string => k.toFixed(k % 1 === 0 ? 0 : 1)

export function InsurePage() {
  const [status, setStatus] = useState<StatusResponse | null>(null)
  const [statusErr, setStatusErr] = useState<string | null>(null)
  const [scans, setScans] = useState<Record<string, ScanResponse | null>>({})
  const [hidden, setHidden] = useState<Set<string>>(new Set())
  const [tick, setTick] = useState(0)
  const alive = useRef(true)
  useEffect(() => () => { alive.current = false }, [])

  const loadStatus = useCallback(async () => {
    try {
      const s = await api<StatusResponse>('GET', '/api/insure/status')
      if (alive.current) { setStatus(s); setStatusErr(null) }
      return s
    } catch (e) {
      if (alive.current) setStatusErr(String(e))
      return null
    }
  }, [])

  // Mount → status → staggered per-symbol scans (4 at a time). 60s re-poll;
  // 2.5s only while any sweep reports measuring (the DataPage job cadence).
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      const s = await loadStatus()
      if (!s || cancelled) return
      const syms = s.favorites.map((f) => f.symbol)
      const pool = [...syms]
      const workers = Array.from({ length: Math.min(4, pool.length) }, async () => {
        for (;;) {
          const sym = pool.shift()
          if (!sym || cancelled) return
          try {
            const r = await api<ScanResponse>(
              'GET', `/api/insure/scan?symbol=${encodeURIComponent(sym)}`)
            if (!cancelled) setScans((cur) => ({ ...cur, [sym]: r }))
          } catch (e) {
            if (!cancelled) setScans((cur) => ({
              ...cur,
              [sym]: { symbol: sym, available: false, reason: String(e),
                       expectancy: { status: 'none' } },
            }))
          }
        }
      })
      await Promise.all(workers)
    })()
    return () => { cancelled = true }
  }, [tick, loadStatus])

  useEffect(() => {
    const anyMeasuring = Object.values(scans).some(
      (s) => s?.expectancy?.status === 'measuring')
    const t = window.setInterval(() => setTick((n) => n + 1),
      anyMeasuring ? 2500 : 60_000)
    return () => window.clearInterval(t)
  }, [scans])

  const today = new Date().toISOString().slice(0, 10)

  // ---- the dots: every plotted point passes through optgrid.insurePoint ---
  const { dots, honesty } = useMemo(() => {
    const out: InsureDot[] = []
    let noBid = 0
    let zeroClaim = 0
    let thin = 0
    for (const s of Object.values(scans)) {
      if (!s?.available || !s.candidates) continue
      for (const c of s.candidates) {
        const m = c.measured && c.measured.n_exp !== undefined ? c.measured : null
        if (c.offered_pct === null) { noBid += 1; continue }
        if (!m || !m.n_exp) continue
        if (m.zero_claims_reason) { zeroClaim += 1; continue }
        if (confidenceOf(m.n_exp) === 'none') { thin += 1; continue }
        const req = requiredCreditPct(m)
        const pt = insurePoint(c.offered_pct, req, today, c.expiration)
        if (!pt) continue
        out.push({
          symbol: s.symbol, occ: c.occ ?? '', expiration: c.expiration,
          dte: c.dte, strike: c.strike,
          x: pt.x, y: pt.y, edgeAnnual: pt.edgeAnnual,
          nExp: m.n_exp, claimFreq: m.claim_freq ?? 0,
          impliedDelta: m.implied ?? null,
          label: `${s.symbol} ${fmtK(c.strike)}P ${c.dte}d`,
        })
      }
    }
    return { dots: out, honesty: { noBid, zeroClaim, thin } }
  }, [scans, today])

  // ---- the ranked table: four hard sections that never interleave ---------
  const rows = useMemo(() => {
    const all: { s: ScanResponse; c: Candidate }[] = []
    for (const s of Object.values(scans)) {
      if (s?.available && s.candidates) {
        for (const c of s.candidates) all.push({ s, c })
      }
    }
    const withEdge = all.filter(({ c }) =>
      c.edge_pct !== null && c.tier === 'solid')
    const worth = withEdge.filter(({ c }) => (c.edge_pct ?? 0) > 0)
      .sort((a, b) => (b.c.edge_pct ?? 0) - (a.c.edge_pct ?? 0))
    const below = withEdge.filter(({ c }) => (c.edge_pct ?? 0) <= 0)
      .sort((a, b) => (b.c.edge_pct ?? 0) - (a.c.edge_pct ?? 0))
    const tail = all.filter(({ c }) =>
      c.measured?.zero_claims_reason && c.offered_pct !== null)
    const noVerdict = all.filter(({ c }) =>
      !worth.some((w) => w.c === c) && !below.some((w) => w.c === c) &&
      !tail.some((w) => w.c === c))
    return { worth, below, tail, noVerdict }
  }, [scans])

  const openOpt = (symbol: string, occ: string) => {
    window.grindstone.openTab?.(`opt:${symbol}${occ ? `:${occ}` : ''}`)
  }

  const scanned = Object.values(scans).filter(Boolean).length
  const total = status?.favorites.length ?? 0
  const measuring = Object.values(scans).some(
    (s) => s?.expectancy?.status === 'measuring')
  const symbols = status?.favorites.map((f) => f.symbol) ?? []

  const cell = ({ s, c }: { s: ScanResponse; c: Candidate }) => {
    const m = c.measured && c.measured.n_exp !== undefined ? c.measured : null
    const pt = c.offered_pct !== null && m
      ? insurePoint(c.offered_pct, requiredCreditPct(m), today, c.expiration)
      : null
    const offered$ = c.mid !== null ? c.mid * 100 : null
    return (
      <tr key={`${s.symbol}:${c.occ}:${c.expiration}:${c.strike}`}
        className="insure-row"
        onClick={() => c.occ && openOpt(s.symbol, c.occ)}>
        <td className="em">{s.symbol} {fmtK(c.strike)}P · {c.expiration} · {c.dte}d</td>
        <td>{offered$ !== null && c.offered_pct !== null
          ? `$${offered$.toFixed(0)} · ${(c.offered_pct * 100).toFixed(2)}%`
          : 'no bid'}</td>
        <td>{m && requiredCreditPct(m) !== null
          ? `${(requiredCreditPct(m)! * 100).toFixed(2)}%`
          : m?.zero_claims_reason ? 'tail unpriced' : '—'}</td>
        <td className={pt && pt.edgeAnnual >= 0 ? 'gain' : pt ? 'loss' : ''}>
          {pt ? `${pt.edgeAnnual >= 0 ? '+' : ''}${pt.edgeAnnual.toFixed(1)}%/yr` : '—'}
          {/* THE BID EDGE, beside the mid edge: the fill you can hit without
              negotiating. Measured on SPXL: crossing from mid to bid costs
              0.3-0.8% of strike, which erases the smallest edges entirely —
              a mid-only column was the flattering half of the truth. */}
          {(() => {
            if (!pt || !m || c.bid === null || c.bid <= 0) return null
            const bp = insurePoint(c.bid / c.strike, requiredCreditPct(m),
              today, c.expiration)
            return bp ? (
              <span className={`dim subtle${bp.edgeAnnual < 0 ? ' loss' : ''}`}>
                {' '}· bid {bp.edgeAnnual >= 0 ? '+' : ''}{bp.edgeAnnual.toFixed(1)}
              </span>
            ) : null
          })()}
        </td>
        <td>{m?.claim_freq !== undefined && m.n_exp
          ? `assigned ${((m.claim_freq ?? 0) * 100).toFixed(1)}%`
            + (m.implied != null ? ` · Δ said ${(m.implied * 100).toFixed(0)}%` : ' · Δ —')
          : '—'}</td>
        <td>{m?.win_at_offer != null ? `${(m.win_at_offer * 100).toFixed(0)}%` : '—'}</td>
        <td>{m?.n_exp ? `${m.n_exp} exp · ${m.episodes ?? 0} epi` : '—'}</td>
        <td className="loss">{m?.severity
          ? `−${(m.severity.worst * 100).toFixed(1)}% on ${m.severity.worst_date}`
          : '—'}</td>
      </tr>
    )
  }

  const section = (title: string, items: { s: ScanResponse; c: Candidate }[],
                   note?: string) =>
    items.length === 0 ? null : (
      <section className="insure-section" key={title}>
        <h2>{title} <span className="dim subtle">{note ?? ''}</span></h2>
        <table className="insure-table">
          <thead>
            <tr>
              <th>contract</th><th>offered</th><th>need</th><th>edge</th>
              <th>odds</th><th>win@offer</th><th>n</th><th>worst</th>
            </tr>
          </thead>
          <tbody>{items.map(cell)}</tbody>
        </table>
      </section>
    )

  return (
    <div className="page insure-page" data-insure-scanned={scanned}
      data-insure-total={total}>
      <div className="page-head">
        <InsureIcon />
        <h1>Insure</h1>
        <span className="dim subtle">selling puts, priced like insurance</span>
        {status?.archive ? (
          <span className="badge" data-insure-archive>
            your archive · {Object.keys(status.archive.underlyings).length} symbols
          </span>
        ) : null}
        <span className="dim subtle">{scanned} of {total} scanned</span>
        {measuring ? <span className="badge">measuring…</span> : null}
        <button type="button" className="btn" onClick={() => setTick((n) => n + 1)}>
          Refresh
        </button>
      </div>

      {statusErr ? <div className="test-result bad">{statusErr}</div> : null}
      {status && !status.available ? (
        <div className="chain-empty" data-insure-nodb>{status.reason}</div>
      ) : null}
      {status && status.favorites.length === 0 ? (
        <div className="dim subtle lc-empty">
          No favorite symbols yet — star a ticker (its page header) and it
          appears here as an insurance candidate.
        </div>
      ) : null}

      {/* legend chips: hue = symbol; click toggles */}
      {symbols.length > 0 ? (
        <div className="insure-legend">
          {symbols.map((sym, i) => (
            <button key={sym} type="button"
              className={`seg-btn${hidden.has(sym) ? '' : ' on'}`}
              // A swatch, not just a border: the 'on' state paints the whole
              // chip, which hid the very colour the chip is there to teach.
              style={{
                borderColor: SYMBOL_INK[i % SYMBOL_INK.length],
                borderLeft: `10px solid ${SYMBOL_INK[i % SYMBOL_INK.length]}`,
              }}
              onClick={() => setHidden((cur) => {
                const next = new Set(cur)
                if (next.has(sym)) next.delete(sym)
                else next.add(sym)
                return next
              })}>
              {sym}
            </button>
          ))}
        </div>
      ) : null}

      {dots.length > 0 ? (
        <div className="opt-card">
          {/* `order` is the legend's own list, so a chip and its dots can
              never disagree about what a colour means. */}
          <InsureScatter dots={dots} hidden={hidden} order={symbols} height={400}
            onPick={(d) => d.occ && openOpt(d.symbol, d.occ)} />
          <div className="dim subtle opt-note">
            each dot is one candidate put · across: the measured cost of claims
            for its risk class ({status?.archive ? 'your archive' : 'archive'}
            {status?.archive?.months ? `, ~${status.archive.months} months — one
            regime; a year without a crash under-prices crashes` : ''}) · up:
            today’s credit at the mid · both %/yr of the strike, the heatmap’s
            own unit · dots above the dashed line pay more than the risk has
            cost · ring = measured claim frequency · hollow = thin evidence
            (8–19 expirations) · click a dot to open the contract
            {honesty.noBid || honesty.zeroClaim || honesty.thin ? (
              ` · not plotted: ${[
                honesty.noBid ? `${honesty.noBid} with no bid` : '',
                honesty.zeroClaim ? `${honesty.zeroClaim} tail-unpriced (zero claims this year)` : '',
                honesty.thin ? `${honesty.thin} too thin to plot` : '',
              ].filter(Boolean).join(', ')} — listed below`
            ) : null}
            {/* The year confesses: a window in which the underlyings ROSE
                makes every short put look brilliant, and that is the window
                these edges were measured in. One number per symbol, no model. */}
            {(() => {
              const chars = Object.values(scans)
                .filter((s): s is ScanResponse => !!s?.available && !!s.window_character)
                .map((s) => `${s.symbol} ${s.window_character!.return_pct >= 0 ? '+' : ''}` +
                  `${(s.window_character!.return_pct * 100).toFixed(0)}% ` +
                  `(worst dip ${(s.window_character!.max_drawdown_pct * 100).toFixed(0)}%)`)
              return chars.length
                ? ` · THE WINDOW'S CHARACTER: ${chars.join(' · ')} — a rising year
                   flatters every put; the fair line has never seen a crash`
                : null
            })()}
          </div>
        </div>
      ) : scanned > 0 ? (
        <div className="dim subtle lc-empty" data-insure-dots-empty>
          nothing plottable yet —
          {Object.values(scans).filter((s) => s && !s.available).length > 0
            ? ' some symbols refused (reasons below);'
            : ''} the measured side needs archived chains and the offered side
          needs a live chain
        </div>
      ) : null}

      {section('Worth a look', rows.worth,
        '— the market pays more than this risk has cost, on solid evidence')}
      {section('Pays less than it has cost', rows.below,
        '— bad insurance to write right now')}
      {section('Tail unpriced', rows.tail,
        '— zero claims in the window; one year cannot price these')}
      {section('No verdict', rows.noVerdict,
        '— no bid, no archive, or too thin to rank')}

      {/* per-symbol refusals, verbatim — a failed symbol fails alone */}
      {Object.values(scans).filter((s): s is ScanResponse => !!s && !s.available)
        .map((s) => (
          <div key={s.symbol} className="dim subtle" data-insure-refused={s.symbol}>
            {s.symbol}: {s.reason}
          </div>
        ))}
      {Object.values(scans).filter((s): s is ScanResponse =>
        !!s?.available && !!s.chain?.reason).map((s) => (
          <div key={`chain:${s.symbol}`} className="dim subtle">
            {s.symbol}: the archive can price the risk — only a live chain can
            say what it pays today: {s.chain?.reason}
          </div>
        ))}
    </div>
  )
}
