# Agent control — requirements

Status: **specification, not yet built.** Written before the code so it is built
against a decided shape.

An AI drives the app the way a person does: it reads the rendered page, clicks,
types, presses buttons, reads text back. Kade enables it deliberately; while it
is on, the app runs on a **system key that cannot trade**, so an agent filling
in forms is safe by construction rather than by my correctness.

A secondary goal: reduce dependence on screenshot e2e. That goal constrains the
design — see §7.

---

## 1. What "read-only" means here

**Decision. Read-only means THE AGENT CANNOT TRADE.** Not "cannot write". An
agent that cannot submit a form cannot test the app, which defeats the purpose.

Two things make a method-based rule wrong, and both are facts about this
codebase rather than opinions:

**A blanket POST ban blocks the exact capability the toggle is for.** The only
two routes that read the *real* broker account — equity, buying power, options
level, account last-4 — are `POST /api/accounts/test` and
`POST /api/accounts/{id}/test` (`app.py:427,435`). Blocking POST blocks reading
the account.

**GETs already write.** `GET /api/symbols/{s}/options` fills and prunes
`chain_cache`; `GET /api/symbols/{s}/bars` fills `bar_cache`; `GET /api/search`
upserts the news store and can issue an outbound web search. So "the agent
changes nothing" is **false today for any page it merely opens**, and the spec
must not claim it.

**Requirement AC-1.** The rule is a **path allow-list**, not a method rule.
Method is a heuristic; the path list is the rule. A deny-list is rejected
because it fails open on the next route somebody adds.

**Requirement AC-2.** The spec distinguishes:
- **durable user-visible state** — blocked (settings, favourites, chart docs,
  wheels, accounts, jobs, imports, presets)
- **provider cache fill** — allowed, and named as allowed
- **the real account** — readable via the two test routes, never writable

---

## 2. The system-key swap — the primary guarantee

**Decision.** While agent control is enabled, the app resolves broker
credentials to the **system key** (per [DATA_SOURCING.md](DATA_SOURCING.md) §6.2
— OAuth `data` scope, or a paper key), never the user's trading key.

This is the load-bearing guarantee, and it is stronger than any check I write,
because it is enforced by the broker.

**Requirement AC-3.** The swap lives in `State.creds_for` (`app.py:86`) — the
single function every broker call resolves credentials through. Not in routes,
not in the UI. Anywhere else and the next call site added bypasses it.

**Requirement AC-4 — it is APP-WIDE, not per-session.** This is forced, not
chosen: `creds_for` borrows the **newest unlocked session for the user**
(`sessions.py:93`), so a "read-only agent session" cannot be made
credential-less while the human is also signed in — the recorder and the quote
path would still find the human's session. Therefore agent mode swaps
credentials for the whole app while it is on. This is also the honest UX: there
is never ambiguity about which key is live.

**Requirement AC-5 — fail closed.** If no system key is configured, enabling
agent control **refuses**. It must never silently fall back to the user's key.

**Requirement AC-6 — the agent cannot disable agent mode.** The toggle is on the
allow-list as readable and on the blocked list as writable. Only the human, at
the real UI, or by locking the app. An agent that can un-restrict itself makes
every other control decorative.

**Requirement AC-7 — unmistakable while on.** A persistent banner, and the
session's provenance labels say which key is live. The user must never mistake
an agent-driven session for their own.

---

## 3. Where enforcement lives

**Requirement AC-8.** The guard is the FastAPI middleware `require_app_token`
(`app.py:248`). It is the one funnel **every** client crosses — the IPC proxy,
main's own door, and any future agent or MCP client — and it already holds
`request.method` and `request.url.path`. It covers 63 of 64 routes; the single
exemption is a GET (`app.py:256`).

Enforcement anywhere else is **bypassable, and the bypasses are documented**:

- **`mainRequest` (`main/api.ts:117`)** — main's second door to the backend,
  applies none of the proxy's checks, and **already performs a PUT**
  (`wheel.ts:775`). So "all traffic goes through `api:request`" is false.
- **CDP `Runtime.evaluate`** — calls `window.grindstone.request` from a
  legitimate frame; `frameIsOurs` cannot tell the difference. This is exactly
  what the existing e2e harness does (`run.mjs:1445`).
- **CDP `Input.dispatchMouseEvent`** — Chromium treats it as **trusted**, so any
  `isTrusted` gate is defeated.
- **DevTools** — F12 / Ctrl+Shift+I are bound on **every** app view with no
  dev-only guard (`tabs.ts:305,426`), giving a console with the bridge in scope.
- **Non-`api:request` IPC** — `wheelui:act` (chart `delete`/`trim`/`clear`),
  `tabs:close`, `tabs:split`, `nav:goto`, `data:pickFile`.
- **The file picker** — `data:pickFile` feeds `POST /api/datamgmt/import`, which
  takes an **arbitrary absolute path** and opens it (`app.py:1039`). An agent
  that can click the picker chooses which local file the backend reads.

**Requirement AC-9.** The UI restriction (disabled buttons) is **cosmetic and
must be described as such**. The backend middleware is the enforcement.

**Requirement AC-10.** `POST /api/datamgmt/import` is blocked for agents
regardless of the read/write split, because it is an arbitrary-file-read
primitive.

---

## 4. What the agent can see

**Nothing is redacted today.** `scrub()` (`main/api.ts:179`) drops exactly three
key names — `token`, `access_token`, `refresh_token`. Everything else reaches
any caller that can read a page:

- real account equity, cash, buying power, options buying power, approved
  options level, PDT flag, day-trade count, account last-4
- the Alpaca `key_id` **last 4** (`GET /api/accounts` `key_hints`)
- absolute local filesystem paths to the multi-GB databases
- favourites, chart drawings, backtest specs and results, the whole local news
  store, and any page open in the embedded browser

The vault's own material is **not** reachable: no route returns a secret key, a
wrapped DEK, a KDF salt, or a password hash, and the DEK lives only in
`SessionStore`.

**Requirement AC-11.** The project already requires this for prompts — §6.3
guardrail 4: *"broker keys and account numbers never enter prompts (redacted
server-side)"*. Extend the same redaction to the agent surface: account numbers,
key hints and absolute paths are masked. If a value is not masked, the spec says
so explicitly rather than letting silence imply safety.

---

## 5. The order-entry invariant

Today the app **cannot place an order at all**: there is no order or position
code anywhere in the backend, every outbound HTTP call is a `GET`, and the gate
pins it (`selftest.py:272`).

So this toggle is currently a promise the code keeps by construction. That will
change when trading lands.

**Requirement AC-12.** When order entry arrives, an agent-driven session must be
structurally barred from it at the **broker-adapter layer** — the one path every
order must cross — not in the UI, and not by a route list that someone must
remember to update.

**Requirement AC-13 — widen the gate check.** `selftest.py:272` greps **only**
`brokers/alpaca.py`. It will not catch an order call added in a future
`brokers/tastytrade.py`. It must scan every adapter.

**Requirement AC-14.** This aligns with §6.3's existing rule, which is stricter
than anything here and is not weakened by this document: *"The MCP surface has
no order-submission tool — `draft_order` stages a ticket in the trade panel; a
human clicks confirm. No exceptions, including 'the user told the AI to
submit.'"*

---

## 6. `robots.md`

A document an agent reads **before** driving, so it does not have to infer the
app from clicking around.

**Requirement AC-15.** `robots.md` is **generated**, not hand-written, from the
two registries that already exist: the page registry (`search_mod.PAGES`, served
by `GET /api/pages` with a `ready` flag) and the route table (all 64 routes are
declared in one file). It is gate-checked against the live tables so it cannot
drift — the pattern this codebase already uses for the settings schema.

**Requirement AC-16.** It states at minimum:

- every page, its address, whether it is `ready`, and what it is for
- which routes are readable and which are blocked under agent control
- **that the data is thinner under a system key** — Basic tier, IEX equities and
  indicative options — so an agent does not file "SIP data missing" as a bug
  when it is the key working as designed
- which surfaces write despite being GETs (§1)
- the honesty labels the UI uses (`alpaca (indicative)`, `cached (fetched …)`,
  `your recorded data`) so an agent can tell a data-source label from a defect
- that it must not attempt to disable agent control

---

## 7. The surface itself — and what it can honestly replace

**Requirement AC-17.** A dedicated `agent:*` IPC channel, separate from
`api:request`, so the two carry different capability sets and can be
audit-logged separately. The read-only guarantee still lives in the backend
middleware (§3), because this channel will not be the only way into the process.

**Requirement AC-18 — it must read the RENDERED view.** This is what makes it
able to displace screenshot testing. The existing harness reads state over the
bridge and asserts on it; `optshot.mjs` exists **because UI truth was not
otherwise checkable**, and it has caught roughly six real rendering bugs that
stayed green through both the gate and the typechecker. An agent surface that
reads the API instead of the rendered view re-opens exactly the gap screenshots
were added to close.

So the surface exposes: an accessibility/DOM tree of the **live view**, element
references, click, type, key, scroll, and text extraction — the shape of a
browser automation tool.

**Requirement AC-19 — honest scope.** It can replace assertions about *what the
page says and does*. It cannot replace **pixel** verification — layout collapse,
a chart drawing into an 8-unit strip, a line the same colour as the gridlines.
Those were real bugs here and a DOM tree reports them as fine. Screenshots stay
for rendering; the agent surface takes over behaviour.

**Requirement AC-20 — audit log.** Every agent action is logged with timestamp,
action, target and result, viewable by the user. §6.3 already requires an audit
log and a kill switch for the AI surface; this is the same requirement.

---

## 8. Verification

1. **Enforcement is server-side** — with agent mode on, a blocked path is
   refused when called through `mainRequest` and through CDP `Runtime.evaluate`,
   not only through the UI. This is the check that proves §3.
2. **Fail closed** — with no system key configured, enabling agent mode refuses.
3. **The swap** — with agent mode on, `creds_for` returns the system key for
   every caller, including the recorder.
4. **Self-disable** — an agent attempting to turn agent mode off is refused.
5. **`robots.md` matches reality** — generated content is diffed against the
   live route and page tables; drift fails the gate.
6. **Redaction** — account numbers, key hints and absolute paths are masked on
   the agent surface.
7. **Order invariant** — the widened adapter scan (AC-13) covers every file in
   `brokers/`.
8. **Mutation-tested** — each of the above is broken deliberately and confirmed
   to go red, via `tools/mutate.py`. A guard nobody has seen fail is not a
   guard.
