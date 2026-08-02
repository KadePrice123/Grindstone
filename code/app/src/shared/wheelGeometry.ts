/**
 * Wheel geometry — imported by BOTH the main process (which decides what a
 * release selects) and the overlay renderer (which draws the highlight).
 * One module so the two can never disagree about what the user is pointing
 * at; the gate checks both sides import from here.
 */

export const WHEEL_RADIUS = 150 // outer ring radius, px
export const WHEEL_INNER = 52 // segments start here
export const WHEEL_DEAD_ZONE = 46 // inside this: "no segment" — release despawns
export const HUB_RADIUS = 42 // the lock/identity hub

/**
 * Which segment a point selects: none inside the dead zone, else by ANGLE
 * alone — beyond the drawn ring still selects (SolidWorks flick behavior; a
 * gesture must not require pixel-precise radius control). Segment 0 is
 * centered at 12 o'clock; indices run clockwise.
 */
export function segmentAt(
  cx: number,
  cy: number,
  px: number,
  py: number,
  count: number
): number | null {
  const dx = px - cx
  const dy = py - cy
  if (Math.hypot(dx, dy) < WHEEL_DEAD_ZONE || count === 0) return null
  const deg = (Math.atan2(dy, dx) * 180) / Math.PI // -180..180, 0 = east
  const span = 360 / count
  const rel = (deg + 90 + span / 2 + 720) % 360
  return Math.floor(rel / span) % count
}

/** SVG path for segment i of n: a donut sector between WHEEL_INNER and
 *  WHEEL_RADIUS, with a small angular gap so segments read as buttons. */
export function sectorPath(i: number, n: number, gapDeg = 2): string {
  const span = 360 / n
  const a0 = -90 - span / 2 + i * span + gapDeg / 2
  const a1 = a0 + span - gapDeg
  const rad = (d: number) => (d * Math.PI) / 180
  const r0 = WHEEL_INNER
  const r1 = WHEEL_RADIUS
  const x = (r: number, a: number) => r * Math.cos(rad(a))
  const y = (r: number, a: number) => r * Math.sin(rad(a))
  const large = span - gapDeg > 180 ? 1 : 0
  return [
    `M ${x(r1, a0)} ${y(r1, a0)}`,
    `A ${r1} ${r1} 0 ${large} 1 ${x(r1, a1)} ${y(r1, a1)}`,
    `L ${x(r0, a1)} ${y(r0, a1)}`,
    `A ${r0} ${r0} 0 ${large} 0 ${x(r0, a0)} ${y(r0, a0)}`,
    'Z',
  ].join(' ')
}

/** Where a segment's icon+label sit: the sector's angular center. */
export function sectorAnchor(i: number, n: number): { x: number; y: number } {
  const span = 360 / n
  const a = ((-90 + i * span) * Math.PI) / 180
  const r = (WHEEL_INNER + WHEEL_RADIUS) / 2
  return { x: r * Math.cos(a), y: r * Math.sin(a) }
}
