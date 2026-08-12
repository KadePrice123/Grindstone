# Selling puts as insurance — the scanner design

One page, one graph, one question: **is the premium offered bigger than the claims this risk class has actually paid?** A cash-secured put is a written insurance contract — the credit is premium income, assignment is the claim, `strike − settle` at expiry is claim severity — so "worth taking" is an actuarial question: offered premium against measured expected loss. This app is unusually placed to answer it because it owns a claims ledger (`options_history.db`, daily EOD chains) and every settlement (the bars ladder), and it has already caught the model alternative lying (~2× optimistic vs real chains). Produced 2026-08-12 by a design panel (three proposals, one judge with tree access, one synthesis) that validated every disputed claim against the live tree and the archive itself. **FILTERING AND MEASUREMENT ONLY: no order surface exists or is implied anywhere in this design.**

Archive facts, verified today by direct query: `hist_chain` holds 7 underlyings (GLD, SPXL, SPY, TQQQ, USO, XLE, XOP), all windowed 2025-07-30 → 2026-08-05/12; SPY has 261 sessions, 3,049,699 rows, delta present on 99.9% of put rows (1,523,149 of 1,524,852). `hist_meta` records `months: 12` of raw rows kept from a 39.2M-row vault reaching back 16 years, with deep history baked only into `hist_spread_pct`. Two consequences drive everything below: (1) a runtime measurement over `hist_chain` is a ONE-YEAR, ONE-REGIME measurement and every caption must say so; (2) the loader precedent (`hist_spread_pct`) is the natural v1.1 home for the all-regime measurement.

---
## Recommendation

Synthesis = **Proposal B's product spine carrying Proposal A's measurement engine**, with two grafts from C. The page is `insure.gs`, no symbol argument, whose upper half is one square scatter — **the insurance line**: x = what this risk class has actually cost (measured expected claim, % of strike, annualised linearly over sessions), y = what the market pays today (offered mid ÷ strike, same unit, same scale, through the SAME `optgrid.ts` functions the heatmap uses). Because both axes are the same quantity in the same unit, the fair price for every favorite at once is the single 45° diagonal y = x; the vertical gap IS the annualised edge; "how much credit do I need" is literally the dot's x-coordinate. The measurement engine is a new pure-stdlib `backend/insurance.py` in the opthist mold, and it adopts A's statistical core wholesale because it is simply more honest than the rivals': **cluster means over expirations** (trials entered on 15 consecutive days into one expiration share ONE settlement draw — the honest sample size is distinct expirations, `n_exp`, not entry days), **two ledgers** (a zero-bid row still tells the truth about whether a strike was breached, but only a sellable row has a P&L), **loss episodes** (9 losing entry-days that are one bad week report as 1 episode, so overlap cannot impersonate independence), and the **zero-claims rule** (a class with no observed claims has an UNMEASURABLE required credit, never a zero one — the rule-of-three ceiling is reported instead, because a measured zero would paint the far wing as free money, the exact good-looking lie the archive was loaded to kill). From C: the dot's **claim-frequency ring** (assignment risk visible on the diagonal plot without a second axis) and **win-at-offer** (the win rate re-priced at today's credit, not just at historical credits — the direct answer to Kade's win/loss question at the price actually on the table).

Arbitrated on tree evidence: **rendering is a dumb SVG component over gate-probed pure math** (B), not lightweight-charts (A) and not canvas (C) — OptCharts' header confines the house engine to time-like panels (its hand-rolled-SVG lesson was about crosshair/pan/zoom, none of which a static scatter has), the synthetic-lattice trick would quantize a continuous % axis and nudge colliding dots to fabricated positions on a page whose religion is never inventing a number, and `OptHeatmap` already establishes the out-of-engine precedent (DOM surface, every number from pure `optgrid`). **The expectancy cache is a table in market.db** (A), not an in-process-only cache (B) and not a new database file (C) — market.db's own header says it holds "recorded data: bigger, write-heavy, every byte re-fetchable, WAL" (the no-WAL synced-folder rule C cited belongs to app.db, verified in db.py:107), and `bar_cache` is the exact precedent including the schema-per-connect `user_version` bump. **Risk classes are keyed on entry |delta| with OTM% fallback** (A/B) — delta is present on 99.9% of archived put rows, so C's missing-field-bias argument is empirically weak here, and delta-as-underwriting-variable produces the killer readout ("the market's Δ.20 SPY puts actually got assigned 8.9% of the time"), which is the BS-was-2×-optimistic finding turned into a permanent instrument. **Offered = mid** for plotting and ranking (B/C — `midOf` is the house convention, the heatmap's colour, the tasty engine's fill), with the bid-edge printed in every row because a wide market can turn a positive mid-edge negative. Rejected after scrutiny: A's delta-x/crosses plane (two populations to reconcile, no universal fair line — calibration survives as a labeled gap in the hover card instead); A's separate `insuremath.ts` twin file (appending exports to `optgrid.ts` makes convention-fork impossible instead of gate-patrolled); C's builder daemon + queue + histogram blobs + rebuild endpoint (machinery a per-underlying few-second sweep does not need — per-expiration aggregates in the cached payload answer `win_at` exactly); C's `n_indep = days/mean_dte` heuristic (A's expiration clustering measures the same thing without a fudge factor); B's trial-level means (dense months outvote sparse ones — expiration-equal weighting fixes it). Verified corrections carried into this document: the gate pin is `SELFTEST OK 67/67` today (not 69/69), so the new check moves it 67→68; the bars ladder order is bar_cache → rec_bars → Yahoo (app.py:980, "ahead of rec_bars"); options fetches carry BOTH a 45s in-memory per-tuple TTL and the optional sqlite cache governed by `options_cache_minutes` (default 15).

---
## The actuarial frame — vocabulary the whole page speaks

**Policy** — one short cash-secured put: collect `credit`, post `strike` of collateral (`capitalFor` = strike; the ×100 multiplier cancels out of every ratio, per its own documented reasons — every % on this page reads against every % on the Opt page). **Claim** — settlement ITM: `S_T < K` strictly at expiration (`settle == strike` is a win, pinned in the gate). **Severity** — `max(0, K − S_T) / K`, the claim as % of collateral. **Pure premium** — measured `E[severity]` per policy for a risk class: the credit at which writing this insurance historically broke even, before any profit. This IS "the credit I need to make the risk worthwhile." **Risk class** — a (DTE band × entry-|delta| band) bucket per underlying. Delta is the UNDERWRITING VARIABLE that names the class; what happened inside it is measured — using delta to sort is not using delta to price, and the whole point is that the measured claim rate of the Δ.15–.25 class gets to disagree with 15–25%. **Assignment odds** appear as TWO labeled numbers, never blended: the measured claim frequency of the class ("finished ITM in 6.4% of 27 expirations") and the live `|delta|` ("market says 20%"; `Δ —` when the indicative feed omits it — the Opt page's first-class-missing-greek rule). **Edge** — offered − required, same units, annualised only for cross-tenor comparability.

---
## The expectancy engine (`backend/insurance.py`)

Pure stdlib, no numpy (the tripwire stands), read-only over the archive (`mode=ro` via opthist's `_open` idiom), no writes anywhere near the loader's file.

**What counts as one trial.** Every archived (entry date `d`, put contract `(K, exp)`) row is a candidate trial: sell at that day's EOD quote, hold to expiry. Every entry day deliberately — a daily seller genuinely could have entered any day, so entry days are the honest universe of opportunities; the dishonesty of overlap lives in the SAMPLE SIZE, and it is repaired there (clustering, below), not by discarding data. Hold-to-expiry only in v1: it is the insurance framing itself (the claim happens at expiry or it doesn't), it is deterministic (no exit-policy parameter to argue about — a 50%-profit-take variant is a strategy backtest, the btengine's job), and the daily marks to support managed exits exist in `hist_chain`, so that is a v1.1 extension, not a redesign.

**Two ledgers, two ns, both reported.** A trial enters the CLAIMS ledger if its settlement is computable, whether or not its entry quote was two-sided — a zero-bid row still tells the truth about whether that strike was breached. It enters the PRICED ledger (win/loss statistics) only if entry `bid > 0 and ask ≥ bid` — a policy that could not actually have been sold has no honest P&L (`midOf`'s rule, mirrored in Python at entry).

**Settlement.** `S_T` = the underlying's daily close ON the expiration date, from an app-assembled closes map in the app's own ladder order (verified app.py:980–1016): `bar_cache` 1Day → `rec_bars` → Yahoo keyless daily — NEVER a live Alpaca fetch inside a sweep (a measurement must not spend rate limit), and each close records its source. If the expiration date has no close for the underlying AND the date is a non-session (weekend/exchange holiday — no close exists for anyone), the last close within 3 calendar days BEFORE it settles the trial and the used date is recorded (holiday-shifted expiries: the prior session's close IS the last price that contract ever saw). If the date was a session and the close is simply missing from our stores, the trial is **unsettleable — counted, never approximated against a neighboring day** (Thursday's close is a different contract outcome). Entry spot `spot_d` = close on the entry date (the archive is EOD; quote and spot share a snapshot cadence). **Corporate-action guard**: a >40% day-over-day jump in the close series anywhere in `[entry, expiration]` marks the trial **suspect** (a split breaks strike/close comparability) — counted, excluded, reported. **Self-check**: on a contract's DTE-0 row, `mid ≈ intrinsic(S_T)`; disagreement beyond tolerance marks that symbol's settlement source suspect and the scan says so rather than shipping numbers built on it. This is both a gate fixture and a scan-time sanity counter.

**Censoring — the tail is missing data, not a quiet trade.** Statuses besides `settled`, all counted per class and surfaced: **pending** (expiration after the last known close — excluded from every statistic; the one bias this creates is stated in the caption: an archive ending mid-drawdown holds its troubled trials open, so a window ending in a red month under-reports losses); **no-close / no-spot**; **suspect**.

**Clustering and the honest n — the statistical core.** Trials entered on 15 consecutive days into one expiration at one class share ONE settlement draw: if that Friday gaps down, all 15 "lose" together. `n_days` is real (15 real opportunities) but it is NOT the sample size of the risk estimate. Every class statistic is a **cluster mean over expirations**:

```
E        = distinct expirations with ≥1 settled trial;  n_exp = |E|
claim_T  = mean(claim_i) over trials settling at T      claim_i = 1 if S_T < K else 0
sev_T    = mean(sev%_i)  over trials settling at T      sev%_i  = max(0, K − S_T) / K
claim_freq        = mean over T of claim_T              (each expiration one vote)
expected_loss_pct = mean over T of sev_T                (THE pure premium; None when Σclaim_T == 0)
ci90              = Wilson(Σ claim_T, n_exp, z=1.645)   (fractional-k tolerant; coarse, clustered, honest)
```

Equal-weighting expirations also fixes a quieter bug: densely-archived months would otherwise outvote sparse ones. Losses additionally report as **episodes** — runs of consecutive claiming expirations merged — so "9 losses" that are one bad week print as "9 losses in 1 episode." Displayed wherever a measured number appears: `n_exp`, `n_days`, episodes, and the window `first → last`. **Confidence tiers drive rendering and ranking**: `n_exp ≥ 20` solid, `8–19` thin (hollow dot, dimmed row), `< 8` unplotted and EXCLUDED from the ranked "worth a look" list — thin data may be looked at, never ranked by.

**Risk classes**, per-underlying ONLY (SPY's claims say nothing about SPXL's — a 3× fund's tail is a different animal; cross-symbol pooling is refused so thin symbols stay honestly thin):

```python
DTE_BANDS   = ((4,10), (11,21), (22,38), (39,60))       # calendar days at entry; half-open [lo,hi+1)
DELTA_BANDS = ((0.05,0.15), (0.15,0.25), (0.25,0.35))   # |delta| at entry — primary; half-open [lo,hi)
OTM_BANDS   = ((0.01,0.03), (0.03,0.06), (0.06,0.10), (0.10,0.16))  # fallback, (spot_d−K)/spot_d
```

Coarse on purpose: with ~52 weekly expirations a year, the `n_exp ≥ 20` solid tier is reachable inside a band this wide and unreachable in half-width bands — finer bands would manufacture hollow dots everywhere. DTE 0–3 excluded by design (EOD granularity misprices the 0-DTE business; annualised front-week credit is capped noise per `ANNUAL_CAP`'s own argument). Band edges are half-open and gate-pinned so Δ = 0.15 lands in exactly one bucket in every future refactor; the server matches candidate → class, the client never re-derives a bucket (no cross-language edge drift). Rows whose entry delta the archive omitted classify by OTM% — first-class, labeled `class_mode: 'otm'`, never silently mixed with delta classes. **Calibration rides along free**: each class carries `implied = mean |delta_i|` at entry beside `claim_freq` — "market said 20%, archive says 8.9% across 27 expirations" on every hover card.

**Priced-ledger statistics** (over trials with a real entry credit, reported with their own `n_priced`, plus cluster-level analogues):

```
credit%_i = mid_i / K                     (entry mid — the house transactable number)
net%_i    = credit%_i − sev%_i
win_rate  = share of expirations T whose sev_T < median credit%           (clustered)
wl_ratio  = avg_win / avg_loss over net%_i                                 (Kade's ratio, at credits history offered)
severity_given_claim: mean, p95, worst (with its expiration date — the claim to look in the eye)
win_at(offer) = share of expirations T with sev_T < offer%                (C's graft: today's credit, history's claims)
```

`win_at` is computable at any offered credit because the cached payload stores the per-expiration aggregates `(T, n_t, claim_T, sev_T)` — at most a few hundred small tuples per class, no histogram blobs, no trial rows retained.

**Pure, gate-probed functions** (no I/O; the connection is a parameter so the gate hands it a fixture db):

```python
def trials(rows, closes, *, today) -> list[dict]         # per-trial dicts + status:
    # 'settled'|'pending'|'no-close'|'no-spot'|'suspect'
def class_of(dte, delta, otm_pct) -> tuple | None        # half-open bands, delta-primary
def class_stats(trials) -> dict[class_key, dict]         # cluster aggregation; all fields above
def win_at(expiries: list[tuple], offer_frac: float) -> float
def wilson(k: float, n: int, z: float) -> tuple[float, float]
def sweep(con, underlying, closes) -> dict               # one SQL pull + one aggregation pass
def daily_closes(state, symbol, start, end) -> tuple[dict, str]   # bar_cache → rec_bars → yahoo
```

**Where it runs and what is cached.** `sweep` is one PK-prefix range scan of that underlying's put rows (~1.5M for SPY) plus one Python pass — a few seconds, cold. Results persist in **market.db** (the app-owned, WAL, re-fetchable-data store — the `bar_cache` precedent, schema-per-connect, `user_version` bump):

```sql
CREATE TABLE IF NOT EXISTS insure_expectancy (
  underlying  TEXT PRIMARY KEY,
  fingerprint TEXT NOT NULL,   -- "{hist_meta.built_at}|{max hist date for sym}|{max close date}"
  computed_at TEXT NOT NULL,
  payload     TEXT NOT NULL    -- class_stats JSON incl. per-expiration aggregates
);
```

Fingerprint mismatch → background recompute, single-flight per underlying, one thread, `LOG.info` duration; the scan meanwhile serves the stale payload LABELED with its `computed_at` and window, or `expectancy.status: "measuring"` when none exists. The archive changes at most once per day, so steady-state cost is one sweep per underlying per archive day; interactive scans are cache reads plus chain fetches the existing TTLs already govern. No new database file, no writes to the loader's archive, no builder daemon.

---
## Required credit and edge

**`required_pct = expected_loss_pct` — the measured pure premium, nothing else.** `edge_pct = offered_pct − required_pct`, where `offered_pct = mid / K` (`midOf` verbatim: the number the heatmap colours by, the tasty engine's fill). Deliberately excluded, with the reasons printed where the numbers appear: **spread loading** — entry is the only transaction a hold-to-expiry trial has, and the bid-edge column (below) shows what hitting the bid costs instead of smuggling execution into a measured quantity; **risk-free yield** — collateral earns its yield whichever put is sold or none, so it translates every dot and the fair line together and can reorder nothing (one line in help, out of the arithmetic); **margin models** — `capitalFor`'s documented refusal stands. A candidate with no bid has no mid, no offered, no dot — counted in `excluded.no_bid`, never priced at half the ask.

Annualisation enters for COMPARABILITY only, never for fairness (both sides of one candidate share a tenor, so the scale factor cancels out of the sign of edge), linearly over sessions through the exact functions `optgrid` already exports:

```
offered_annual  = annualise(mid/K,            tradingDaysTo(today, exp))   # ≡ annualYieldOn — the heatmap's own number
required_annual = annualise(required_pct,     tradingDaysTo(today, exp))
edge_annual     = annualise(edge_pct,         tradingDaysTo(today, exp))
```

**The zero-claims rule — the most load-bearing decision in this document.** A class with `claim_freq == 0` returns `required = null` with the reason: *"no claims in `n_exp` expirations — one year cannot price this tail"*, plus the rule-of-three ceiling the row prints: *"0 claims in 58 expirations caps the true claim rate near 5% at 95% confidence — severity unknowable."* A measured zero would make every deep-OTM put show infinite edge; refusing to quote pushes the eye toward strikes where the data can speak.

The caption, one sentence: *"Offered 0.52% of strike at the mid ($322/contract); puts in this class (Δ.15–.25, 22–38 DTE on SPY) cost sellers 0.31%/policy in claims across 27 expirations, 2025-07-30 → 2026-08-11 (2 claims, 1 episode) — edge +0.21%/trade, +1.8%/yr on the strike."* Dollars via `CONTRACT_MULTIPLIER`, because "$322" is money and "0.52" is a quote — the exit view learned that already.

---
## The one graph — the insurance line

**x = required credit (measured, annualised %/yr on strike). y = offered credit (today's mid, annualised, same unit — literally `annualYieldOn`). The fair line is y = x**, one universal 45° diagonal for all favorites at once, TRUE BY CONSTRUCTION because both axes are the same quantity in the same unit through the same function. Above the diagonal the market pays more than the risk has cost; the vertical gap is the annualised edge; the dot's x answers "how much credit do I need." The plot area is kept SQUARE (equal % per pixel both axes) so the diagonal is visually 45° and distance above it reads honestly. Axes auto-scale, capped at 60%/yr with an "n beyond cap" count — never a silent clip.

Why not the alternatives, judged: **delta-vs-yield with measured crosses** (Proposal A) keeps the risk axis but has no universal fair line — fair value at Δ.20 depends on severity, which differs per symbol and tenor, so the reader reconciles two populations before knowing anything; the calibration insight it carried survives as the hover card's labeled gap ("assigned 8.9% · Δ said 20%"). **Per-symbol fair curves** — three or four measured class points per symbol do not earn a continuous curve; drawing one implies interpolated knowledge the archive does not have. **Edge-ranked bars** — answers "which" fastest but amputates risk entirely; it survives as the ranked table, which is the same information in list form.

**Dots.** One per plotted candidate. Hue by symbol (slot palette in favorites order; legend chips toggle symbols). **Ring width in three discrete steps encodes measured claim frequency** (<10%, 10–25%, >25%) in the established odds ink — assignment risk visible on the plane without a second axis (C's graft). Fill = confidence tier: solid ≥20 expirations, hollow 8–19, <8 unplotted (a dot IS a claim of measurement; three expirations is not one). Top three dots by edge carry small text labels; the rest label on hover. Hover card: OCC, DTE, strike (OTM%), bid×ask + spread%, offered/required/edge (per-trade and /yr), measured claim freq + `n_exp` + episodes, `Δ` or `Δ —`, win-at-offer, worst claim + date, class identity ("measured over Δ.15–.25 at 22–38 DTE"), window. **Click → `openTab('opt:SYM:OCC')`** — the deep link verified in urls.ts: the scanner finds, the Opt page interrogates; no duplicated charts.

**Deliberately NOT plotted, each counted in the honesty line under the caption**: no-bid candidates; zero-claim classes (an x of 0 would render the far wing as free money); thin classes (<8); symbols with no archive. All appear in the table with named reasons.

**Rendering: `InsureScatter.tsx`, a dumb SVG component over gate-probed pure math** — the `OptHeatmap` pattern (its header: "every number comes from optgrid.ts, which is pure and gate-probed; this file is the surface only"). Not lightweight-charts: the house engine's monopoly covers time-like panels (OptCharts' hand-rolled-SVG lesson was about a chart imitating the candle chart's crosshair/pan/zoom — a static bounded scatter has none of that contract), and the synthetic-lattice trick would quantize a continuous rate axis and shift colliding dots to fabricated positions. Nearest-dot hit test in the component; no pan, no zoom, no crosshair.

---
## The scanner — scope, candidates, fetch shape

**Universe.** Favorites of kind `'symbol'` (the app's existing definition of "my tickers") ∩ archive coverage for the measured side; ALL symbol-favorites get a row (a starred symbol absent from the archive renders honestly: *"NVDA — not measurable: no archived chains for it — record chains from Data management, or import"*, `NO_DB_REASON`'s vocabulary, cross-linked — never silence). No favorites → an empty state pointing at the star. Cap 16 symbols per scan with honest `skipped` rows beyond it.

**Candidate grid, bounded on purpose — one candidate per risk class per expiration.** Every strike inside a class shares one measured expectancy, so plotting thirty of them adds dots, not information; "show me every strike" is the Opt page's job, one click away. Per symbol: expirations = listed dates nearest {7, 14, 30, 45} calendar DTE within a 4–60 window (from the chain response's `expirations` field, deduped — ≤4). Per expiration: the priced OTM put nearest each anchor of Δ {.05, .10, .15, .20, .25, .30, .35} when deltas are present, else OTM% {2, 4, 7, 10, 13, 16}% (the fallback is first-class and the row says which matching it used — the `series_history` discipline). ≤28 candidates per symbol, ≤~200 across favorites, ~100–150 dots after no-bid drops — a readable plot, not a chain dump.

**Fetch shape.** Server-side through `options_mod.fetch` (same 45s in-memory per-tuple TTL, same optional `options_cache_minutes` sqlite cache, same refusal vocabulary): one narrow discovery fetch per symbol (strikes 0.95–1.00 × spot, full 4–60 window — cheap, carries `expirations`), then one windowed fetch per targeted expiration (that date exactly, strikes 0.70 × spot → spot, `right=P`) — per-expiration windows stay far under `MAX_ROWS = 400`, so nothing truncates silently mid-scan. Five upstream calls per symbol worst case, all TTL-absorbed on the refresh tick. Spot = last daily close from the bars ladder, source named; no spot → that symbol refuses ("no price for X — the strike window needs one").

**Cadence.** The page fetches PER SYMBOL (`/api/insure/scan?symbol=`), staggered 4-concurrent on mount — dots land symbol by symbol, a slow symbol delays nobody, a failed one fails alone with its reason. 60s re-poll (the TTLs make it cheap); a 2.5s poll only while any symbol reports `measuring` (the DataPage job cadence); focus/visibility bump like OptPage; manual refresh. Expectancy is never re-swept by polling — the fingerprint is the only invalidator.

---
## The page — `insure.gs`

No symbol argument; the favorites list IS the argument. Workstation shell (`.page insure-page` — the opt-page full-width override; nothing here owns leftover space, so not `.page-chart`). Rows, top to bottom:

1. **Head strip** — h1 "Insure" + the thesis in eight words ("selling puts, priced like insurance") + archive badge ("your archive · 2025-07-30 → 2026-08-12 · 7 symbols") + "n of m scanned · k measured" + refresh + a measuring chip while any sweep runs.
2. **The graph card** (~55% height) — `InsureScatter`, the diagonal dashed and labeled "fair — pays what it has cost", the caption sentence beneath naming source and window, and one honesty line when applicable: *"9 candidates have no measured price (zero claims this year), 3 have no bid, 2 classes too thin to plot — listed below, not plotted."* Plus the window's character, one measured number, no model: *"measured over a window whose worst drawdown was −12%; a year without a crash under-prices crashes."*
3. **The ranked table** — sorted by `edge_annual`, four hard sections that never interleave: **Worth a look** (edge > 0, solid tier), **Pays less than it has cost** (edge ≤ 0 — seeing WHICH favorites are currently bad insurance to write is half the point), **Tail unpriced** (zero-claim classes, with the rule-of-three sentence), **No verdict** (no bid / no archive / no chain / thin evidence, each reason verbatim). Columns: contract ("SPY 620P · Sep 18 · 37d") · offered ("$322 · 0.52%", bid beside) · **need** ("0.31%") · edge ("+1.8%/yr", bid-edge beside) · odds ("assigned 8.9% · Δ said 20%") · win@offer · n ("27 expirations · 1 episode") · worst ("−4.1% on 2026-04-17"). Rows expand to the detail card (W/L sentence in Kade's phrasing: *"won 53 of 58 expirations (10.6 : 1); when it lost it cost 1.9% of strike against your 0.52% credit"*, spread%, p95, class identity, censored counts). Row click and dot click → `openTab('opt:SYM:OCC')`.

**Honest states, enumerated as findable nodes**: archive absent → `NO_DB_REASON` verbatim page-level; no creds → the measured side still renders per symbol ("the archive can price the risk — only a live chain can say what it pays today: *no data key — add an Alpaca account…*"), keeping the page useful and provable offline; per-symbol chain failure → that row carries the endpoint's reason; favorite not archived → the not-measurable row; `measuring` → labeled, never a spinner-forever, and stale payloads serve LABELED with their `computed_at`.

---
## Backend — endpoints and orchestration

`backend/insurance.py` (pure functions + sweep, above) and thin orchestration in `app.py` — both routes in `create_app`, `s=Depends(current_session)`, X-App-Token free, refusal-as-200 with `LOG.info` reasons, ValueError → 422:

```
GET /api/insure/status
  → { available, archive: {source, built_at, window, underlyings},
      favorites: [{symbol, in_archive}], measuring: [sym], reason? }
      # cheap; the page mounts on this, then fans out one scan per symbol

GET /api/insure/scan?symbol=SPY
  → { symbol, available, reason?,
      spot: {price, date, source}, chain: {source, age_seconds},
      expectancy: { status: "ready"|"measuring"|"stale"|"none",
                    computed_at?, window?: {first,last}, sessions?,
                    censored?: {pending, no_close, suspect}, settle_sources? },
      candidates: [ { occ, expiration, dte, strike, bid, ask, mid, delta, otm_pct,
          offered_pct,                       # mid/K; null + reason on no bid
          class: {mode: "delta"|"otm", dte_band, band} | null,
          measured: { n_exp, n_days, episodes, claim_freq, ci90, implied,
                      expected_loss_pct,     # null on zero claims, with reason
                      required_pct, severity: {mean, p95, worst, worst_date},
                      win_rate, wl_ratio, win_at_offer, n_priced,
                      window: {first, last} } | { available: false, reason },
          edge_pct, tier: "solid"|"thin"|"none"|"unmeasured" } ],
      excluded: { no_bid, zero_claim, thin } }
```

The server returns MEASURED raw fractions and live quote fields; **annualisation is deliberately absent from the response** — every displayed %/yr derives client-side through `optgrid.ts`, so this page, the heatmap, and the Opt history chart can never quote two conventions. A drill endpoint (`GET /api/insure/expectancy?symbol&dte&delta|otm_pct`) exposes one class's full stats for the detail card and future Opt-page reuse.

---
## Frontend + touch list

**`optgrid.ts` additions** (appended to the existing pure, import-free, node-probeable file — NOT a new module, so the annualisation convention cannot fork):

```ts
export interface MeasuredClass { nExp; nDays; episodes; claimFreq; impliedDelta;
  expectedLossPct: number | null; winRate; wlRatio; winAtOffer: number | null;
  worst: { claimPct: number; expiration: string } | null; first: string; last: string }
export function requiredCreditPct(m: MeasuredClass): number | null   // null when zero-claims
export function edgePct(offered: number | null, required: number | null): number | null
export function confidenceOf(nExp: number): 'solid' | 'thin' | 'none'
export function insurePoint(offeredPct, requiredPct, today, expiration):
  { x: number; y: number; edgeAnnual: number } | null
  // both axes through annualise + tradingDaysTo — one scale factor, applied twice
export function claimRing(claimFreq: number): 0 | 1 | 2                // <10% / 10–25% / >25%
```

New files: `pages/InsurePage.tsx` (mount → status → staggered per-symbol scans, per-symbol state so failures are local, 60s tick + measuring poll), `components/InsureScatter.tsx` (dumb SVG: axes, diagonal, dots, rings, labels, hover card, `onPick`). CSS `.insure-*` in charts.css.

**Touch list** (recon-verified against urls.ts / search.py today): `urls.ts` — `PAGES` += `'insure'` (line 22; `isKnownPage`/main-process tabs auto-correct); `PAGE_ROUTES` += `insure: 'insure'` (it IS a no-arg page; the opt exception does not apply); `BARE` aliases `insurance` and `puts` → insure; `gsRoute` case; `gsAddress` case → `'insure.gs'`. `App.tsx` — Route union `{ name: 'insure' }` + `parseRoute` + `routeKey`. `ContentApp.tsx` — import + `meta()` `{ title: 'Insure', icon: 'insure' }` + body case (no per-tab arg special-casing — no symbol; the opt trap does not bite). `icons.tsx` — `PAGE_ICONS.insure` (shield). `backend/search.py` — PAGES row `{key:'insure', title:'Insure', words:['insure','insurance','puts','premium','credit','csp','scanner'], ready:True}` (tight words — no ticker hijack). `HelpPage.tsx` HELP_SECTIONS + `search.py` HELP_TOPICS gain `'insurance'` TOGETHER (the set-equality gate at selftest:1778 enforces the pair). `marketdb.py` — `insure_expectancy` table + `user_version` bump. `backend/insurance.py` new; `app.py` routes + single-flight recompute thread. `selftest.py` new check + node probes; `checkpoint.json` → `SELFTEST OK 68/68` (pin verified 67/67 today), same commit. `e2e/insureshot.mjs` new. The addresses round-trip check (selftest:4750) picks up `insure` automatically once PAGES grows.

---
## Staging

**Stage 0 — the engine, provable before any pixel.** `insurance.py` + `daily_closes` + the market.db cache + both routes + the gate check with mutation discipline. Fully exercisable by curl against the real archive on day one. **Stage 1 — the page exists honestly.** Routing seams, page shell, status strip, ranked table on real scan data, every empty/refusal state verbatim — no graph yet (the table already answers the question in text). E2e green on the credential-less scratch profile here, because honest emptiness is this app's designed first state. **Stage 2 — the insurance line.** `optgrid` additions + `InsureScatter` + rings + hover + dot/row click-through + staggered load + 60s tick. **Stage 3 — polish.** Bid-edge, top-dot labels, win/loss sentences, drawdown-character caption, focus-bump, captions audit (every number traceable to a named source and window). **v1.1, in earned order:** `tools/loadhist.py` runs the SAME sweep over the full 16-year vault at build time and bakes `hist_expectancy` beside `hist_spread_pct`; the runtime prefers it and the caption owns the difference — "measured 2010–2026 (incl. 2020)" vs "measured 2025–2026 (one regime)" — the fair line that remembers a crash, and the proper answer to zero-claim tail classes; managed-exit measurement (close at 50% of credit — the daily marks exist) as a second column, never a replacement; Opt-page reuse of the expectancy drill as an exit-view caption; covered calls (the mirrored actuarial question) if wanted.

---
## Test strategy

**Gate** (one new `@check`, pin 67→68 same commit): fixture archive db + synthetic closes map with KNOWN settlements, exact-value asserts — `claim_freq` and `expected_loss_pct` to the digit; expiration-clustered means (two entry days into one expiration move `n_days`, not `n_exp`); `settle == strike` is a win (strict inequality pinned); zero-claim class → `expected_loss_pct is None` + rule-of-three ceiling present; pending/no-close/suspect each counted; the non-session 3-day settle lookback used and a missing-session close refused; zero-bid entry in claims ledger but not priced ledger; DTE-0 mid≈intrinsic self-check; half-open band edges (Δ = 0.15 lands in exactly one bucket); OTM fallback engaged on null delta; `win_at` known answers; Wilson fractional-k endpoints; cache invalidates on fingerprint change and NOT otherwise (counting-fake connection proves one sweep for two scans). **Mutations that must redden before green is believed:** count pending trials as wins (THE lie this design exists to prevent); weight by entry days instead of expirations; settle against a neighboring session close; return 0 for a zero-claim class; flip `<` to `<=`; break the fingerprint so staleness never fires. **Node probes** (the optgrid idiom, plain node): the new exports' known answers, and the AGREEMENT assert — `insurePoint`'s y equals `annualYieldOn` for identical inputs, the two-walks-must-agree pattern keeping scanner and heatmap in one unit forever. **Seam checks that pass by construction:** address round-trip (4750), help set-equality, numpy tripwire, engine purity greps. **E2E** (`e2e/insureshot.mjs`, scratch profile, NO creds, NO archive — the fresh-install truth): `insure.gs` opens from the omnibox; h1; page-level `NO_DB_REASON` verbatim; the no-creds chain sentence per symbol; zero fabricated dots (assert the dot-count node reads 0); the no-favorites state names the fix; then with a seeded 20-row fixture archive + recorded bars: expectancy builds, ≥1 dot renders, dot click asserts the `opt:SYM:OCC` route. Measured numbers are NOT e2e-asserted — numbers live in the offline gate with fixtures, the `_options_chain` division of labor.

---
## What one year can and cannot say

Stated here because the captions must keep saying it: the engine measures ONE regime (~12 months per `hist_meta.months` — read from the data, printed, never assumed). It cannot price tails it has not seen (the zero-claims rule exists for exactly this); its claim frequencies carry ~27–52 effective samples per class per year, so one bad week legitimately flips a class from green to red — episodes make that visible instead of smoothing it; hold-to-expiry only; early assignment is unmodelled (≈ intrinsic at the same moment — severity-equivalent for the cash-secured seller, noted in help); settle is the close, not the 4pm print; cross-symbol pooling is refused. The page's job is not to promise edge — it puts the measured cost of claims on the same axes as the offered premium and lets the gap, its sample size, and its window speak for themselves.

---
## Open questions (Kade)

- **Mid ranks, bid rides along.** Dots plot and rank at mid (house convention, comparable with the heatmap); every row shows the bid-edge — the fill you can hit without negotiating. Want the conservative bid to RANK by instead? One-line swap.
- **Hold-to-expiry is the v1 trial.** A "close at 50% of credit" measurement is possible from the same daily marks but imports an exit policy into a pure measurement. Second column later, or is expiry-settled the contract you mean by insurance?
- **One regime.** The recent archive starts 2025-07-30 — the fair line has never seen a crash. Is a one-regime v1 acceptable with the caption owning it, or should the 16-year loader bake be pulled forward before you'd trust the page?
- **Bounds**: DTE 4–60, Δ anchors .05–.35, one candidate per class per expiration (every strike one click away on Opt). Widen to 90 DTE quarterlies at the cost of thinner classes? Expand a class to all strikes in place?
- **Name**: `insure.gs`, bare words `insurance` and `puts`. If "puts" is what you'd actually type, the alias can become the address.
- **Calls later?** Covered-call writing is the mirrored actuarial question; the engine generalizes with a sign flip. v1.1 scope, or puts-only on principle?

---
## Panel record

Scores (1–10), judged 2026-08-12 with tree access; claims verified against `code/` and `data/options_history.db` directly.

| Criterion | A (actuarial) | B (product) | C (systems) |
|---|---|---|---|
| Measurement honesty | 10 | 8 | 7 |
| Answers the question in one graph | 6 | 9 | 8 |
| Implementability in this codebase | 7 | 9 | 6 |
| Scan cost realism | 8 | 9 | 8 |
| Fidelity to house rules | 9 | 9 | 7 |
| **Total** | **40** | **44** | **36** |

**B is the spine** (best single graph — the y = x diagonal; every archive fact checked out exactly against the db: 7 underlyings, 261 SPY sessions, 3.05M rows, 99.9% delta, `months: 12`; correct gate pin 67/67; SVG-over-pure-math matches the OptHeatmap precedent; cleanest cost story). **A supplies the engine** (cluster means over expirations, episodes, two ledgers, suspect guard, DTE-0 self-check, tier-gated ranking, market.db cache with fingerprint — the strongest measurement discipline of the three; its delta-x graph and lightweight-charts lattice were its weak joints, and bid-as-offered deviates from the house mid convention). **C contributed the claim-frequency rings and win-at-offer** (both grafted); its separate cache db, builder daemon, and histogram blobs were over-built for a per-underlying few-second sweep, its `n_indep` heuristic is cruder than clustering, and three verified misreads (gate pin 69→70 vs actual 67, bars ladder order reversed, the no-WAL rule attributed to market.db when db.py assigns it to app.db) cost it on implementability. Contradictions resolved in this document: axes (B/C diagonal over A's delta plane, with A's calibration preserved in the hover card), class key (A/B delta-primary over C's moneyness-only — 99.9% delta presence measured), offered side (B/C mid over A's bid, bid-edge shown), cache home (A's market.db over B's process-only and C's new file), settlement lookback (B/C's holiday shift narrowed to non-session dates only, A's strictness kept for missing session closes), rendering (B's SVG over A's lattice and C's canvas), sample bands (A's coarse bands over B/C's fine ones, half-open edges gate-pinned per B).