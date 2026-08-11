# Data sourcing, storage and recording — requirements

Status: **specification, not yet built.** Written before the code so it is built
against a decided shape. Companion to [DATA_IMPORT.md](DATA_IMPORT.md), which
already specifies the file formats.

Everything below is anchored to code that exists today. Where a requirement is
impossible as first asked, that is said plainly and the honest alternative is
given — a spec that promises what the architecture cannot deliver is worse than
no spec.

---

## 1. What was asked for

1. A **priority list** of data providers the user can order — Alpaca preferred,
   OnclickMedia demoted to fallback and gap-filler — extensible as brokers are
   added.
2. **Market-data storage configurable separately from user data**, so it can
   live on a network drive.
3. A **recording server** that can auto-start with the PC, record from live
   APIs, and backfill missing data.
4. **Adding a symbol to favourites auto-records it**, as an OFF-by-default
   toggle.
5. A **system / data-recording key**: a credential the user restricts
   *broker-side* to read-only, given to the recorder so a stored key cannot be
   used to trade.

---

## 2. The capability matrix — why one list will not do

**Decision. Priority is ordered PER DATA KIND, never as one global list.**

Providers do not all serve all kinds, and the ragged edges are not incidental —
they are the whole reason OnclickMedia exists in this codebase at all.

| Kind | Alpaca | Yahoo | OnclickMedia | Local |
|---|---|---|---|---|
| Daily bars | yes (IEX from 2016) | yes | **no** | `bar_cache`, `rec_bars` |
| Intraday bars | yes | **no** (`app.py:841`) | **no** | `bar_cache`, `rec_bars` |
| Live option chains | yes (indicative) | **no** | **no** | `chain_cache` |
| Historical option chains | **no, at any plan** | **no** | yes (rolling ~180d) | `options_history.db`, file import |
| Quotes | yes | yes | **no** | — |
| News | yes | **no** | **no** | `news` |

A single ordered list `[alpaca, yahoo, onclickmedia]` is therefore **incoherent
for three of the six kinds**. "OnclickMedia as fallback" is only meaningful for
historical chains, where it is not a fallback at all — it is the *only* network
source that exists.

**Requirement DS-1.** The setting is a map from data kind to an ordered list of
provider ids. Providers that cannot serve a kind are absent from that kind's
list and the UI must not offer them there.

**Requirement DS-2.** `alpaca-iex` and `alpaca-sip` are **distinct providers**,
not one entry. The feed is hardcoded today (`alpaca_data.py:232/244/248`: `iex`
for stocks, `indicative` for chains). IEX is a few percent of volume and
indicative quotes are not OPRA NBBO — which bears directly on the standing
mid-fill assumption. A priority list that hides this difference lets a user
believe they demoted a provider when they only changed a label.

---

## 3. The provider registry — this is a refactor, not a setting

There is **no provider abstraction today**. `brokers/base.py:BrokerAdapter` is
an account-auth contract with one method (`test_connection`), and the data
clients do not implement it. Every choice is a hardcoded `if/try/except` ladder
inlined at its call site:

- bars — inside the route function, `app.py:776-857`
- quotes — `market.py:48-77`
- chains — `options.py:333-365`, Alpaca-only, no fallback at all

**Requirement DS-3.** Introduce a provider registry: each provider declares an
id, a display name, the kinds it serves, and a fetch method per kind. The three
ladders are replaced by one resolver that walks the user's order for that kind.

**Requirement DS-4 — empty is not failure.** The bars ladder currently treats an
Alpaca `BrokerError` and an Alpaca empty list identically (`app.py:790-812`);
both fall through to the same generic `source: "none"`, with the error visible
only in the log. A recorder built on that will **write holes and call them
holidays** — the exact failure `onclick.py` already refuses to make. The
resolver must return a three-state result:

- `DATA` — rows, plus the provider that served them
- `ABSENT` — the provider is authoritative and says there is nothing (a
  genuine non-session, a delisted symbol)
- `FAILED` — transient; the next provider is tried and **nothing is recorded as
  absent**

The quote path has the same conflation in the other direction
(`market.py:57-64` returns on an HTTP-200-but-empty snapshot without consulting
Yahoo). Under a priority list that becomes "provider 1 answered, stop" when
provider 1 in fact answered with nothing.

**Requirement DS-5 — provenance on every recorded row.** `rec_bars`,
`rec_chain` and `bar_cache` have **no source column** (`marketdb.py:47-93`). The
moment more than one provider can write them, "where did this candle come from"
is unanswerable, and the bars route's `source: "cached (fetched …)"` label
becomes a lie by omission. Adopt the `sources` / `src_id` design already proven
in `btdata.py` — measured at +2% file size against +40% for repeated text.

**Requirement DS-6.** The gate pins the honesty-label strings
(`selftest.py:819-822`) and bans yfinance/pandas/curl_cffi from the sidecar
(`selftest.py:783-798`, a freeze that has happened twice). The registry
refactor must preserve both or update them deliberately, never incidentally.

---

## 4. Settings shape

The settings system supports `bool | float | choice | json | path`
(`settings.py`). **There is no ordered-list kind.**

**Requirement DS-7.** Add a `providers` kind: a per-kind ordered list rendered
as a drag-to-reorder control, validated against the registry so a provider that
cannot serve a kind is rejected rather than silently ignored. Storage is JSON;
the validation is what makes it a setting rather than a text box.

---

## 5. Storage location

`data_dir()` (`db.py:85`) is the single knob, and everything hangs off it:

| File | Holds | May relocate? |
|---|---|---|
| `app.db` | **credentials, KDF salt, wrapped DEK, user identity** | **NO — never** |
| `market.db` | universe, news, recorded data, caches | yes |
| `backtest_data/<SYM>.db` | engine store | yes |
| `options_history.db` | archived chains (588 MB) | yes |
| `backtests/`, `logs/` | run output, logs | yes |

**Requirement DS-8.** Add `market_data_dir()`, defaulting to `data_dir()` and
overridable by a setting. `marketdb.market_path`, `btdata.data_db_path`,
`opthist.db_path` and the backtest output root resolve through it. `app.db`
resolves through `data_dir()` and **must refuse a network path** — moving the
credential vault onto a share is the dangerous direction, and §7.9's placement
discipline exists to prevent exactly that.

Separating them is a security *improvement*: bulk market data goes to the
network drive, the vault stays local.

**Requirement DS-9 — network drives are not free.** SQLite over SMB has real
locking hazards, and every relocatable database here runs WAL, which is worse
over a share than rollback-journal. The spec requires:

- On selecting a network path, **probe it**: create a test database, take a
  write lock from two connections, and report the result before accepting.
- Prefer `journal_mode=DELETE` on a detected UNC/network path, with the
  reasoning recorded in the file's own comment rather than in a commit message.
- Refuse silently-degraded operation: if locking probes fail, say so and keep
  the previous location.
- One machine writes at a time. Multi-machine concurrent write is **out of
  scope** and must be stated in the UI, not merely omitted.

---

## 6. The recording server

### 6.1 The blocker, stated plainly

**A recorder cannot start with the PC and record, as literally asked, under
today's design.** The DEK exists only inside a `SessionStore` entry
(`sessions.py:34`); sessions are created only by `/api/auth/setup` and
`/api/auth/login` from a plaintext password. At boot there is no session,
`State.creds_for` returns `None` (`app.py:88`), and every job records
`"locked — sign in to record"` (`recorder.py:124`).

### 6.2 The system key — the decision that resolves it

**Decision.** The recorder gets its **own credential**, restricted read-only at
the broker, stored separately from the user's trading keys and protected
proportionally to what it can do.

Three tiers, in descending order of what they actually promise:

1. **OAuth token with `data` scope, no `trading` scope** — the broker itself
   refuses orders. Strongest. Costs an OAuth app registration and approval.
2. **Paper-account key** — cannot touch real money; works only against
   `paper-api.alpaca.markets` (`PAPER_URL` already exists). Zero friction.
   **Caveat that must be surfaced:** entitlements follow the account, so a
   paper key records Basic-tier data (IEX equities, indicative options) even if
   the live account carries a better plan. Isolation costs data quality, and
   the user must be told which they are choosing.
3. **A live key** — all-or-nothing; Alpaca does not scope plain key pairs. May
   be used, but the UI must state that this key can trade.

**Requirement DS-10 — probe the key, and report only what is knowable.**

*(Restated after adversarial review. The first version — "verify the key cannot
trade" — is NOT IMPLEMENTABLE: Alpaca does not scope plain key pairs, so no
read-only call can prove a negative about one. A requirement that cannot be met
is worse than none, because it will be marked done.)*

On save, probe both Alpaca trading hosts with read-only GETs and record one of
three verdicts:

- **`LIVE_CAPABLE`** — a live host authenticated. Say it in those words: *this
  key can place real orders.*
- **`PAPER_ONLY`** — only the paper host authenticated. This key cannot move
  real money. It is **not** "read-only" — it can still place paper orders and
  reset the paper account, and the UI must never call it read-only.
- **`UNDETERMINED`** — network error, 429, or an ambiguous answer. Never
  recorded as safe.

Fail closed: both hosts answering means `LIVE_CAPABLE`. The unattended toggle
**refuses** `LIVE_CAPABLE` and `UNDETERMINED` unless the user explicitly
overrides with the consequence stated in front of them.

**This failure mode is already shipped.** `kind='data'` exists today and its
adapter only pings the news endpoint (`brokers/alpaca.py:91-104`), so a live,
fully order-capable key enrolled as "Data only" gets a green **Connected**
right now. Fixing that is part of this requirement, not a separate task.

OAuth (tier 1) is **out of scope for v1** for a concrete reason rather than
effort: both HTTP clients hardcode key-pair headers
(`alpaca.py:68-71`, `alpaca_data.py:163-166`), so a Bearer token cannot be sent
by either without an auth-mode branch and a refresh path.

**Requirement DS-11 — proportional protection.** Because a tier-1 or tier-2 key
**cannot trade**, it may be stored under OS-bound encryption for unattended use.
This is not a new security decision: `REQUIREMENTS.md:659` already sanctions an
opt-in machine-bound unlock ("DPAPI-wrapped … in `%LOCALAPPDATA%`, never in a
synced folder, default off"), and `RESEARCH.md:612` records the same. The user's
**trading** keys stay password-derived, always.

The blob goes in `%LOCALAPPDATA%`, **never** in `data_dir()` — that tree is
Drive-synced, and §7.9 makes "outside every synced folder" a placement rule the
gate should enforce (it currently does not). The property that a stolen blob is
useless off-machine holds *because* `%LOCALAPPDATA%` does not roam: the DPAPI
masterkey lives in roaming `%APPDATA%`, so moving the blob there "for symmetry"
would silently destroy the guarantee while every test still passed.

**Three things this must never do**, each an easy and catastrophic shortcut:

1. **Never wrap the DEK.** §6.6 sanctions a DPAPI-wrapped *DEK* for the separate
   "skip password on this machine" convenience. Reusing that blob here would
   hand a background process the key to the user's **live trading
   credentials** — the exact inversion of this feature's premise.
2. **Never store the system key in `accounts`/`secrets`.** Two copies with
   different lifetimes is the bug class that produces "it worked yesterday",
   and enrolling it as `kind='data'` puts it at `_KIND_PREFERENCE` rank 0
   (`market.py:22`), silently making it the source for **every** interactive
   quote, chart and chain — an entitlement downgrade to IEX/indicative with no
   UI event. The key lives only in the blob; a secret-free metadata file beside
   it carries the verdict, the key-id last 4, and the enrolment date.
3. **Never add a system-key fallback to `State.creds_for`.** It has eight
   interactive callers. Add a separate `State.system_creds()` injected only at
   the recorder entry point.

On decryption failure the resolver returns `None` — a normal state, never an
exception, never a retry loop, never a fall back to the user's vault — and the
recorder writes a system-key-specific status, because `last_status` is the only
surface the user has.

**Requirement DS-12 — Electron `safeStorage` vs Python DPAPI is a real fork.**
`safeStorage` is a main-process API, so choosing it forces the auto-start unit
to be the Electron app. A Python-side wrap (`CryptProtectData`, what
RESEARCH.md recommends) lets the sidecar run as a service or scheduled task
without the UI. **If the recorder is to be independent of the UI, it must be the
Python-side wrap.** Either sits behind the NFR-6 platform seam, with
libsecret/keyring on Linux.

### 6.3 Auto-start, honestly

**Requirement DS-13.** Two modes, both off by default:

- **Attended** (no new secrets): the app auto-launches at login, the recorder
  runs in `locked` state, and **backfill closes the gap** once the user signs
  in. Bars backfill is nearly free — the collector already resumes from
  `MAX(ts)` (`recorder.py:149-157`).
- **Unattended** (requires DS-11): the recorder holds the system key and runs
  with no app sign-in at all.

**The promise is "starts at your WINDOWS LOGIN", not "starts with the PC".**
*(Corrected after review — the original wording was unachievable.)* A
user-scope DPAPI blob can only be decrypted inside that user's logged-on
session, so a machine rebooted and left sitting at the login screen records
nothing. The autostart unit is therefore Task Scheduler with an at-logon
trigger and an interactive token — not a service (LocalSystem cannot read a
user-scope blob), and not S4U (no access to encrypted data). Say this plainly
in the UI, or the first overnight test reads as a bug.

**The cost that must be stated out loud:** chain snapshots are point-in-time and
**cannot be backfilled from Alpaca at any plan**. An overnight lock permanently
loses intraday chain snapshots. Bars can be recovered; chains cannot.

**Requirement DS-14 — fix the accidental session dependency.**
`any_for_user` does not refresh `last_seen` (`sessions.py:104`), so a recorder
borrowing a UI session loses credentials 15 minutes after the last UI request —
meaning whether recording survives depends on **which page happens to be open**
(DataPage polls every 15-20 s and keeps it alive; a minimised app on a static
page does not). That is a latent bug today. The unattended vault handle must be
a separate concept from a user session — bolting a never-expiring entry into
`SessionStore` would silently defeat the interactive auto-lock, because idle
expiry and `revoke_user` are the same mechanism.

---

## 7. Coverage and backfill

**"Backfill missing data" is not expressible today.** Nothing can answer "which
days am I missing for SPY".

**Requirement DS-15.** A coverage table keyed by
`(provider, kind, symbol, timeframe, period)` recording, per period, one of
`have | absent | unknown | failed`. `absent` is a *positive* claim by an
authoritative provider (see DS-4) and suppresses retries; `failed` and `unknown`
do not. Without this distinction a backfill either re-requests forever or
records outages as holidays.

**Requirement DS-16.** Backfill is a paced, resumable, finite work list with a
visible queue — the shape `DataJobs` already implements. For OnclickMedia the
window must be **recomputed at execution time, never at enqueue time**: it moves
one day per day (`onclick.py:53-61`), so a queue built on Monday contains items
that are outside the window by the time they run.

---

## 8. Auto-record favourites

**Requirement DS-17.** An OFF-by-default setting. When on, adding a symbol
favourite enqueues a recording job and a backfill for it; removing the favourite
**does not** delete recorded data (deleting a user's data as a side effect of an
unrelated click is never acceptable) — it stops the job and says so.

**Requirement DS-18 — the legal domains differ.** Favourites accept
`kind in ('symbol','page','web')` with no asset filter, but `recorder.validate_job`
refuses index and future outright, refuses crypto for bars and chains, and
`jobs_create` 422s any symbol the universe has not synced. So the toggle must be
able to report **"starred, but not recordable, and why"** rather than failing
silently or refusing the favourite.

---

## 8b. What shipped (DS-15, DS-16, DS-17, DS-18)

`backend/coverage.py` — the `data_cover` table (market.db schema 5) and the
claim-per-period model. The load-bearing line is between `absent` and
everything else: `absent` is a provider asserting a fact about the WORLD and
suppresses retries forever, while `failed` and `unknown` are facts about *us*
and do not. Collapse them and the backfill either re-requests every market
holiday until the end of time, or writes a five-minute outage down as "the
market was closed" and never looks again. Both failures are silent.

Only a provider AUTHORITATIVE for a kind may claim `absent`; anyone else's
empty response is downgraded to `unknown`, because "I don't carry this name"
and "the market was shut" are the same empty list on the wire. `mark()` is the
single enforcement point — a mutation removing the backfill's own check was
correctly *not* caught, because the downgrade in `mark()` still held.

`backend/backfill.py` — plan, run, resume. Resumption is free: the plan is
recomputed from coverage on every call, so there is no cursor to corrupt and
no queue to go stale. **The OnclickMedia window is recomputed at EXECUTION
time**, never at plan time; a chunk that has fallen outside it is reported as
skipped and marked `unknown` (retryable), not silently fetched-as-nothing.

`backend/autorecord.py` — starring a symbol records it; un-starring **stops
the job and keeps the data**. Deleting months of chain history as a side
effect of un-starring a shortcut would be unrecoverable, since the provider
windows have already moved past it. A symbol that cannot be recorded stays
starred and reports why (DS-18) rather than failing silently or refusing the
star.

**Chain history comes from OnclickMedia, and absence there is EARNED.**
Alpaca sells no historical option snapshots at all, so past chains have
exactly one source and its window is a rolling ~180 days. That provider
brings a problem the coverage model had to grow for: its empty response means
either "the market was shut" or "I do not carry this ticker", and it
genuinely has never carried SPX, SPXW or XSP. Blanket authority would let one
empty response permanently blacklist a date for a symbol it never had; no
authority would re-ask every holiday inside the window forever. So
`coverage.EARNS_AUTHORITY` lets a provider earn the right to claim absence,
per symbol, by having answered for that symbol at least once.

The day loop maps each outcome deliberately: a parsed body is `have` (stored
before the claim), an open session's greek-less header is `failed` and
retried once it settles, a transient fault is `failed`, a 403 outside the
plan's range is `unknown` because the provider failing to answer is not a
statement about the market, and an empty body is `absent` only if authority
has been earned. Off by default under its own switch, separate from the main
backfill because it reaches a third party rather than the user's broker key.

**Turning the setting on enrolls the favourites you already have.** The
`PUT /api/settings` handler reads the OLD value before writing the new one
and runs `sync_all` on the off→on edge only. Applying to future stars alone
would be invisible in the worst way: the user flips the toggle, watches
nothing happen to the twelve symbols they have starred for months, and
concludes the feature is broken. The edge matters too — re-running on every
save would re-enable jobs they had deliberately paused in the Data page,
overruling that choice from an unrelated setting change.

Three settings, both toggles OFF by default: `autorecord_favorites`,
`backfill_enabled`, `backfill_years`. The Data page gained a Coverage &
backfill panel, which states `truncated` explicitly — a run capped at
`MAX_CHUNKS` must not read as a finished one.

Verified by `python code/selftest.py` (one check, run against a real database
rather than a source scan, because every failure mode here is silent) and
**11 mutations each confirmed red** — including "absent is retryable",
"failed is settled", "un-starring deletes the data", and "auto-record ships
on by default". One mutation exposed a real defect in the code under test:
`RETRYABLE` was a constant that documented the policy without driving it, so
editing it changed nothing. `gaps()` now derives its query from it.

---

## 9. Verification

Each requirement lands with a gate check in `selftest.py`'s idiom, and none of
these are screenshot-checkable:

1. **Registry** — every provider's declared kinds match what it implements; a
   provider absent from a kind cannot be ordered into it.
2. **Three-state resolution** — a simulated transient failure advances to the
   next provider and writes **no** coverage row; an authoritative empty writes
   `absent`. This is the check that prevents holes-as-holidays.
3. **Provenance** — a row written by provider A reports A, through a store
   filled by two providers.
4. **Relocation** — with `market_data_dir` pointed elsewhere, `app.db` stays
   put; a network-shaped path for `app.db` is refused.
5. **Placement** — the OS-wrapped blob is outside every synced directory. §7.9
   calls this gate-enforced; it currently is not.
6. **Stolen-pair** — a stolen `app.db` *plus* a stolen `%LOCALAPPDATA%` blob
   from a **different machine** still yields no plaintext.
7. **Key capability** — a key that can trade is reported as trading-capable
   (DS-10) rather than accepted on its label.
8. **Favourites** — a non-recordable favourite reports why, and un-favouriting
   never deletes data.
