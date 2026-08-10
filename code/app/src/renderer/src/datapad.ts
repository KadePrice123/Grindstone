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

/** Announce what a data action did. The quick variants skip every picker, so
 *  this feedback is what keeps them legible (DX-6): the pages own the
 *  rendering; this is the one channel they all listen on. */
export function announce(text: string): void {
  window.dispatchEvent(new CustomEvent('datapad:announce', { detail: text }))
}
