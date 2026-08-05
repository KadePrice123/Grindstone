/** The eight distinguishable series colors on the dark surface — one shared
 *  ramp for everything that needs "give each of N things its own color":
 *  compare-chart lines today, options legs next. Assignment is by SLOT
 *  (index), so colors shift when an earlier item is removed — stable identity
 *  would cost a persisted map for no real gain, the same call ChartsPage
 *  already made and documented. */
export const PALETTE = [
  '#d98324',
  '#6ba4e8',
  '#2ebd85',
  '#c77dd6',
  '#e5484d',
  '#e8c55b',
  '#5bc8c8',
  '#9aa0a6',
] as const

export const paletteColor = (slot: number): string => PALETTE[slot % PALETTE.length]
