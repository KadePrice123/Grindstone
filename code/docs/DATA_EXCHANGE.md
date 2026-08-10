# Get data / Post data — requirements

Status: **specification, not yet built.** Written before the code so it is
built against a decided shape. Companion to
[AGENT_CONTROL.md](AGENT_CONTROL.md), which builds on this primitive.

One idea: **any enrolled element can hand you its data, and any enrolled
target can accept data it understands.** Get data on a chart grabs everything
the chart stores; on a single line, that line's data; on an option chain, the
whole chain; on a form, its values. Post data applies what you're holding to
whatever you click next — a chain posted onto a chart becomes legs, a
contract posted onto the backtest form becomes a spec, form values posted
onto the same form elsewhere fill it in. No copying field by field, ever.

This is also the agent's data channel. The AI workspace
([AGENT_CONTROL.md](AGENT_CONTROL.md)) moves data to and from the user's
workspace **only** through this primitive — same payloads, same pad, same
rules — which is what makes an agent's actions legible: it is doing exactly
what the human would do, through the same door.

---

## 1. Enrollment — the tool is grey until an element earns it

**Decision (Kade's).** Get/Post only light up on **enrolled** entities;
everywhere else they are greyed out. Enrollment is deliberate, incremental,
and per element class — we take our time, enroll only what we really want,
and test each enrollment before it ships.

This makes rollout state *visible*: the wheel opens, Get is lit on the chain
and grey on the news article, and the UI **is** the compatibility table.
Nothing claims universality; the claim is made element by element, by
lighting up.

**Requirement DX-1.** An enrollment is one declaration in the registry
carrying:

1. the element class and its **payload schema** (what Get serializes),
2. its **target resolver** (how the clicked instance is found — see DX-8),
3. for targets: the payload kinds it **accepts**, and per accepted kind,
   what *applying* concretely means,
4. **tests**: a fixture payload that round-trips, plus a refusal test for
   every non-accepted kind.

**Requirement DX-2 — compatibility lives with the target, appears on the
source.** A news article's effective target set simply lacks "chart", so Post
greys over charts while news is held — exactly the metadata behaviour Kade
described. The *declaration* sits on the target, because the target owns what
"apply" means (a chart knows how to chart a chain; a chain knows nothing
about charts), and new targets can then accept existing payloads without
editing every payload already enrolled.

**Requirement DX-3 — a refusal is a valid conversion.** Post on a
compatible-looking but unaccepted pair does not no-op: the segment is grey,
and the reason is one line ("chain has 38 contracts; charts hold 12 legs —
narrow the window or post one contract"). A grey tool that never explains
itself reads as broken.

**Requirement DX-4 — the registry is the single source of truth.** The gate
asserts the UI's lit/grey state matches the registry exactly, and
`robots.md` is generated from it, so the agent knows precisely what it may
grab and post from the same declaration the human uses.

### v1 enrollment set

| Enrolled | As source | As target |
|---|---|---|
| chart (whole doc) | yes | yes — accepts contract, chain, drawing, chart-doc |
| single drawing/line | yes | — (posts *onto* charts) |
| option leg | yes | — |
| option chain | yes | no |
| single contract (chain row / heatmap cell) | yes | no |
| backtest spec form | yes | yes — accepts contract, chain, backtest-spec |
| recording-job form | yes | yes — accepts same-kind form |
| **Not enrolled in v1:** news, settings, accounts form (deliberately — §5), tables, third-party browser pages (structurally never — their preload sends no context by hardening design). |

---

## 2. The payload

A discriminated union, versioned, with provenance:

```
DataPayload = {
  v: 1,
  kind: 'chart-doc' | 'drawing' | 'leg' | 'chain' | 'contract'
      | 'form' | 'backtest-spec',
  data: <per-kind>,
  provenance: {
    page, key?, symbol?, timeframe?, axis?,   // EXPLICIT — chart keys are not
                                              // self-describing (symbol-page
                                              // keys omit axis; 'multi|1Day|%'
                                              // names no symbol)
    workspace: 'user' | 'agent',              // who grabbed it (AGENT_CONTROL)
    capturedAt, user
  }
}
```

Grounding rules that came out of the scout, each preventing a real bug:

- **chain/contract carry all 13 backend fields** (`occ_symbol, expiration,
  strike, right, bid, ask, last, iv, delta, gamma, theta, vega, rho`). The
  frontend `Contract` interfaces declare only 9 — the payload is written from
  the **backend envelope** (`options.py:82-88`), or four greeks already on
  the wire get silently discarded.
- **leg serializes the RESOLVED leg** (`resolveLegDoc`), never the stored
  birth values — the Opt page already shipped a bug by reading stored fields
  (`OptPage.tsx:18-24`). The stored leg and the pick ride along.
- **drawing carries its closure**: the constraints naming it, measures
  anchored on it, legs hosting it. A line alone is two points; its meaning
  lives in the collections that reference its id. The *target* decides what
  of the closure to keep.
- **capture is spawn-time.** The wheel freezes context at the spawning
  right-click by design (stale-hover race, `wheel.ts:33-36`); `capturedAt`
  makes that honest rather than surprising.

---

## 3. The pad — one holding slot, backend-held

**Requirement DX-5.** The held payload lives in the backend
(`backend/datapad.py`, `GET/PUT /api/datapad`, one slot per user, ≤256 KB,
strict validator in the `chartobjects.py` style). Not renderer state, not
main-process state — the backend is the only component both app instances
see, so a pad held anywhere else is invisible to the agent. The wheel's main
process caches only a `hasPayload` boolean to light/grey the Post segment.

**Requirement DX-6.** One slot, replaced on every Get. A clipboard history is
v2; a stack nobody can see is a bug factory.

**Requirement DX-7 — one codepath.** Human gesture → `data:action` IPC →
page adapter → `/api/datapad`. Agent → `get_data`/`post_data` tools →
`/api/datapad` directly, or drives its own UI which runs the same adapters.
No second serialization path exists, so the payload the agent produces is
byte-compatible with the one the wheel produces.

---

## 4. The wheel plumbing this needs

The scout established the wheel today carries **a tool name and at most one
symbol — never an element target**, and context-sensing exists for exactly
one element class (the chart container's `data-wheel-context`).

**Requirement DX-8 — target resolution.** `sendDataAction` (a sibling of
`sendChartAction`) forwards the **spawn coordinates**, which main already
holds and chart actions deliberately drop. The page handler resolves the
element at those coordinates (the heatmap cell's `data-occ`, the engine
hit-test for a line), falls back to the engine's current selection, falls
back to the whole container. Down-click semantics, accepted and documented —
a release-time re-read is the cross-process race the wheel's own comments
warn about.

**Requirement DX-9 — enrollment declarations generalize the existing seam.**
Elements declare themselves the way the chart already does
(`data-wheel-context` + data attributes), and the wheel greys Get where no
declaration is under the cursor. Grey-state computation happens at wheel
open, from the spawn-time snapshot.

**Requirement DX-10.** New tools land at the three documented registration
points (catalog entry, page handler, backend validation) as a `DATA_TOOLS`
tuple — a **sibling** of `CHART_TOOLS`, not a member, because these tools are
not chart-only. Adding Get/Post to default wheel layouts bumps `DOC_VERSION`
5→6, which regenerates stored layouts — announced, deliberate.

---

## 5. Secrets — what Get must never serialize

The pad is read by the agent **by design**, which makes it an exfiltration
path if a credential ever lands in it. Nothing in any form's state marks a
field secret today; only DOM `type="password"` does.

**Requirement DX-11 — three layers, the last one is the boundary:**

1. **Default-deny**: only forms with a registered adapter serialize at all,
   and adapters enumerate fields (allowlist) — never a DOM or state scrape.
   The accounts form and the recording-key flow have no adapter, ever.
2. **Adapter helper** refuses any field rendered `type="password"` or
   declared `secret: true` — belt for adapters written carelessly later.
3. **Backend validator** on `PUT /api/datapad` scans for credential-shaped
   strings (the `doctor.py` precedent) and 422s. This is the layer a
   CDP-driven agent cannot bypass; renderer gating alone is defeatable
   (established in AGENT_CONTROL §3).

The stored-accounts *list* is safe to enroll later: `GET /api/accounts`
already projects id/broker/kind/nickname/hints and never credentials — that
projection exists; do not build a second one.

---

## 6. Post — the v1 conversion matrix

Cardinal rule, proven in-repo: **every chart-targeted post routes through the
owning page's live engine** (`draw.current`), never a direct PUT. The 400 ms
whole-doc autosave plus the session cache silently reverts any out-of-band
write — the Opt-page pick deletion was exactly this, "post reports success,
data vanishes, no error anywhere."

| Payload → target | v1 | Applying means |
|---|---|---|
| contract → chart | yes | `engine.addLeg({right, expiration, strike, pick})`; side defaults long, flipped in the LegEditor |
| chain → chart | yes, capped | ≤12 distinct (strike, expiration) → `addLegGroup` as one strategy group; more → refuse with the count. Charting strike/max-gain/expiration as a derived *series* needs a new engine element — named v2 gap. |
| contract/chain → backtest form | yes | map to spec JSON (underlying, per-leg right/strike-or-delta/dte), write into the spec editor; the existing debounced validate gives the engine's verdict free |
| drawing → chart | yes | re-mint id; keep absolute time/price; drop constraints whose other endpoint is absent; `legOwned` lines refuse ("guide lines belong to their leg — post the leg") |
| chart-doc → chart | yes | append with a consistent old→new id map so intra-doc refs survive; refuse over 500/collection or 12 legs, with counts |
| form → same-kind form | yes | apply through the form's own save path, respecting its sequence guards — never raw setState |
| contract → Opt page | refuse | teaching reason: "the Opt page mirrors the chart — post to the chart instead." The Opt page is a pure reader by hard-won rule. |
| anything → AI chat | with the panel | the pad is backend-held; the AI panel reads it like any client |

---

## 7. Verification

1. **Registry↔UI parity** — the lit/grey state matches the registry; an
   unenrolled element can never produce a payload (gate).
2. **Round-trip per kind** — fixture payloads validate, oversize/malformed
   422 (gate).
3. **The 13-field assertion** — a chain Get carries all backend fields, not
   the frontend 9 (e2e, from a real `data-occ` cell).
4. **The autosave tripwire** — post a contract, screenshot the leg on the
   chart, re-read the doc after >400 ms and assert it survived (e2e).
5. **Secrets** — an account fixture's secret cannot round-trip the pad
   (gate, 422); the accounts form has no adapter (gate, registry scan).
6. **Refusals say why** — every non-accepted pair refuses with a non-empty
   reason (gate, table-driven).
7. **Mutation-tested** via `tools/mutate.py` before any of it is trusted.
