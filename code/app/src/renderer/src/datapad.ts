/**
 * The renderer half of Get/Post data (docs/DATA_EXCHANGE.md).
 *
 * Payload BUILDERS are pure — (element state, context) -> DataPayload — so
 * they can be probed under plain node and shared by every caller: the wheel
 * handlers here, and later the agent, which produces byte-compatible payloads
 * because there is exactly one serialization path. The backend
 * (`backend/notepad.py`) is the enforced schema; these types are convenience.
 *
 * Provenance is ROUTABLE by design: `address` is the element's own .gs URL,
 * so "open the source" is `openTab(address)` — no new machinery (DX-7d).
 */
import { api } from './api'
import { componentOf } from './datapadCore'
import type { ChartDoc } from './components/ChartDraw'

export type PayloadKind =
  | 'chart-doc' | 'drawing' | 'leg' | 'chain' | 'contract'
  | 'form' | 'backtest-spec' | 'note'

export interface Provenance {
  address?: string
  page: string
  key?: string
  symbol?: string
  timeframe?: string
  axis?: '%' | '$'
  workspace: 'user' | 'agent'
  capturedAt: string
}

export interface DataPayload {
  v: 1
  kind: PayloadKind
  data: Record<string, unknown>
  provenance: Provenance
}

/** Whether THIS renderer is the agent instance. Stamped into provenance so a
 *  grab's origin workspace stays visible wherever the entry travels. */
export function workspace(): 'user' | 'agent' {
  return (window as { GRINDSTONE_AGENT?: boolean }).GRINDSTONE_AGENT === true
    ? 'agent'
    : 'user'
}

function provenance(p: Omit<Provenance, 'workspace' | 'capturedAt'>): Provenance {
  return { ...p, workspace: workspace(), capturedAt: new Date().toISOString() }
}

// ------------------------------------------------------------------ builders
/** The whole chart: everything the doc stores, plus enough provenance to
 *  reopen the source chart and to re-key a posted copy. */
export function buildChartDocPayload(args: {
  key: string
  doc: ChartDoc
  page: string
  address: string
  symbol?: string
  timeframe?: string
  axis?: '%' | '$'
}): DataPayload {
  return {
    v: 1,
    kind: 'chart-doc',
    data: { key: args.key, doc: args.doc as unknown as Record<string, unknown> },
    provenance: provenance({
      page: args.page,
      address: args.address,
      key: args.key,
      symbol: args.symbol,
      timeframe: args.timeframe,
      axis: args.axis,
    }),
  }
}

/** One contract, ALL 13 backend fields (plus the grid's derived values when
 *  sourced from a heatmap cell). Built from the backend envelope on purpose:
 *  the page-local Contract interfaces carry 9 fields, and serializing from
 *  them would silently drop four greeks that are already on the wire. */
export function buildContractPayload(args: {
  contract: Record<string, unknown>
  page: string
  address: string
  symbol: string
}): DataPayload {
  return {
    v: 1,
    kind: 'contract',
    data: args.contract,
    provenance: provenance({ page: args.page, address: args.address, symbol: args.symbol }),
  }
}

/** The whole chain envelope, verbatim, plus the query that produced it. */
export function buildChainPayload(args: {
  envelope: Record<string, unknown>
  page: string
  address: string
  symbol: string
}): DataPayload {
  return {
    v: 1,
    kind: 'chain',
    data: args.envelope,
    provenance: provenance({ page: args.page, address: args.address, symbol: args.symbol }),
  }
}

/** A single drawing (or leg) grab: the component, as a SUBDOC — the same
 *  shape a chart-doc payload carries, so the post path is one code path. */
export function buildDrawingPayload(args: {
  rootId: string
  doc: ChartDoc
  page: string
  address: string
  symbol?: string
  timeframe?: string
}): DataPayload | null {
  const d = args.doc as unknown as Parameters<typeof componentOf>[0]
  const comp = componentOf(d, args.rootId)
  if (comp.drawings.length === 0 && comp.legs.length === 0) return null
  const keep = {
    drawings: new Set(comp.drawings), measures: new Set(comp.measures),
    constraints: new Set(comp.constraints), legs: new Set(comp.legs),
  }
  const doc = args.doc as unknown as {
    version: number
    drawings: Array<{ id: string }>
    measures: Array<{ id: string }>
    pins: unknown[]
    constraints: Array<{ id: string }>
    legs: Array<{ id: string }>
  }
  return {
    v: 1,
    kind: 'drawing',
    data: {
      root: args.rootId,
      subdoc: {
        version: doc.version,
        drawings: doc.drawings.filter((x) => keep.drawings.has(x.id)),
        measures: doc.measures.filter((x) => keep.measures.has(x.id)),
        pins: [],   // pins anchor to bars, not to drawings — never part of a component
        constraints: doc.constraints.filter((x) => keep.constraints.has(x.id)),
        legs: doc.legs.filter((x) => keep.legs.has(x.id)),
      },
    },
    provenance: provenance({
      page: args.page, address: args.address,
      symbol: args.symbol, timeframe: args.timeframe,
    }),
  }
}

// ------------------------------------------------------------- compatibility
/** What each TARGET class accepts (DX-2: declared on the target, appears on
 *  the source). This map is the renderer's authority; main mirrors it for
 *  picker greying, and the gate pins the two against each other. */
export const ACCEPTS: Record<string, PayloadKind[]> = {
  chart: ['contract', 'chain', 'drawing', 'chart-doc'],
  'backtest-form': ['contract', 'chain', 'backtest-spec'],
}

// ------------------------------------------------------------------ applying
export interface ChartEngine {
  addLeg(l: Record<string, unknown>): { ok: true; id: string }
  addLegGroup(specs: Array<Record<string, unknown>>): string
}

type ApplyResult = { ok: true; what: string } | { ok: false; reason: string }

/** Post onto a chart, THROUGH THE LIVE ENGINE — never a direct PUT: the
 *  400ms whole-doc autosave silently reverts out-of-band writes (the proven
 *  in-repo failure). A refusal is a valid conversion and carries its reason. */
export function applyToChart(engine: ChartEngine, payload: DataPayload): ApplyResult {
  if (payload.kind === 'contract') {
    const c = payload.data as { right?: string; expiration?: string; strike?: number; occ_symbol?: string }
    if (!c.right || !c.expiration || typeof c.strike !== 'number') {
      return { ok: false, reason: 'contract payload is missing strike/expiration/right' }
    }
    engine.addLeg({
      side: 'long',            // the safe default; flipped in the LegEditor
      right: c.right,
      expiration: c.expiration,
      strike: c.strike,
      dteTol: 3,
      strikeTol: 4,
      pick: c.occ_symbol,
    })
    return { ok: true, what: `leg ${c.strike}${c.right} ${c.expiration}` }
  }
  if (payload.kind === 'chain') {
    const rows = (payload.data as { contracts?: Array<Record<string, unknown>> }).contracts ?? []
    const seen = new Set<string>()
    const distinct = rows.filter((r) => {
      const k = `${r.strike}|${r.expiration}|${r.right}`
      if (seen.has(k)) return false
      seen.add(k)
      return true
    })
    if (distinct.length > 12) {
      return {
        ok: false,
        reason: `chain has ${distinct.length} contracts; charts hold 12 legs — ` +
                `narrow the window or post one contract`,
      }
    }
    if (distinct.length === 0) return { ok: false, reason: 'the chain payload holds no contracts' }
    engine.addLegGroup(distinct.map((r) => ({
      side: 'long', right: r.right, expiration: r.expiration, strike: r.strike,
      dteTol: 3, strikeTol: 4, pick: r.occ_symbol,
    })))
    return { ok: true, what: `${distinct.length}-leg group` }
  }
  return { ok: false, reason: `a chart does not accept ${payload.kind} yet` }
}

// --------------------------------------------------------------------- pad
export interface PadEntry {
  id: string
  payload: DataPayload
  label: string
  added_at?: string
}

/** Add to the notepad. The backend validates (and refuses secrets); the
 *  caller surfaces the refusal reason — a grab that silently vanished would
 *  be indistinguishable from one that worked. */
export async function grab(payload: DataPayload, label = ''): Promise<PadEntry> {
  return api<PadEntry>('POST', '/api/notepad', { payload, label })
}

/** The pad's entries, newest first (the backend orders them). */
export async function listPad(): Promise<PadEntry[]> {
  return api<PadEntry[]>('GET', '/api/notepad')
}

/** The quick-post rule (DX-6): the NEWEST entry whose kind the target class
 *  accepts. Null is an announceable outcome, not an error. */
export function mostRecentCompatible(
  entries: PadEntry[], targetClass: string
): PadEntry | null {
  const ok = new Set(ACCEPTS[targetClass] ?? [])
  return entries.find((e) => ok.has(e.payload.kind)) ?? null
}

/** Announce what a data action did. The quick variants skip every picker, so
 *  this feedback is what keeps them legible (DX-6): the pages own the
 *  rendering; this is the one channel they all listen on. */
export function announce(text: string): void {
  window.dispatchEvent(new CustomEvent('datapad:announce', { detail: text }))
}
