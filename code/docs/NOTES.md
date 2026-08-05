# Pickup notes — install, browsing, charting (2026-08-04)

Newest session first. The backtest section below is still accurate history but
its gate number is stale: the gate is **42/42** now, not 38/38.

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

1. **Measure boxes are STILL not clickable.** Reported after the fix shipped,
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
2. **No auto-selection on left click.** This is the plan's Phase 3 and it was
   never implemented — the default `pointer` tool returns immediately at the
   top of `handleClick`, so nothing is pickable without arming Select first.
   Kade wants plain left-click to select. NOTE the trap: naively deleting that
   guard puts a full overlay rebuild on every crosshair move in the app's
   default mode, which is the "nothing repaints at rest" invariant
   (`selftest.py` greps for it). Needs a `hoverId` unchanged-id early-out.
3. **Drag to move.** Genuinely unimplemented: only two DOM listeners exist in
   the whole 1525-line engine (`mousedown` as a pan discriminator, `keydown`)
   and neither drags. ~140 lines, the largest remaining piece.
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
  drop features. Gate at the time: `SELFTEST OK 38/38` (42/42 now), incl. an end-to-end check that
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
- Adding a selftest check = bump `checkpoint.json` expect string in the SAME
  commit (currently 38/38).
