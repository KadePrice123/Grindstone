/**
 * PREDICTIVE QUICK INTENT — the table (docs/DATA_EXCHANGE.md DX-13).
 *
 * Kade's rule: what you clicked changes what a tool ASSUMES you meant, but
 * only what the tool DOES — never where it sits. Wheel layouts are never
 * restructured by context. This is the idea the chart wheel already proves
 * (right-click a chart, get chart tools) generalised to every enrolled class,
 * and confined to the QUICK variant so nothing a user has learned changes.
 *
 * The priority is fixed rather than clever, because a prediction that
 * reorders itself by circumstance is one the user has to think about:
 *
 *   1. held data wins        — a payload's provenance reopens its source
 *   2. else the class prediction — a contract cell knows it means Opt
 *   3. else the tool's universal quick variant, if it declares one
 *   4. else quick behaves as primary
 *
 * It lives HERE, apart from wheel.ts, for two reasons. It is a TABLE keyed by
 * (tool, class) rather than an if-ladder that grows a branch per page; and it
 * is pure, so the gate runs the real priority order under node instead of
 * reading the source and hoping. Like datapadCore.ts this file must stay
 * IMPORT-FREE — the probe runs it under plain node, which cannot resolve
 * Vite-style paths.
 */

/** The frozen spawn context a prediction is resolved from. Structurally the
 *  subset of WheelCtx that predictions may see: widening this is a decision,
 *  not an accident. */
export interface PredictCtx {
  context?: string
  occ?: string
  symbols?: string[]
}

/** What the newest routable notepad entry is, or null when nothing is held.
 *
 *  `destination` is where the DATA belongs (a contract's own Opt page);
 *  `address` is where it was GRABBED. They are different questions, and
 *  answering the first with the second is how "open my contract's history"
 *  became "navigate to the ticker page you are already looking at". */
export interface PadHint {
  label: string
  address: string
  destination?: string
}

/** A resolved prediction. `kind` says how the release acts on `arg`; `hint`
 *  is what the face shows, because a predicted action the user cannot see
 *  before releasing is a misclick generator (DX-14). */
export interface Prediction {
  hint: string
  kind: 'address'
  arg: string
}

/** A tool's identity for the table: its segment type plus the specific thing
 *  it opens. Kept as one string so the table reads as a table. */
export type ToolKey = string

/** THE TABLE. Keyed by tool, then by the element class under the spawn.
 *  Adding a class is adding a row here — not a branch in the dispatcher. */
const CLASS_PREDICTIONS: Record<ToolKey, Record<string, (c: PredictCtx) => Prediction | null>> = {
  // The tab tool over a contract: its natural destination is the options
  // workstation for that contract. No Get data first — the class already
  // knows where its data belongs.
  'wheel:tabs': {
    chain: optFor,
    heatmap: optFor,
  },
}

function optFor(c: PredictCtx): Prediction | null {
  const sym = (c.symbols?.[0] ?? '').toUpperCase()
  if (!sym) return null
  const occ = c.occ ?? ''
  return {
    hint: `→ ${sym} Opt`,
    kind: 'address',
    // tabs.openAddress speaks .gs ADDRESSES, not routes — it drops anything
    // without a .gs head on the floor, silently. A hint the face shows and
    // the release then ignores is worse than showing no hint at all.
    arg: `opt.gs?s=${sym}${occ ? `&occ=${occ}` : ''}`,
  }
}

/** Tools that predict from HELD DATA regardless of what sits underneath.
 *  Only the tab tool does today: with a payload held, it opens that payload's
 *  own source, which is the routable-provenance promise cashed in (DX-7d). */
const HELD_DATA_TOOLS = new Set<ToolKey>(['wheel:tabs'])

/** Tools with a universal quick variant — one that needs neither held data
 *  nor a class. None yet; the rung exists so priority 3 is a real rung the
 *  gate can test, not a comment describing an intention. */
const UNIVERSAL_QUICK: Record<ToolKey, (c: PredictCtx) => Prediction | null> = {}

/**
 * Resolve what a QUICK release on this tool will do, or null for "behave
 * exactly as primary". Pure: same inputs, same answer, no clock, no I/O.
 */
export function predictCandidates(
  tool: ToolKey,
  ctx: PredictCtx | null,
  pad: PadHint | null,
): Prediction[] {
  const out: Prediction[] = []
  // 1. HELD DATA WINS, and its DESTINATION outranks its provenance.
  //
  //    Kade's correction, and it is the whole point of the feature: grab a
  //    contract off a chain, ask where it goes, and the answer is that
  //    contract's options workstation — its strike, its expiration, its
  //    history. NOT the ticker page it was grabbed from, which is the page
  //    the user is already standing on.
  if (pad && HELD_DATA_TOOLS.has(tool)) {
    if (pad.destination) {
      out.push({ hint: `→ ${pad.label}`, kind: 'address', arg: pad.destination })
    }
    // Provenance is the FALLBACK, for kinds with no natural home of their
    // own — and it is still useful when the destination is already open.
    if (pad.address && pad.address !== pad.destination) {
      out.push({ hint: `→ ${pad.label} source`, kind: 'address', arg: pad.address })
    }
  }
  // 2. THE CLASS PREDICTION.
  const cls = ctx?.context ?? ''
  const row = CLASS_PREDICTIONS[tool]
  const fn = row ? row[cls] : undefined
  const byClass = fn ? fn(ctx ?? {}) : null
  if (byClass) out.push(byClass)
  // 3. THE TOOL'S UNIVERSAL QUICK VARIANT.
  const uni = UNIVERSAL_QUICK[tool]
  const byTool = uni ? uni(ctx ?? {}) : null
  if (byTool) out.push(byTool)
  // 4. Nothing: quick is primary. An empty list rather than a no-op entry
  //    keeps that indistinguishable from a tool that was never enrolled —
  //    which is the honest state.
  return out
}

/**
 * The single prediction to show and fire, given what is ALREADY OPEN.
 *
 * Kade's rule: "I probably don't want to navigate to the page where I
 * already am or even a tab I have open — I would just use the regular tab
 * navigation for that, so I probably want to open something that I don't
 * already have open." A shortcut whose payoff is a tab you could have
 * clicked is not a shortcut.
 *
 * `isOpen` answers "is this address already on screen or in a tab?". The
 * first candidate it does not veto wins. If it vetoes every one, the FIRST
 * is used anyway — focusing an existing tab is a weak outcome, but a wheel
 * segment that silently does nothing is a worse one, and the hint on the
 * face has already promised something.
 */
export function predictBest(
  tool: ToolKey,
  ctx: PredictCtx | null,
  pad: PadHint | null,
  isOpen?: (address: string) => boolean,
): Prediction | null {
  const all = predictCandidates(tool, ctx, pad)
  if (all.length === 0) return null
  if (!isOpen) return all[0]
  return all.find((c) => !isOpen(c.arg)) ?? all[0]
}

/** The classes that predict for a tool, for the gate and for docs. */
export function predictingClasses(tool: ToolKey): string[] {
  return Object.keys(CLASS_PREDICTIONS[tool] ?? {}).sort()
}
