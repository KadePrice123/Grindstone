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

/** What the newest routable notepad entry is, or null when nothing is held. */
export interface PadHint {
  label: string
  address: string
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
export function predictIntent(
  tool: ToolKey,
  ctx: PredictCtx | null,
  pad: PadHint | null,
): Prediction | null {
  // 1. HELD DATA WINS. It is the most recent thing the user did, and the
  //    least surprising thing for the app to still be thinking about.
  if (pad && HELD_DATA_TOOLS.has(tool)) {
    return { hint: `→ ${pad.label}`, kind: 'address', arg: pad.address }
  }
  // 2. THE CLASS PREDICTION.
  const cls = ctx?.context ?? ''
  const row = CLASS_PREDICTIONS[tool]
  const fn = row ? row[cls] : undefined
  const byClass = fn ? fn(ctx ?? {}) : null
  if (byClass) return byClass
  // 3. THE TOOL'S UNIVERSAL QUICK VARIANT.
  const uni = UNIVERSAL_QUICK[tool]
  const byTool = uni ? uni(ctx ?? {}) : null
  if (byTool) return byTool
  // 4. No prediction: quick is primary. Returning null rather than a
  //    no-op prediction keeps that indistinguishable from a tool that was
  //    never enrolled — which is the honest state.
  return null
}

/** The classes that predict for a tool, for the gate and for docs. */
export function predictingClasses(tool: ToolKey): string[] {
  return Object.keys(CLASS_PREDICTIONS[tool] ?? {}).sort()
}
