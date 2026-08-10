/**
 * The pure heart of Get/Post: the connected-component walk.
 *
 * ZERO imports, on purpose twice over: the gate probes this file under
 * plain node, whose type-stripping does not resolve Vite's extensionless
 * imports — and a walk this load-bearing should not be able to grow a
 * dependency without the probe noticing the file stopped loading.
 */

/** Every reference field a leg can use to name a host drawing. Mirrors the
 *  OptionLeg shape: the legacy single binding, the explicit time/price
 *  hosts, and the four bounding lines. Missing one here silently orphans
 *  that relationship in every grab, so the gate pins each name. */
const LEG_REF_FIELDS = [
  'hostId', 'timeHostId', 'priceHostId',
  'strikeHostA', 'strikeHostB', 'timeHostA', 'timeHostB',
] as const

/** The CONNECTED COMPONENT of one drawing or leg — Kade's rule: grabbing a
 *  line that is constrained to other entities grabs the full chain of
 *  objects. Pure and dependency-free on purpose: the same walk must serve
 *  the wheel today and the agent later, and a probe can run it standalone.
 *
 *  Edges, walked to a fixpoint:
 *    constraint  — joins its two endpoint drawings (a and b);
 *    measure     — joins the drawings its anchors sit on;
 *    leg         — joins every host drawing it names, in both directions.
 *
 *  A constraint therefore always travels with BOTH endpoints, so nothing
 *  dangles at post time — completeness instead of the earlier "drop the
 *  dangling reference", which quietly discarded the very relationships that
 *  made the line worth grabbing. Sibling legs of a strategy group join
 *  through their shared vlines, with no group special case.
 */
export function componentOf(
  doc: {
    drawings: Array<{ id: string }>
    measures: Array<{ a?: { drawingId?: string }; b?: { drawingId?: string }; id: string }>
    constraints: Array<{ id: string; a?: { id?: string }; b?: { id?: string } }>
    legs: Array<Record<string, unknown> & { id: string }>
  },
  rootId: string
): { drawings: string[]; measures: string[]; constraints: string[]; legs: string[] } {
  const inDrawings = new Set<string>()
  const inLegs = new Set<string>()
  const isDrawing = new Set(doc.drawings.map((d) => d.id))
  const isLeg = new Set(doc.legs.map((l) => l.id))
  if (isDrawing.has(rootId)) inDrawings.add(rootId)
  else if (isLeg.has(rootId)) inLegs.add(rootId)
  else return { drawings: [], measures: [], constraints: [], legs: [] }

  // Fixpoint, not a single pass: joining a leg can join a drawing that a
  // constraint then bridges to a third, which hosts another leg. The doc is
  // capped (500/collection, 12 legs), so the loop is tiny in practice.
  let grew = true
  while (grew) {
    grew = false
    for (const c of doc.constraints) {
      const aId = c.a?.id
      const bId = c.b?.id
      const touches = (aId !== undefined && inDrawings.has(aId)) ||
                      (bId !== undefined && inDrawings.has(bId))
      if (!touches) continue
      for (const id of [aId, bId]) {
        if (id !== undefined && isDrawing.has(id) && !inDrawings.has(id)) {
          inDrawings.add(id)
          grew = true
        }
      }
    }
    for (const m of doc.measures) {
      const aId = m.a?.drawingId
      const bId = m.b?.drawingId
      const touches = (aId !== undefined && inDrawings.has(aId)) ||
                      (bId !== undefined && inDrawings.has(bId))
      if (!touches) continue
      for (const id of [aId, bId]) {
        if (id !== undefined && isDrawing.has(id) && !inDrawings.has(id)) {
          inDrawings.add(id)
          grew = true
        }
      }
    }
    for (const l of doc.legs) {
      const hosts = LEG_REF_FIELDS
        .map((f) => l[f])
        .filter((v): v is string => typeof v === 'string')
      const touches = inLegs.has(l.id) || hosts.some((h) => inDrawings.has(h))
      if (!touches) continue
      if (!inLegs.has(l.id)) {
        inLegs.add(l.id)
        grew = true
      }
      for (const h of hosts) {
        if (isDrawing.has(h) && !inDrawings.has(h)) {
          inDrawings.add(h)
          grew = true
        }
      }
    }
  }

  // Collections that REFERENCE the component, kept in doc order so the
  // subdoc round-trips deterministically.
  const measures = doc.measures
    .filter((m) => (m.a?.drawingId !== undefined && inDrawings.has(m.a.drawingId)) ||
                   (m.b?.drawingId !== undefined && inDrawings.has(m.b.drawingId)))
    .map((m) => m.id)
  const constraints = doc.constraints
    .filter((c) => (c.a?.id !== undefined && inDrawings.has(c.a.id)) ||
                   (c.b?.id !== undefined && inDrawings.has(c.b.id)))
    .map((c) => c.id)
  return {
    drawings: doc.drawings.filter((d) => inDrawings.has(d.id)).map((d) => d.id),
    measures,
    constraints,
    legs: doc.legs.filter((l) => inLegs.has(l.id)).map((l) => l.id),
  }
}


