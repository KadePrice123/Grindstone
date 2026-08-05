# Pickup notes — installers, then chart dimensions (2026-08-05)

Newest session first. Older sections are accurate history; where they quote a
gate count it is stale — **the gate is 41/41** as of 2026-08-05.

## Open work — the whole list

Every open item, in one place. Details live in the dated sections below.

**Charting** (details: [Not done yet — charting](#not-done-yet--pick-up-here-charting))

| # | Item | Size |
|---|---|---|
| ~~C1~~ | ~~Measure boxes still not clickable~~ — **DONE 2026-08-05.** Never a picking bug: an e2e click on a placed measure passed first try with Select armed. It was C2 wearing a disguise — you had to arm a tool nobody armed. | |
| ~~C2~~ | ~~Plain left-click should select~~ — **DONE 2026-08-05**, and the Select tool was removed with it. | |
| ~~C3~~ | ~~Drag to move~~ — **DONE 2026-08-05** (`f70e1ad`). Pixel-space translation with bar snapping, pan suspended on mousedown, trailing click swallowed, window-level move/up. Stage 1 of the constraints work. | |
| C4 | Per-drawing colour — `Drawing` has no style field | medium |
| C5 | Tickers wheel does nothing — three stacked causes | medium |
| C6 | No candle/line toggle — indicators are built inside the candlestick branch | medium |

**Backtesting** (details: [Not done yet — backtest](#not-done-yet--pick-up-here))

| # | Item | Size |
|---|---|---|
| B1 | Multi-underlying UI — backend already takes `underlying`; only the card's buttons are hardwired to SPY | small |
| B2 | TastyTrade / other recording adapters — anything writing `rec_chain`/`rec_bars` feeds backtests free | medium |
| B3 | Recorded-data quality guards — no holiday filter, no minimum-contract sanity check | small |
| B4 | Form coverage — params, sizing ladders, calendars are JSON-only; `/api/backtests/vocab` is unused | medium |
| B5 | Sweeps — engine vendored and tested, no API/UI | medium |
| B6 | Packaged build — runner needs its own entry point in the frozen exe (501 today) | medium |
| B7 | Timezone edge — fixed-EST fallback mislabels one hour around DST | small |

**Dimensions & constraints** — SolidWorks-style, in progress 2026-08-05

Shipped so far: drag-to-move (`f70e1ad`), derived chart time so a slope reads
the same on any timeframe (`118f8ba`), axis-locked draggable dimensions
(`576bfc3`).

The settled model, which everything below builds on — **do not relitigate**:
a dimension has the same shape as the line tools that already exist. An hline
is one price, a vline is one time; a dimension is **two prices at one time**
(vertical, measures a price gap) or **two times at one price** (horizontal,
measures a span). The single shared coordinate is also the drag handle, so no
separate offset field exists. A diagonal is a **slope**, in $ or % per hour of
**chart time**, where chart time is counted from candles — each step
contributes `min(real gap, one candle)`, so an overnight break costs one candle
rather than seventeen hours. **Nothing angular is ever stored.** Degrees are
not an input unit and not a stored one: the price scale drifts with no user
input at all (nothing pins `rightPriceScale`), so a stored degree would go
false while you merely scroll sideways.

| # | Item | Size |
|---|---|---|
| D1 | **Type a value to make a dimension a driving constraint** — the core of the ask. Each constraint is one scalar equation over an affine quantity, so it is closed-form, one pass; no iterative solver needed. Detect over-definition at CREATION on a speculative copy and refuse, naming the conflict, rather than at drag time days later | medium |
| D2 | **Drag a constrained object and the set follows** — the other half of D1. Needs blocked-drag behaviour: clamp at the last feasible position, keep the ghost tracking the cursor, paint the blocking constraint red. Never let the driver move while a constraint sits violated | medium |
| D3 | **Ctrl-select two entities + a hotkey mints a dimension** — ctrl-click already means "add to selection", so this needs no new picking code, just a hotkey that reads `selected` and requires exactly two | small |
| D4 | **Value entry**: a real focused `<input>` in the PAGE's float layer (reusing DrawEditor's Field contract), not in-chip digit capture — the cheap path has no backspace, no paste, no minus sign, and collides with Backspace-deletes-the-selection | small |
| D5 | **Diagonal/slope dimension form** — the unit and its arithmetic are already shipped and gate-proven; this is the dimension *kind* plus its entry | small |
| D6 | **Configurable hotkeys** — chart-scoped for v1. Truly app-wide means routing through main's `before-input-event` (the pattern `main/tabs.ts` uses for F12), which is a shell change, not a chart change | medium |
| D7 | **Persistence, and it gates D1** — nothing in the drawing engine survives a reload (`sessionStore` is a module-level Map; no drawing state reaches the backend). Two lines are cheap to redraw; a set of typed constraints is not, so constraints without persistence is arguably a worse deal than a plain measurement. Kade chose a small `chart_objects(key, doc, updated)` table — NOT the settings blob, which `_coerce` caps at 8192 bytes | medium |
| D8 | Log scale is **deferred by decision**; amend REQUIREMENTS FR-CHART-1 so it is not a silent broken promise. Under log, Δ$ and Δbars stay correct but `parallel` becomes false and $/bar stops describing the drawn line | small |

Refused by design, with reasons to give in the chip rather than silent no-ops:
perpendicular distance between two non-parallel lines (its data-space magnitude
is √(dollars² + bars²), not a quantity, and the lines cross so it is zero
somewhere on screen), perpendicularity as a constraint, anything touching a
circle (`ellipsePx` is this codebase's own in-tree proof the plane is not
isotropic), and a dimension from a drawing to itself.

**Platform and tooling** (found 2026-08-05, details in this section)

| # | Item | Size |
|---|---|---|
| P1 | **The gate is not offline** — it makes a live Alpaca call every run. Stub the transport, or no-op `kick_market_refresh` behind a test flag | small |
| P2 | **macOS is untested** — add a GitHub Actions matrix (`macos-latest` is free) so it is built and gated every push | small |
| P3 | Regenerate an Alpaca paper key and add it via Accounts — the old one was deleted with `env/` | trivial |
| P4 | Order entry — every adapter is `order_entry: False`; the trading milestone flips it | large |
| P5 | **e2e flake, cause unknown**: selecting the trend line mid-span failed 1 run in 6 (all seven `fanClick` offsets missed on an 8435-bar chart). A retry now absorbs it, but the underlying race is not understood — suspect the crosshair not having resolved when the click lands. Worth a real diagnosis before trusting a single green e2e run. | small |

Roadmap-level work (AI layer, TastyTrade adapter, session restore, sidebar
rail) is REQUIREMENTS.md §10, not this list.

Both installers were run end to end against genuinely clean machines, and the
test found three defects that a re-run on a working machine never would.

**Windows** — fresh clone on a box with neither Python nor Node: winget
installed both, Electron's binary postinstall was skipped and the fallback
fetched it, gate green. **206s.** The Node MSI is machine-scope, so it raises
a UAC prompt; that is unavoidable and worth saying in any install doc.

**Linux** — a *freshly created* Ubuntu 26.04 (WSL2), Python 3.14.4, no
`ensurepip`, no node. The installer recovered the missing system package by
itself and finished green in **103s**. Every pinned dependency had a cp314
wheel. The private-Node install (`~/.local/share/grindstone`, 54 MB, no root)
is the strongest part of that script.

What the clean machines exposed, in order of how much it mattered:

1. **The frontend typecheck was a false green.** `_frontend` resolved node
   three levels *above* the repo (`<workspace>/runtimes/node/node.exe`), so it
   only ever ran on the one machine that had that folder — and being `.exe`,
   it could never run on Linux or macOS at all. It printed "toolchain absent"
   and still counted `ok`. Confirmed in the wild on Linux, not just reasoned
   about. Now resolves via PATH, then the portable copy, then the POSIX
   installer's private Node; **and a missing runtime with typescript present
   is a FAIL, not a skip.** If a check can silently no-op, assume it does.
2. **`./install.sh --no-ui` hung forever** whenever sudo wanted a password —
   and `--no-ui` is the documented unattended mode. Closing stdin does not
   help: **sudo reads its prompt from `/dev/tty`, not stdin.** It sat in `S+`
   on pts/0 until killed. `gs_sudo_prefix` now answers "can I become root
   without blocking?" via `sudo -n`, treats already-being-root as needing no
   sudo (the container case, where sudo often is not installed), and otherwise
   bails in ~3s printing the one command to run.
3. **`Find-Python` rejected a working Microsoft Store Python** as an alias
   stub. The bare stub is dead, but the real interpreter lives in a package
   subfolder under the same root and builds a valid venv — so the installer
   declared "no Python" on a machine that had a good 3.12 and installed a
   second copy.

Also: the gate printed a full `BrokerError` stack trace on every clone,
because selftest boots the app and the background market refresh used
`LOG.exception`. Expected broker failures now log one line.

**Correction, and a finding worth acting on.** That 401/403 was first read as
a dead Alpaca key. It is not — the key in `env/alpaca.env` was tested
directly on 2026-08-05 and returns 200 on both the trading and data APIs.
The rejection comes from the gate's own fixture credentials, and that is the
interesting part: **the "offline" gate makes a live outbound HTTPS call to
Alpaca on every run.** `_auth_accounts` creates a throwaway profile with
fixture keys, the app's `kick_market_refresh` fires on a background thread,
and `AlpacaData._get` does a real `httpx.get` — a 401/403 status can only
come from a server that answered. So the gate is neither offline nor
hermetic: it is slower than it looks, behaves differently with no network,
and quietly depends on Alpaca being up. README and REQUIREMENTS both say
live connectivity belongs to `npm run e2e`, never the gate. Not yet fixed;
the honest options are to stub the transport under the gate or to have
`kick_market_refresh` no-op when a test flag is set.

Related, and now acted on: nothing in `code/` ever read `env/alpaca.env` —
credentials come from the encrypted DB (`accounts` + `secrets`, decrypted with
the profile DEK) via `market.alpaca_creds_for`. It was a leftover from before
Accounts existed, so **`env/`, `.env.example` and the gate's env-template
check were all removed on 2026-08-05** (gate 42 → **41**; `checkpoint.json`
follows). `_secrets()` still scans every tracked file for credential-shaped
strings, which is the guarantee that actually mattered. The Alpaca paper key
that lived there is gone with it — regenerate one from Alpaca's dashboard and
add it through Accounts, which is the only path the app supports.

Untested and should not be claimed otherwise: **macOS**. No Mac here. The
right fix is a GitHub Actions matrix (`macos-latest` runners are free) rather
than shipping a Linux container for Mac — a container gives an unsigned,
XQuartz-dependent, still-untested app, and does not produce the notarized
`.app` that Gatekeeper expects.

## Shipped and verified

**Install (pushed, `bfb7fe3`).** Double-click installers in the repo root:
`Install.cmd`, `Install.command`, `install.sh`. They install Python and Node
if missing, build, run the gate, make shortcuts. `setup.ps1`/`setup.sh` are
now thin `--no-ui --no-shortcuts` wrappers over the same code. Shortcuts point
at the Electron binary directly, so there is no console window.
Three latent bugs it flushed out, all fixed:
  - A fresh clone could not launch: `npm run start` is electron-vite PREVIEW,
    it serves `out/` and never creates it, and no setup script built.
  - `setup.sh` was mode 100644, so the documented `./setup.sh` never ran.
  - The bt engine's 120 known-answer tests had never run — two `@check`
    decorators stacked on one function registered both names against
    `_favorites_system`. `main()` now refuses two checks sharing a body.

**Browser tabs (pushed, `bfb7fe3`).** Permission blanket-deny replaced with a
tiered policy — denying `storage-access` was putting any site with an embedded
SSO iframe into an endless login loop. `disableDialogs` removed (it made
`confirm()` return false unconditionally). Downloads hand off instead of being
silently cancelled. Client-hint headers added to navigations (Electron ships no
ClientHintsControllerDelegate, so the document request claimed Chrome and
carried none of the hints real Chrome sends). Google identity hosts hand off to
the system browser — that list is deliberately TWO entries; every host added
costs the user their in-app tools. **F12 opens DevTools for the first time**
(frameless BaseWindows get no menu, so the built-in accelerator never applied),
and page console warnings/errors mirror into `data/logs/shell.log`.

**Charting (pushed, `dbc64c9`).** Five reported problems were one structural
fact: the engine had one picking function whose loop body was
`bucket().drawings`, feeding a `Drawing`-typed selection end to end.
`hitAny()` now widens picking to measures and pins; `hitTest()` is left
byte-identical because trim and measure-snap call it directly and their
vocabulary IS lines. `deleteSelected()`/`clickDelete()` sweep all three
collections with one doomed set (ids are globally unique — one `mkId`
counter). Escape gained a third rung that disarms the TOOL, and both pages
mirror the engine's tool back or the toolbar button goes dead. The wheel lock
now outranks chart context (the old comment said it outright: "Context BEATS
the locked default").

**Confirmed working by Kade's own testing:** Escape cancels the tool; the
Delete key deletes; the wheel lock survives a chart click.

## Not done yet — pick up here (charting)

> **Items 1 and 2 are DONE (2026-08-05).** Left-click in Pointer now picks any
> drawing, measurement or pin; the Select tool is gone entirely. Item 1 was
> never a picking bug — an e2e click on a placed measure with Select armed
> passed on the first attempt, and the label layer sat exactly on the canvas
> origin, so all three candidates below were wrong. The real cause was item 2:
> `handleClick` returned immediately in `pointer`, so a user who never armed
> Select saw nothing happen and reported it as "not clickable". Kept below
> because the reasoning is a useful record of how a mis-framed bug report
> survived a green gate — the check that should have caught it greps
> ChartDraw.ts for wiring that was correct the whole time.

1. ~~**Measure boxes are STILL not clickable.**~~ Reported after the fix shipped,
   with the Select tool active. The wiring all reads correct, so this needs a
   RUNTIME diagnosis, not more code review. Verified already: `handleClick`
   routes `select` → `clickSelect` → `hitAny`; `zoneDraft` is reset at the top
   of `render()`, pushed in `renderMeasure`/`renderPin`, published at the end;
   `this.labels` has inline `pointerEvents='none'` so clicks do reach the
   canvas; there is no early return between draft and publish. Candidates, in
   the order worth checking with F12 open:
     a. Coordinate space. `chip()` computes left/top against `pane.width/height`
        while `p.point` comes from lightweight-charts. If `this.host` is not
        the pane origin (a LEFT price scale would do it) every zone is offset.
        Log `hitAny`'s x/y beside `this.hotZones` and compare.
     b. `hotZones` empty at click time. Log its length in `clickSelect`.
     c. The `CLICK_SLOP_PX` pan discriminator swallowing the click — log
        whether `handleClick` is entered at all.
   Clicking a measure's CONNECTOR LINE (not its chip) exercises a different
   branch of `hitAny`; testing that first splits the problem in half.
2. ~~**No auto-selection on left click.**~~ This is the plan's Phase 3 and it was
   never implemented — the default `pointer` tool returns immediately at the
   top of `handleClick`, so nothing is pickable without arming Select first.
   Kade wants plain left-click to select. NOTE the trap: naively deleting that
   guard puts a full overlay rebuild on every crosshair move in the app's
   default mode, which is the "nothing repaints at rest" invariant
   (`selftest.py` greps for it). Needs a `hoverId` unchanged-id early-out.
3. ~~**Drag to move.**~~ **DONE 2026-08-05** (`f70e1ad`), as stage 1 of the
   SolidWorks-style dimensions work. Three things that each look like they work
   while being wrong, all now covered by the gate: translation must be in
   PIXELS (the x axis is affine in bar index, so a constant Δtime is not a
   constant Δx across a weekend); pan must be suspended on the mousedown, not
   at the slop threshold, because the chart pans on its own mousemove; and the
   click the library fires on the mouseup that ends a drag must be swallowed,
   because the 4px guard does not cover it and it lands at the DROP point.
4. **Per-drawing colour.** `Drawing` is `{id, kind, points}` — no style field
   exists, and one module constant `STROKE` feeds stroke, halo and handle fill.
   `spanToDrawing()` mints fresh drawings, so trim would erase any colour
   unless it carries the field over.
5. **Tickers wheel does nothing.** Three stacked causes: it collapses to a
   disabled placeholder when `symbols.length < 2` (the default is `['SPY']`);
   `chart-add` sources only `tabs.symbolTabs()` and never `s.favorites`, which
   is already fetched and on the session but dropped before `chartSegments`;
   and no search-to-add path exists. A radial wheel cannot host a text field —
   the search half belongs on ChartsPage's existing `.mc-add` form against
   `GET /api/search?q=`.
6. **No candle/line toggle.** Pure absence — `chartType` appears nowhere.
   Trap: SMA/EMA/vol/RSI are all built INSIDE the candlestick branch of
   `Chart.tsx`, so a naive switch drops every indicator.

A full verified plan for 2–6, with exact code and the selftest/e2e impact of
each, was produced 2026-08-04 and is worth regenerating rather than guessing.

## Related research — not part of this app (yet)

`Desktop/market/TinyAgent/` is a separate spike on small-model tool calling,
NOT version controlled and not a dependency of Grindstone. Relevant to future
Grindstone tooling:
  - **`grab/`** — a zero-dependency structured extraction library (click a
    table/card grid/chart, get JSON/CSV/TSV/Markdown, no per-site rules). This
    is the reusable part; it would suit pulling data out of pages opened in
    Grindstone's browser tabs.
  - **The load-bearing negative result:** deterministic rules scored 100% on
    all four measures across 11 DOM shapes; adding LFM2.5-350M *lowered*
    dataset-kind accuracy to 73%, and neither it nor LFM2-1.2B-Tool named a
    single unlabelled column correctly. On the isolated skill, 350M scored at
    chance (3/9) and was not order-stable; 1.2B got 8/9. Emitting a well-formed
    tool call and knowing what to put in it are different skills. Do not reach
    for a model where rules already work.
  - **Small-model gotcha worth remembering:** a bare scalar tool result is
    silently ignored — hand back `234` for `1280*0.15+42` and the model answers
    187.2. Results must be wrapped as `[{"name": …, "result": …}]`.

---

# Pickup notes — backtest data + form (2026-08-03)

Working notes for whoever continues this (written for Kade picking up from
home). Everything below the "shipped" line is verified and pushed; the list
after it is the honest remainder.

## Shipped and verified

- **App-owned backtest store**: `data/backtest_data/<UNDERLYING>.db`, created
  automatically (first status call is enough), both engine tables (`opt` +
  `bars`) in one file. Source resolution per run: Settings path → workspace
  `spy_options.db` (SPY only) → the app's store. `backend/btdata.py` owns
  schema + sync; `data_status()` reports source/coverage honestly.
- **Recorder → store sync**: last in-market-hours (ET) snapshot per trading
  day from `rec_chain`, finest recorded timeframe from `rec_bars`.
  Incremental via `sync_state`. Runs on 'recorded' source auto-sync in the
  runner subprocess first; manual sync = POST `/api/backtests/data/sync`
  (background thread, progress in status). Synced rows deliberately OUTLIVE
  recorder retention — the store is the long-term archive, rec_* the inbox.
- **One-click wiring**: POST `/api/backtests/data/setup-recording` creates
  hourly-chain + daily-bars jobs (idempotent; not gated on universe sync —
  see comment in app.py).
- **Form editor**: `BacktestSpecForm.tsx` compiles to the same spec JSON the
  validator/AI path uses; `tryDecompile` refuses (→ JSON mode) rather than
  drop features. Gate at the time: `SELFTEST OK 38/38` (41/41 now), incl. an end-to-end check that
  builds synthetic arbitrage-clean chains in rec_chain, syncs, and runs the
  REAL runner subprocess on the result.

## Not done yet — pick up here

1. **Multi-underlying UI**: the Data card and its two buttons are hardwired
   to SPY (`syncNow`/`setupRecording` in BacktestPage.tsx). Backend already
   takes `underlying` everywhere (status query param, BtDataIn, per-symbol
   store files); the page needs a small underlying picker that re-fetches
   status and passes it to both POSTs. Run-time resolution already follows
   the spec's `underlying`, so a TSLA spec + TSLA recordings works today —
   only the card's buttons don't know it.
2. **TastyTrade (and other) recording adapters**: the recorder is
   Alpaca-only. Any new provider that lands rows in `rec_chain`/`rec_bars`
   feeds backtests with ZERO further work — the sync doesn't care who wrote
   the rows. Same story for the hosted-data-API idea: write into the same
   two tables (or straight into `opt`/`bars` with the mapping in
   `btdata.py`'s module docstring).
3. **Recorded-data quality guards**: the sync takes the last in-hours
   snapshot as "the day". No holiday filter (a stale half-day quote set can
   become a thin trading day) and no minimum-contract-count sanity check per
   snapshot. Both belong in `btdata.sync_from_recorded`.
4. **Form coverage**: params, sizing scale ladders, expression max_active,
   raw exit-rule lists, calendars (`expiration_of`) are JSON-only — by
   design for now. The vocab endpoint (`/api/backtests/vocab`) is unused by
   the UI; autocomplete in the JSON editor would be the next friendliness
   win.
5. **Sweeps**: engine module is vendored and tested; no API/UI yet.
6. **Packaged build**: the runner needs its own entry point in the frozen
   exe (start() raises RunnerUnavailable → 501 today, see backtests.py).
7. **Timezone edge**: ET conversion falls back to fixed EST when tzdata is
   missing; `tzdata` is in requirements so venv installs are exact. The
   fallback mislabels one hour around DST — harmless for daily snapshots,
   worth removing once packaging bundles tzdata.

## Gotchas that will bite you if forgotten

- Sidecar children MUST get `stdin=DEVNULL` (watchdog pipe wedge, see
  backtests.py comment) and never a piped stderr nobody drains.
- Push failures with `curl 55` on this workspace's machines:
  `git config http.sslBackend openssl` (endpoint AV kills schannel uploads).
- Adding OR removing a selftest check = bump `checkpoint.json`'s expect string
  in the SAME commit (currently **41/41**). The count is a sentinel: it exists
  so a crash mid-run cannot look like a pass.
