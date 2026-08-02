# Grindstone Investments — Platform Requirements

> **Working name.** "Grindstone Investments" and the spinning-triangle logo are
> placeholders; a permanent name for the platform, the AI assistant, and the
> product family is an open decision (§12). Everything brand-related must be
> swappable from one config file.

**Status:** Draft v1 — 2026-08-01. Product requirements are settled from Kade's
brief; technical decisions in §6 are grounded in the research digest
([RESEARCH.md](RESEARCH.md)).

---

## 1. Vision

A desktop trading platform that looks and behaves like a **web browser**, not
like a trading terminal. The entire complexity of multi-broker trading, options
analytics, live data, news, and an embedded AI hides behind three ideas:

1. **One search bar.** Everything — tickers, news, app pages, AI — is reached by
   typing into an omnibox, exactly like a search engine.
2. **Everything is a tab.** Charts, account pages, news articles, the AI, real
   websites — all open as tabs that tear off into windows and regroup by drag,
   exactly like Chrome.
3. **The AI is a first-class user.** It sees what you see (charts, news,
   positions), acts through the same tools you do, and lives one click away in a
   sidebar.

Design register: **minimal and professional**. Dark and light mode throughout,
including the embedded AI UI. No dashboard clutter; nothing on screen the user
didn't ask for.

## 2. Glossary

| Term | Meaning |
|---|---|
| **Shell** | The desktop application chrome: windows, tabs, omnibox, sidebar. |
| **Page** | Content rendered inside a tab (chart, account page, article, AI, settings, external website). |
| **Account** | One configured connection to a brokerage: `live`, `paper`, or `data-only`. |
| **Omnibox** | The single search bar; center-screen when idle, top-of-tab otherwise. |
| **Sidebar** | Right-hand slim rail; expands into panels (AI chat, trade panel, related news…). |
| **Trade panel** | Sidebar panel for building and submitting orders, incl. multi-leg options. |
| **KB** | The AI's knowledge base (vector store) of news and platform documents. |
| **MCP** | Model Context Protocol — how the AI calls platform tools and reads platform state. |

## 3. System architecture (summary)

Decided in §6; summarized here so the requirements below have a spine:

- **Electron shell** (frontend: React + TypeScript) — the only technology that
  natively supports Chrome-style tab tear-off across OS windows, because Chrome
  itself is the ancestor. Custom context menus, multi-window, and an embedded
  real browser (`WebContentsView`) are first-class.
- **Python sidecar** (FastAPI) — all broker connectivity, data, auth, analytics,
  search indexing, news pipeline. Started/supervised by the shell; speaks HTTP +
  WebSocket to the frontend on a random localhost port secured by a per-boot
  bearer token.
- **Open WebUI** (separate local FastAPI service) — the AI's home. Reached
  through the shell's embedded-browser page; wired to Claude via a pipe
  function; extended with platform tools; its Knowledge feature holds the news
  KB. We integrate and skin it rather than fork it wholesale (§6.5, license
  constraint §7.5).
- **MCP server** (Python) — exposes platform state and actions (charts,
  positions, news, orders) to Claude Code / Claude Agent SDK sessions and to
  Open WebUI.
- **Storage** — SQLite (users, accounts, encrypted keys, tabs/session state,
  favorites, indicator library), LanceDB for the news vector store, an
  in-memory numpy matrix for omnibox embeddings, on-disk cache for bars.

```
┌───────────────────────────── Electron shell ─────────────────────────────┐
│  windows ⇄ tabs (tear-off/regroup)   omnibox   sidebar   context menus   │
│  React pages: chart | account | positions | settings | article | AI(tab) │
│  WebContentsView: integrated browser + Open WebUI                        │
└───────────────┬───────────────────────────────┬──────────────────────────┘
                │ http/ws + bearer (localhost)  │ http (localhost)
┌───────────────▼───────────────┐   ┌───────────▼───────────┐
│  Python sidecar (FastAPI)     │   │  Open WebUI (FastAPI) │
│  auth · accounts · brokers    │   │  chat UI · models     │
│  data · news · search · math  │◄──┤  knowledge (news KB)  │
│  indicator engine · updater   │   │  tools → our MCP      │
└───────┬───────────────────────┘   └───────────┬───────────┘
        │                                       │
   Alpaca · TastyTrade · (Webull) · aggregator  └─ Claude (Agent SDK / MCP)
```

## 4. Functional requirements

### 4.1 Authentication & user profiles

- **FR-AUTH-1** The app requires login before showing anything but the login
  screen. Full local auth: users table, username + password, argon2id hashing.
- **FR-AUTH-2** Multiple user profiles on one machine, each with isolated
  accounts, favorites, tabs, indicator library, and AI history.
- **FR-AUTH-3** Broker API keys are stored **encrypted, not merely hashed** —
  the app must decrypt them to call brokers. (The brief says "hashed"; a hash is
  one-way and would make the key unusable. See §6.7 for the exact scheme:
  password-derived KEK + DPAPI wrap, per-secret AES-GCM.) Keys are never written
  to logs, never rendered back in full (last-4 display only), and never leave
  the machine.
- **FR-AUTH-4** Session: auto-lock after configurable idle time; re-prompt for
  password to unlock (unlock re-derives the key-encryption key).
- **FR-AUTH-5** The same login opens Open WebUI silently (§6.5) — the user never
  sees a second login screen.

### 4.2 Accounts & brokerages

- **FR-ACCT-1** A user can add any number of accounts. Each account =
  brokerage + type (`live` | `paper` | `data-only`) + credentials + settings.
- **FR-ACCT-2** Brokerage support tiers (from API reality, §7.1–7.4):
  - **Tier 1 — Alpaca:** live, paper, data, news. Full trading (equities +
    multi-leg options), streaming data, streaming news.
  - **Tier 1 — TastyTrade:** live + sandbox trading (equities, complex options,
    futures, futures options), DXLink streaming data, **dry-run endpoint** for
    broker-computed buying-power effect.
  - **Tier 2 — Webull:** full trading via the official Webull OpenAPI (stocks,
    options, futures, crypto), but gated on Kade's application ($100 min
    account, 1–2 day discretionary review — §7.3). Build against its sandbox
    behind a feature flag; enable when approved.
  - **Tier 3 — Fidelity:** **no official retail trading API exists** (§7.4).
    At most read-only positions/balances via an aggregator (SnapTrade) or CSV
    import as an optional account type; never claim Fidelity order entry.
- **FR-ACCT-3** Every account gets a **settings page** (a normal tab): edit
  keys (re-encrypt on save), nickname, color tag, default order preferences,
  enable/disable, delete (with typed confirmation), connection test button with
  clear pass/fail output.
- **FR-ACCT-4** Trading uses the selected live/paper account's **own live
  data**; historical/backfill queries may use any configured `data-only`
  account. The account used for each quote/order is always visible.
- **FR-ACCT-5** One broker abstraction layer (`brokers/base.py`) defines the
  contract: auth, accounts, positions, balances, orders (place/replace/cancel),
  chains, quotes, streams. Each broker implements what it can; capabilities are
  declared, and UI degrades honestly (a button a broker can't do is disabled
  with the reason, not hidden).

### 4.3 Market data & news

- **FR-DATA-1** Real-time quotes/trades via broker WebSocket streams (Alpaca
  IEX free tier by default; SIP if the user's plan has it; TastyTrade DXLink).
  One internal stream hub fans out to all tabs so one symbol = one upstream
  subscription.
- **FR-DATA-2** Historical bars (1min → monthly), quotes, and options chains
  pulled through the data-account layer with an on-disk cache; cache is
  invalidated by calendar, not TTL guesswork.
- **FR-DATA-3** Alpaca News: REST for search/backfill (history to 2015),
  WebSocket for real-time. Articles carry symbols, headline, summary, content,
  images, source URL.
- **FR-DATA-4** News pipeline: every incoming article is (a) stored locally,
  (b) embedded into the KB (§4.8), (c) available to omnibox search within
  seconds.
- **FR-DATA-5 Recording jobs (data management).** The user chooses what to
  record, how often, and how long to keep it: price bars (1Min–1Day), options
  chain snapshots (per underlying, every 1min–daily), and news capture — each
  with a retention window, managed from a **Data management** page showing
  per-job status, what's stored, and store size. Jobs run only while the
  owner is unlocked (credentials never leave the vault); chain rows record
  the feed they came from; futures/index jobs are rejected with the real
  reason until a source exists (§6.9). *(Delivered early with the search
  milestone.)*
- **FR-DATA-6 Keyless fallback.** Users without any data API still get
  delayed quotes and daily history from Yahoo Finance, always labeled
  *delayed* and never used to price orders. Provider policy per instrument:
  §6.9. Also covers **index** quotes (SPX/NDX/VIX/XSP), which no connected
  broker feed carries.

  *Implementation revised 2026-08-02, twice measured.* This was specified as
  yfinance (pinned, with curl_cffi). Both dependencies are now **removed**:
  importing yfinance pulls in pandas, costs ~8s, and holds the GIL for most
  of it, which in a single-process sidecar freezes the entire backend. Eagerly
  that hung sign-in; deferred to the request path it blew three concurrent
  chart requests past their deadline and opened the provider's circuit
  breaker for five minutes, so a fresh install showed *no data source
  available* for every symbol while the same code worked from a shell. The
  provider now calls Yahoo's chart endpoint directly over httpx: ~0.5s, no
  key, no cookie, and the quote metadata and OHLC series arrive in one
  response. Verified end-to-end by the e2e check *"a profile with no broker
  account still gets market data"*.

  **A second keyless source is still wanted** — Yahoo is unofficial and
  intermittent. Google Finance via Google Sheets (`GOOGLEFINANCE`) is the
  candidate on the table; it needs Google OAuth, so it lands with the Google
  integration (§4.11 extensions), not before. Open decision §12.

### 4.4 Browser-style shell

**Idle state (after login):** a near-empty page — centered logo, the omnibox,
and a favorites grid below it (search-engine homepage pattern). Bottom-left:
user avatar; clicking opens a small upward nav list (Profile, Settings, Lock,
Sign out, About).

- **FR-SHELL-1 Tabs.** Opening any result opens a tab; the omnibox docks to the
  top; tab strip appears. Tabs show a page-type icon (§8), title, close button,
  and a per-page menu (bookmark/favorite, duplicate, pin, page settings).
  - **Split view** *(delivered 2026-08-02)*: right-clicking a tab opens the
    native tab menu (the gesture wheel deliberately does NOT spawn on tabs);
    "Split with <tab>" pairs it side-by-side with any other tab in the window
    — invoked tab left, partner right — with a draggable divider (20–80%).
    Activating either pair member shows the pair; activating any other tab
    shows it alone with the split kept; closing or tearing off a pair member
    dissolves it. The menu also carries New tab / Close tab / Close others /
    Move to new window.
- **FR-SHELL-2 Tear-off & regroup.** Dragging a tab out of the strip creates a
  new OS window; dragging a tab into another window's strip moves it there —
  including its **live running state** (chart continues streaming; a playing
  article keeps its scroll position). Behavior must be indistinguishable from
  Chrome's.
- **FR-SHELL-3 Windows.** Any number of windows; each remembers geometry; the
  full window/tab layout is restored on next launch (per user).
- **FR-SHELL-4 Right-click: gesture wheels.** *(Delivered 2026-08-02, Kade's
  spec.)* The platform owns right-click everywhere — including over
  third-party pages — and it spawns a SolidWorks-style radial **gesture
  wheel**, not a context menu:
  - **Click** (press+release, no travel): the wheel stays up with a
    lock/unlock hub in the center; left-click interacts; left-click outside
    closes; right-click moves the wheel; Escape closes.
  - **Hold + drag**: drag over a segment and release to act. Releasing over a
    go-to-wheel segment switches wheels and keeps the wheel open in click
    mode; releasing in the center dead-zone despawns. Selection is by angle
    beyond the dead zone (flick-friendly). No lock hub while holding.
  - **The hub lock** pins the currently shown wheel as the spawn default;
    unlock reverts to Main. Persisted per user.
  - **Wheels are user-editable** (Settings → Gesture wheels): carousel with
    one wheel shown and arrows to page; each wheel has a name + symbol.
    *(Editor v2, 2026-08-02, replacing the row-per-segment form Kade
    rejected)*: click a segment on the wheel preview, then pick its new
    meaning from a searchable, category-filtered catalog (navigation, wheels,
    chart drawing/indicators/view, tools, tickers); rotate/remove/add/label
    ops beside it. 12 segments max; new wheels start with 6 slots. Stored via
    `/api/wheels` (backend/wheels.py validates and says why it rejects;
    doc version 2 — older docs regenerate to defaults).
  - **Context wheels** *(2026-08-02)*: what you right-click picks the wheel.
    Any chart → the **Chart wheel** (editable; default: pointer/trend/
    h-line/clear + dynamic Add-symbol, Indicators, Show/Hide companion
    wheels built from the clicked chart's live state + Main). Off-chart →
    the default (locked ?? Main). Tabs → the native split menu, never the
    wheel. Chart segments act on the exact chart that was right-clicked,
    not whatever is focused when the action fires.
  - **Defaults**: *Main* (8: AI wheel N, Tabs wheel E, Tickers wheel W,
    Search S; home/news/SPY/settings between), *AI* (placeholder until the AI
    milestone, honestly dimmed), *Tabs* (dynamic — built from open tabs at
    spawn; >8 tabs paginate E/W), *Tickers* (SPY QQQ GLD USO VIX + Main).
  - **Ticker segments** show the symbol plus %-change or price
    (configurable) and can tint by day direction — green bullish, red
    bearish — using a snapshot taken at spawn (`/api/quotes`), never
    re-polled while open, so colors cannot flash. Reopen to refresh.
  - Architecture: one transparent overlay `WebContentsView` per app;
    right-button events forwarded from app views via the bridge and from
    browser tabs via a minimal no-contextBridge preload (isTrusted-gated,
    one fixed channel). Main is the single state machine and computes the
    released segment itself (shared geometry module) — renderer hover is
    never trusted for selection.
  The context-aware menu items originally listed here (ticker → Trade/Ask
  AI, chart → drawing tools, tab → Chrome set) become wheel segments and
  arrive with their features.
- **FR-SHELL-5 Sidebar.** A slim rail on the right edge of every window,
  toggled by a top-right button. Collapsed: icon buttons only. Each button opens
  one panel: **AI chat**, **Trade panel**, **Related news** (scoped to the
  active tab's symbols), **Watchlist**, **Notifications**. Panels are per-window
  and remember their width.
- **FR-SHELL-6 Integrated browser.** A tab type that is a real browser view
  (external websites — e.g., a news article's source, or any URL typed into the
  omnibox). Navigation controls appear only on browser tabs. This same
  mechanism hosts Open WebUI.
- **FR-SHELL-7 Favorites.** Any page can be favorited; favorites appear on the
  idle page as tiles with the page's logo. Default set on first run:
  **Accounts**, **APIs**, **AI**, **Positions** (§4.9).

### 4.5 Omnibox search

- **FR-SEARCH-1** As-you-type dropdown (<100 ms perceived) over mixed entity
  types: tickers, news articles, app pages, favorites, AI actions, settings,
  open tabs, URLs. Each row shows its type icon; Enter opens the top hit;
  clicking any row opens its tab.
- **FR-SEARCH-2** Ranking = fuzzy (typo-tolerant symbol/name match) + semantic
  (embedding similarity for news/pages) fused with RRF; exact ticker match
  always pins first. Stack and latency budget: §6.6.
- **FR-SEARCH-3** Intent grammar for multi-word queries: `SPY news` → news
  scoped to SPY (plus the SPY ticker row); `AAPL chart`, `sell put SPY`,
  `positions fidelity`, `ai make me an RSI variant` → routed to the right page
  or panel with the arguments prefilled.
- **FR-SEARCH-4** News search uses semantic + symbol scoping over the local KB
  and, when the query looks live ("today", "now", breaking), falls through to
  the Alpaca news REST endpoint directly.
- **FR-SEARCH-5** Typing a URL navigates an integrated-browser tab (browser
  behavior); typing anything else searches (search-engine behavior).

### 4.6 Charts & indicators

- **FR-CHART-1** Candlestick/line/area charts with the standard trader toolkit:
  crosshair, OHLCV readout, volume, log/linear, timeframes 1m→1M, session
  shading, symbol compare (multiple tickers on one chart — required for
  multi-underlying option strategies).
  *(First slice delivered 2026-08-02: the dedicated multi-symbol chart tab,
  `charts.gs` — line-series comparison of any symbol set, per-symbol
  show/hide, normalize-to-% toggle, persisted per user. The Main wheel's SW
  segment navigates to it. The chart gesture wheel — chart wheel over ANY
  chart, default wheel elsewhere — carries add-open-symbol / hide /
  indicator / drawing segments; every tool is equally reachable from the
  on-page toolbar for mouseless use.)*
- **FR-CHART-2 Drawing.** Trendlines, rays, horizontal/vertical lines,
  rectangles, fib retracements, text notes. Drawings persist per
  symbol+timeframe, survive restarts, and are visible to the AI as structured
  metadata.
  *(First slice delivered 2026-08-02: trend line + horizontal line + clear,
  drawn in data space over lightweight-charts and re-projected on pan/zoom;
  session-persistent per chart. Rays/rects/fibs/text and restart persistence
  remain open.)*
- **FR-CHART-3 Indicators.** Built-in library (MA/EMA, RSI, MACD, Bollinger,
  VWAP, ATR, volume profile at minimum) plus **user-created indicators written
  inside the platform**: an editor page where the user (or the AI, on the
  user's behalf) writes an indicator as Python (computed by the sidecar,
  returned as series) — versioned in the user's indicator library, shareable as
  a file.
- **FR-CHART-4 Trade graphics.** Positions and working orders render on the
  chart: strike lines with leg labels (e.g., `-1 PUT 480`), expiration vertical,
  breakeven lines, and a P&L-at-expiration overlay band. Selling a put from the
  trade panel immediately draws its strike + expiration on the chart (§4.7).
- **FR-CHART-5** Chart state (symbols, timeframe, indicators, drawings,
  visible range) is fully serializable — this is what gets piped to the AI on
  drag-and-drop (§4.8) and what session restore uses.

### 4.7 Trade panel

- **FR-TRADE-1** Opens from the sidebar; defaults its underlying(s) to the
  active chart's tickers. Supports equities, single/multi-leg options
  (verticals, iron condors, strangles, calendars, custom), index options, and
  futures where the broker supports them (§4.2 capability matrix).
- **FR-TRADE-2** Leg builder: pick expiration + strike from a live chain
  (streamed greeks/IV where available); each added leg immediately appears on
  the chart (FR-CHART-4).
- **FR-TRADE-3** Analytics shown live as the order is built: **probability of
  profit** (and P50-style variants), **max gain / max loss / breakevens**,
  **buying-power effect**, and a **per-leg contribution** table (how each leg
  moves the greeks, BP, and P&L shape). Methods documented in §6.4 — where a
  broker computes BP server-side (TastyTrade dry-run) use the broker's number
  and label it; where we compute it, label it *estimated*.
- **FR-TRADE-4** Order ticket: qty, limit/market/stop, TIF, preview → confirm →
  submit through the selected account; full order status stream, replace and
  cancel. Paper and live are visually unmistakable (persistent badge + color).
- **FR-TRADE-5** Every number follows the workspace honesty rule: mid-price is
  the assumed fill for options analytics; anything unverifiable is labeled.

### 4.8 AI layer

- **FR-AI-1** The assistant (name TBD) lives in three places: a **sidebar chat
  panel** (always one click away), a **full tab** (the Open WebUI app via the
  AI favorite), and **right-click → Ask AI** everywhere.
- **FR-AI-2** Backing model: Claude via the user's Claude Code subscription
  (Agent SDK; see §6.3 for how subscription auth works and its ToS bounds), so
  no per-token API bill. Model settings, prompt library, and chat history live
  in Open WebUI.
- **FR-AI-3 Context piping.** Dragging a tab onto the AI panel/tab hands the AI
  that page's full context: for a chart, the serialized state (FR-CHART-5) plus
  a rendered PNG; for an article, the text + URL; for a positions page, the
  structured positions. The AI can then **act back** through MCP tools: modify
  the chart, add an indicator, draft (never auto-submit) an order.
- **FR-AI-4 Platform tools (MCP).** The MCP server exposes at minimum:
  `get_chart_state`, `render_chart_png`, `set_indicator`, `create_indicator`,
  `get_positions`, `get_quotes`, `get_chain`, `search_news`,
  `get_latest_news` (live REST, for what the KB hasn't indexed yet),
  `read_url` (readability-extracted article text), `draft_order`. Order
  *submission* is never an AI capability — drafts land in the trade panel for
  the human to confirm.
- **FR-AI-5 Knowledge base.** A continuously-updated KB of news (and platform
  docs) inside Open WebUI's Knowledge feature; the pipeline (§4.3) pushes fresh
  articles in near-real-time and prunes by retention policy. The AI answers
  news questions from KB search *plus* the live `get_latest_news` tool, and
  says which it used.
- **FR-AI-6** The AI can browse: given an article URL it fetches readable text
  (server-side extraction) and can open the page in an integrated-browser tab
  for the user.
- **FR-AI-7** AI-created indicators go through the same indicator engine and
  library as user-written ones (FR-CHART-3) — review-then-save, never silently
  installed.

### 4.9 Default pages

| Page | Contents |
|---|---|
| **Accounts** | All configured accounts as cards: broker, type badge, connection health, balances snapshot; click → that account's settings/detail page. Add-account wizard lives here. |
| **APIs** | Every configured API credential (broker + data + AI), scope, last-used, health check, rotate/edit (re-encrypt), delete. |
| **AI** | Opens the assistant's full Open WebUI app in a tab. |
| **Positions** | All positions across all accounts: symbol, qty, basis, mark, day & total P&L ($ and %), greeks aggregation for options, per-account grouping, click-through to chart with the position drawn on it (FR-CHART-4). |

### 4.10 Settings & theming

- **FR-SET-1** Global settings page: theme (dark/light/system — one toggle also
  restyles Open WebUI, §6.5), startup behavior (restore session), default
  broker per asset class, data-source preferences, keyboard shortcuts, update
  channel.
- **FR-SET-2** All branding (product name, AI name, logos) reads from
  `branding.json` — renaming later must be a one-file change (§8).

### 4.11 Extensibility (architecture now, features later)

The platform must be **extendable by a future community** — new pages, tools,
and integrations (e.g. Google Drive, Google Sheets) added without touching core
code. Nothing here ships as a feature in v1; what ships is the architecture
that makes it cheap later (§6.11):

- **FR-EXT-1** Every first-party page (Accounts, APIs, Positions, Chart, …) is
  registered through the same internal **page registry** an extension would
  use — page type, icon, omnibox search provider, sidebar panels, context-menu
  contributions. If our own pages can't live on that API, neither can anyone
  else's; dog-fooding it is the design test.
- **FR-EXT-2** An extension is a folder/zip with a **manifest** (id, name,
  version, permissions, contributed page types / sidebar panels / search
  providers / MCP tools) plus web assets and an optional Python service module.
- **FR-EXT-3** Extension pages run in **sandboxed webviews** with a typed
  bridge API — scoped tokens against the same local API the first-party UI
  uses; an extension never sees raw broker credentials, other extensions'
  data, or unscoped account access.
- **FR-EXT-4** **Permission model** from day one of the SDK: declared in the
  manifest (read positions, read market data, draft orders, external hosts
  allowlist), surfaced to the user at install, enforced at the API layer.
  Order *submission* is never grantable to an extension (same rule as the AI,
  §6.3).
- **FR-EXT-5** v1 install path is local folder/zip ("developer mode"). A
  marketplace, extension signing, a hosted Grindstone server, and first-party
  hosted AI are explicitly future phases (§11) — the manifest format reserves
  fields for signatures/update URLs so they can be added without breaking
  existing extensions.

## 5. Non-functional requirements

- **NFR-1 Security.** Secrets encrypted at rest (§6.7); local API port
  token-protected; no secret in any log, error message, or renderer process; no
  telemetry. `doctor.py`-style secret scan runs in the gate.
- **NFR-2 Honesty.** Never present an estimated number as broker truth
  (labels: *broker* vs *estimated*); never present an unverified result as
  confirmed; failed data fetches surface as errors, not stale numbers.
- **NFR-3 Performance.** Omnibox dropdown <100 ms perceived; chart pan/zoom
  60 fps on 50k candles; cold start to login <5 s (Open WebUI may finish
  booting in background); streaming fan-out handles 50 subscribed symbols.
- **NFR-4 Resilience.** Broker/stream disconnects auto-reconnect with visible
  status; the shell survives a sidecar crash (auto-restart + session restore);
  the app is fully usable with zero accounts configured (charts on free data).
- **NFR-5 Restorability.** Everything user-created (accounts sans secrets,
  favorites, drawings, indicators, layouts) exports to a portable backup.
- **NFR-6 Portability.** v1 ships Windows, but nothing outside a named
  platform-abstraction layer may assume Windows: DPAPI, Credential Manager,
  paths, process supervision, and installer specifics live behind interfaces
  with Linux (libsecret/keyring) and macOS (Keychain) counterparts planned.
  Electron, FastAPI, and every chosen library are already cross-platform —
  portability is preserved by discipline, not ported later (§6.8).

## 6. Technical decisions

*(Grounded in [RESEARCH.md](RESEARCH.md) — every version, price, and license
below was verified against primary sources on 2026-08-01. RESEARCH.md holds the
deep detail and citations; this section holds the decisions.)*

### 6.1 Shell: Electron + Python sidecar

**Decision.** Electron 43+ using the modern `BaseWindow` + `WebContentsView`
architecture (not legacy BrowserWindow/webview/BrowserView — BrowserView is a
deprecated shim since Electron 30). React + TypeScript renders the chrome (tab
strip, omnibox, sidebar); each tab is a `WebContentsView`. Tab tear-off =
renderer detects drag-out → IPC to main → new `BaseWindow` at the cursor → the
**live** `webContents` is reparented into a view in the new window, no reload —
the same architecture class Chrome and VS Code use. The integrated web browser
and the embedded Open WebUI are the same primitive pointed at other URLs.

**Why not fork Chromium itself** (Kade asked): the tab strip/omnibox in real
Chromium is native C++ Views code, not web content — replacing it means working
in the hardest layer of a ~40 GB codebase, then rebasing security fixes every
~2 weeks forever (Brave/Vivaldi staff whole teams for this). Electron *is*
Chromium's engine with that layer already removed and replaced by our web code,
with security updates reduced to a version bump.

**Rejected.** pywebview 6.x and Tauri v2 cannot move a live webview between OS
windows (tear-off would be destroy-and-recreate with reload); Tauri stable also
can't put multiple webviews in one window. CEF Python is dead (no Python 3.12
support). PySide6/Qt WebEngine 6.11 is the named **fallback** — the only other
stack with true live-view reparenting — at the cost of fragile
PyInstaller+WebEngine packaging and a lagging Chromium.

**Backend.** Python 3.12 FastAPI sidecar, spawned/supervised by Electron main,
HTTP + WebSocket on `127.0.0.1` with an OS-assigned random port and a per-launch
bearer token (§6.7). All broker, data, news, search, analytics, and indicator
logic lives here; the renderer is presentation only.

### 6.2 Charting: Lightweight Charts + custom layers

**Decision.** TradingView **Lightweight Charts v5.2** (Apache-2.0) as the price
chart engine: fastest canvas engine, native multi-pane and multi-series
(multi-ticker overlay), plugin/primitives API, built-in `createPriceLine()` for
strike lines and `attributionLogo` for the required TradingView attribution.
Explicit custom-build items on top:
- **Drawing layer** — fork the MIT `deepentropy/lightweight-charts-drawing`
  plugin (68 tools, JSON export/import) into our tree and own it (it's ~84
  stars; a dependency we must control, not track). Drawings persist as JSON.
- **Expiration verticals / breakeven / expected-move cone** — small custom pane
  primitives (official plugin examples cover vertical lines and banners).
- **User indicators** — written in **Python**, computed by the sidecar in a
  restricted subprocess (CPU/memory/time-limited, no filesystem/network) and
  streamed to the chart as plain series. No `eval`/`new Function()` in the
  renderer. Optional later: JS indicators in a Web Worker sandbox.
- **Payoff diagrams** — Apache **ECharts 6.1** (numeric x-axis, area fills);
  LWC's time-based x-axis is wrong for P/L-vs-price curves.

**Fallback:** KLineCharts 10.0.1 (Apache-2.0) if the drawing fork proves too
costly — it has native drawings + a runtime `registerIndicator` API, but weaker
multi-ticker support and undocumented overlay persistence. **Rejected:**
TradingView Advanced Charts (license excludes personal/privately-distributed
use and grants TradingView audit access), Highcharts Stock (~$833/dev),
Chart.js (financial plugin stagnant at 0.2.1 since 2024).

### 6.3 AI: Agent SDK on the subscription, one MCP server over Streamable HTTP

**Decision.** The platform's agentic loop runs the **Claude Agent SDK
(Python)** inside the sidecar, authenticated with Kade's Claude subscription —
verified 2026-08-01 against Anthropic's support article: the per-plan Agent SDK
credit program is paused and **Agent SDK / `claude -p` / third-party app usage
draws from the subscription's usage limits — a supported path**. What is
prohibited (and server-side enforced) is extracting `CLAUDE_CODE_OAUTH_TOKEN`
into other harnesses (e.g. community Open WebUI "subscription billing" pipes).
If Open WebUI's own native Anthropic chat connection is wanted, that takes a
metered API key — optional, Kade's call (§12).

**One MCP server, Streamable HTTP transport** (FastMCP), serving three clients
identically: the Agent SDK loop, Claude Code sessions, and Open WebUI (whose
native MCP support is Streamable-HTTP-only; stdio would force an mcpo bridge).
Tools per FR-AI-4; Alpaca's official `alpaca-mcp-server` is prior art to crib
from, not a dependency.

**AI trading guardrails (mandatory, from the completeness critique):**
1. The MCP surface has **no order-submission tool** — `draft_order` stages a
   ticket in the trade panel; a human clicks confirm. No exceptions, including
   "the user told the AI to submit."
2. Every AI tool call is **audit-logged** app-side (tool, args, result hash,
   timestamp, conversation id) — MCP has no built-in audit trail.
3. A visible **kill switch** disconnects the AI layer from all broker tools.
4. Context sent to Claude is minimized: chart PNGs are cropped/compressed;
   broker keys and account numbers never enter prompts (redacted server-side).

### 6.4 Options analytics: one leg model, three consumers

**Decision.** A single normalized leg dataclass (`occ_symbol, underlying,
right, strike, expiration, signed_qty, entry_price, multiplier, mark, iv,
greeks`) feeds:
1. **Payoff/POP engine** — piecewise-linear expiration payoff with kinks at the
   strikes; breakevens solved analytically; **POP = lognormal terminal-density
   integration over the profitable region** (reproduces N(d2) single-leg and
   tastytrade-style strategy POP); Monte Carlo only for P50 and
   calendars/diagonals. The UI labels the method; local numbers are never
   presented as the broker's.
2. **Greeks/IV service** — numpy-vectorized Black-Scholes + `vollib` 1.0.11
   (the canonical package after the mid-2026 rename; LetsBeRational IV).
   `py-vollib-vectorized` (unmaintained since 2021, numba-pinned) is adopted
   only if it passes a Python 3.12 pin-and-test — a tracked open issue.
3. **Buying-power service** — local RegT formulas (CBOE margin manual: naked
   put `max(20%·S − OTM + prem, 10%·K + prem)`, credit vertical = width −
   credit, iron condor = worse side only, CSP = K·100 − prem, …) shown
   instantly and labeled *estimated*; **TastyTrade's dry-run endpoint is the
   authoritative number** (also returns itemized fees) and reconciles before
   submit. Alpaca has no dry-run, so there the local number is the display,
   still labeled. SPAN margin for futures options is broker-supplied only; any
   local scan-range grid is labeled a coarse estimate.

SPX/XSP are modeled European/cash-settled with a per-series **settlement-type
flag** (SPX monthlies AM-settled, SPXW PM-settled) — wrong settlement produces
wrong payoff and POP.

### 6.5 Open WebUI: embed stock, don't fork

**Decision.** Run a **stock, pip-installed Open WebUI (pinned, v0.11.x line)**
as a loopback service on its own app-managed Python 3.11 runtime
(python-build-standalone + uv env in `%LOCALAPPDATA%`, outside every synced
folder) — never a source fork, never frozen with PyInstaller, and nothing
copied from the AGPL `open-webui/desktop` repo. Integration surface:
- **Auto-login**: our local reverse proxy injects
  `WEBUI_AUTH_TRUSTED_EMAIL_HEADER`; OWUI binds to loopback and is reachable
  *only* through the proxy (the header is an auth bypass for anyone who can
  reach the port — see §7.5).
- **Theme**: the shell writes `localStorage.theme` into the webview before
  load (undocumented but stable mechanism; pinned version + regression test).
- **Knowledge/news KB**: managed via the REST API — upload file → poll
  `/process/status` until `completed` → `/knowledge/{id}/file/add` (naive
  upload-then-add produces a silently empty KB); rolling 60–90-day window via
  `/file/update` and `/file/remove`.
- **Extensions**: new AI logic ships as Functions (Pipe/Filter/Action) and our
  MCP server registered in External Tools. Pipelines are officially legacy —
  not used.
- **Branding**: the license (BSD-3 + branding clause since v0.6.6) permits full
  rebrand only for deployments ≤50 users in any rolling 30 days — a personal
  desktop app qualifies. Branding stays toggleable (`branding.json`) so a
  scaled distribution can flip it back on or buy the enterprise license (§7.5).

### 6.6 Omnibox search: two tiers + intent grammar

**Decision.** Python-side, two tiers. **Tier 1, every keystroke (<30 ms):**
in-memory prefix trie over the ticker universe; RapidFuzz `process.extract`
(WRatio, cutoff ≈70) over ~11k "SYMBOL Name" strings; SQLite **FTS5 trigram**
over app pages, AI actions, and cached news headlines (queries under 3 chars
route to prefix-only — trigram returns nothing for them). **Tier 2, debounced
150–200 ms:** query embedding via **fastembed** (ONNX all-MiniLM-L6-v2, model
bundled in the installer — first-run download would break offline), cosine over
a plain in-memory numpy float32 matrix. Lists fuse with **RRF (k=60)**; exact
ticker match pins first. A deterministic **intent grammar** runs before
ranking: `"SPY news"` → ticker-scoped news action at rank 1; URL-shaped input
→ browser tab (FR-SEARCH-5).

Ticker universe: Alpaca `GET /v2/assets` (free with paper keys), cached in
SQLite, daily refresh — **plus a supplemental hand-maintained table for what
Alpaca lacks**: index roots (SPX/XSP/VIX), futures roots (/ES…), and anything
TastyTrade trades that Alpaca doesn't list (critique gap). One embedding
runtime (fastembed) is shared by omnibox and the news store; OWUI's internal
embedder is its own and stays out of our path.

### 6.7 Auth & secret storage: envelope encryption, bearer-token localhost

**Decision** (the brief's "hashed keys" corrected to the recoverable-secret
standard):
- **Login**: SQLite users table; argon2id via argon2-cffi 25.x at RFC 9106
  low-memory params (64 MiB, t=3, p=4). Separate 16-byte `kdf_salt` per user.
- **Secrets**: envelope encryption. Per-user random 32-byte **DEK** encrypts
  each broker credential individually with **AES-256-GCM** (fresh 96-bit
  nonce, AAD = `user_id‖field`); the DEK is stored wrapped by a **KEK** derived
  from the login password (`hash_secret_raw`, salt independent of the login
  hash). Login verifies, derives, unwraps into memory. Password reset **cannot**
  recover keys — the flow deletes and re-enrolls broker credentials, stated in
  the UI. Optional opt-in "skip password on this machine": DPAPI-wrapped DEK in
  `%LOCALAPPDATA%` (never in a synced folder), default **off**.
- **Local API**: uvicorn on `127.0.0.1`, random port; per-launch 256-bit
  bootstrap token passed via stdin/env (never argv — process-list visible);
  every route requires `Authorization: Bearer`; **no cookies** (RFC 6265 scopes
  cookies by host, not port — any localhost server would share them);
  `TrustedHostMiddleware` against DNS rebinding (an active 2025-26 attack
  class, cf. the MCP SDK CVE); no CORS headers; renderer receives the token
  only via contextBridge.
- **Auto-lock**: `powerMonitor` idle / lock-screen event → revoke sessions,
  best-effort-wipe DEK/KEK (bytearray overwrite; Python can't guarantee
  zeroization and we don't claim it).
- **DB**: plain SQLite with field-level encryption is the baseline (protects
  exactly what matters if the file is synced/stolen). SQLCipher only as
  optional hardening via `sqlcipher3-wheels` (community fork — supply-chain
  tradeoff); `pysqlcipher3` is abandoned, never used.

### 6.8 Packaging, installer, updates

**Decision.**
- **Sidecar**: PyInstaller 6.x **onedir** (never onefile — the self-extracting
  bootloader is what AV heuristics fingerprint), shipped in electron-builder
  `extraResources`.
- **Installer**: electron-builder **NSIS one-click per-user** (`perMachine:
  false`, no admin) — the only Windows target electron-updater supports, and
  per-machine installs have long-standing auto-update elevation bugs.
- **Updates**: electron-updater **generic HTTPS provider** — a release is three
  static files (`latest.yml`, `Setup.exe`, `Setup.exe.blockmap`); blockmap
  gives block-level **delta updates for free, including sidecar changes**. The
  dedicated update script (`code/tools/release.py`) builds, versions, signs,
  and emits that trio.
- **Signing**: **Azure Artifact Signing** Basic ($9.99/mo; US individuals
  eligible; AU10TIX government-ID validation takes 1–20 business days — start
  early). Unsigned = SmartScreen wall on every release (per-file-hash
  reputation) *and* cryptographically unverified updates. Cert continuity is
  load-bearing: changing publisher identity strands existing installs (§7.7).
- **Open WebUI**: never inside the installer (its ML tree would make every
  update full-size). Installed/updated out-of-band into `%LOCALAPPDATA%` by the
  app on first run and on demand, embedding model pre-bundled/offline-pinned;
  cold start is 10+ s → shell shows a health-checked splash state.
- **Distribution: GitHub** (Kade's repo,
  `github.com/KadePrice123/Grindstone`). Source pushes at **major completed
  milestones** — never keys, never `env/`, never bulk data (the gate's secret
  scan runs before every push). Installers ship as **GitHub Releases** so
  users can download-and-install without the source; electron-updater's GitHub
  provider reads update metadata straight from Releases, so the release
  channel and the auto-update host are the same thing. `release.py` builds,
  signs, tags, and publishes the release via `gh`.
- **OS targets**: v1 = Windows (NSIS). **Linux is the committed second
  target** — electron-builder AppImage + deb, PyInstaller sidecar rebuilt
  per-OS, secret wrap via libsecret/keyring instead of DPAPI (the envelope
  scheme in §6.7 is OS-independent by design; only the optional convenience
  wrap is per-OS). macOS is technically the same recipe but requires an Apple
  Developer account ($99/yr) + notarization — open decision (§12). CI matrix
  builds (GitHub Actions) become worthwhile the moment a second OS lands.

### 6.9 Data & process topology

- **Stream hub** (critique gap): exactly **one** upstream connection per feed —
  Alpaca allows 1 websocket per data endpoint and DXLink tokens are
  per-customer. The sidecar owns subscriptions with **refcounting**: tabs
  subscribe/unsubscribe via the local WS; a symbol unsubscribes upstream only
  when its last viewer closes; tear-off/regroup does not touch upstream state.
- **Mid-mark policy** (critique gap; extends the standing mid-fill rule): each
  instrument has one designated marking feed — TastyTrade DXLink for
  everything it trades (incl. SPX, futures); Alpaca for instruments held in
  Alpaca accounts; where only the free indicative options feed is available,
  the UI labels marks *indicative*. Cross-broker positions in one view are
  marked per this policy, never mixed silently.
- **Futures/index history** (critique gap): TastyTrade has no deep OHLC REST
  (DXLink Candle replay only, ~1 day of 1-min practical) and Alpaca carries no
  futures or index data. v1 charts /ES and SPX from DXLink candle-replay depth
  only, clearly labeled; a paid vendor (Databento CME / Polygon indices) is an
  open decision (§12) before deep futures history is promised.
- **Process supervision**: Electron main supervises the sidecar; the sidecar
  supervises OWUI, the news ingester, and indicator subprocesses — health
  checks, port registry, crash-restart with backoff, and an orderly shutdown
  that provably kills the whole tree (orphaned `python.exe` is the classic bug
  in this pattern).
- **Python versions**: sidecar 3.12 (machine Python); OWUI on its own 3.11
  runtime; `tastytrade` SDK ≥13.2,<14 (needs ≥3.11 ✓); Webull SDK 3.8–3.13 ✓;
  `py-vollib-vectorized` on 3.12 is unverified → tracked test before the
  analytics epic.

### 6.10 Cross-broker abstraction (the platform's spine)

The critique's biggest finding: this layer must be designed, not grown.
- **Normalized models**: order, position, execution, balance — every adapter
  maps into them; nothing broker-shaped leaks past `brokers/`.
- **Symbology service**: one instrument identity with per-context renderings
  (OCC option symbol ↔ TastyTrade instrument symbol ↔ dxFeed streamer symbol ↔
  Webull `instrument_type`+legs). All internal keys are the normalized id.
- **Order-state machine**: one canonical lifecycle
  (`draft→validated→submitted→accepted→working→[partial]→filled/canceled/
  rejected/replaced/expired`), reconciling Alpaca `trade_updates` events,
  TastyTrade full-object account-streamer messages, and Webull gRPC events.
- **Capability matrix**: declared per adapter and consumed by the UI to gate
  controls with reasons (FR-ACCT-5). Seed matrix from research: bracket/OCO —
  Alpaca equities only; complex orders — TastyTrade via separate
  `complex-orders` endpoints/IDs; leg caps — 4 both, but Alpaca requires all
  short legs covered (no naked, no L4) while TastyTrade allows naked;
  fractional — Alpaca market-only; futures — TastyTrade only; index options —
  TastyTrade live, Alpaca paper-only; equity+option mixed legs — neither.
- **PDT gating** (critique gap): margin accounts under $25k get day-trade
  counting and a pre-submit warning when an order would consume the last day
  trade or trigger PDT restrictions.

### 6.11 Extension architecture (built-in seams, deferred features)

**Decision.** Extensibility is bought with three seams built into M1–M2, not a
plugin framework bolted on later:
1. **Page registry** (FR-EXT-1): the shell renders whatever the registry
   declares — first-party pages are entry #1 through #9. An extension page is
   just a registry entry whose assets load in a sandboxed `WebContentsView`
   with a preload-injected, permission-scoped bridge.
2. **Scoped API tokens**: the sidecar's bearer-token auth (§6.7) gains an
   audience/scope field now (cheap), so an extension token that can read
   positions but not touch credentials is a data change, not a redesign.
3. **Contribution points as data**: omnibox providers, sidebar panels,
   context-menu items, and MCP tools are all registered declaratively —
   the same mechanism the AI tools and first-party panels already use.

Integrations like Google Drive/Sheets then become ordinary extensions: a
manifest, an OAuth flow in their own sandbox, a page, maybe an MCP tool. The
future hosted phase (Grindstone server, first-party hosted AI, marketplace
with signed extensions) plugs into the reserved manifest fields (FR-EXT-5)
and replaces "developer mode" installs — nothing in v1 needs rework for it.

## 7. Constraints & risks

*(The load-bearing subset; full lists per topic in [RESEARCH.md](RESEARCH.md).)*

### 7.1 Alpaca
- **200 req/min** trading-API cap, no self-serve increase — all tabs multiplex
  through one shared client; honor `X-RateLimit-*` headers at runtime.
- **1 websocket per data endpoint** — the stream hub (§6.9) is mandatory, not
  an optimization.
- Free tier: IEX covers ~2.5% of volume (quotes can look stale next to other
  platforms) and options quotes are **indicative**, not OPRA NBBO — mids
  computed from them will diverge from live OPRA mids; the UI labels this.
  Algo Trader Plus ($99/mo, SIP + OPRA) is a user upgrade the app detects at
  runtime, never assumes.
- No futures; index options **paper-only** as of 2026-08; no naked options at
  any level; no bracket/OCO for options or crypto; options history starts
  Feb 2024; GTC auto-cancels at 90 days; **paper fills are optimistic** (no
  queue/latency/fees, 10% random partials) — paper P&L overstates live.

### 7.2 TastyTrade
- OAuth2 is **mandatory** (session tokens died 2025-12-01): 15-minute access
  tokens minted from a never-expiring refresh token; every request needs a
  `User-Agent`; the client secret is shown exactly once (rotation runbook
  required).
- The Python SDK (`tastytrade`, tastyware) is **unofficial**, one-maintainer;
  pin `>=13.2,<14` and keep the adapter thin enough to hand-roll REST if needed.
- **Never poll `/orders/live`** — documented throttle/suspension risk; the
  account streamer is the only order-state source.
- No deep OHLC history (candle replay only); sandbox fidelity is low ($1 market
  fills, 15-min quotes, 24 h resets) — final verification happens against live.
- Data entitlement is personal to Kade's login; redistribution or multi-user
  serving would need TastyTrade's trusted-third-party review. Fine for a
  personal desktop app; a blocker for any SaaS pivot.

### 7.3 Webull
- Official OpenAPI exists for US retail ($100 min account, 1–2 day
  **discretionary** review) — Tier 2, enabled only after Kade's application.
- **Unresolved conflict** (tracked open issue): help center says API keys live
  1 day (max 7, 3 resets/day); developer docs imply non-expiring. If the help
  center is right, unattended use breaks weekly — credential layer is designed
  for short-lived keys until empirically resolved.
- Real-time market data costs extra (undocumented amount). The unofficial
  `webull` pip package is dead (login 403s since 2025) and is never used.

### 7.4 Fidelity
- **No retail trading API exists, full stop** — institutional FIX only
  (millions in minimums). Any Fidelity order-entry expectation dies in this
  document.
- Read-only holdings are possible via SnapTrade's OAuth integration
  (explicitly no trading); whether an individual can register for SnapTrade at
  acceptable cost is unverified — open decision (§12). Manual CSV import is the
  zero-dependency fallback.
- Screen-scraping (Playwright/Selenium projects) is rejected: ToS-violating
  against a broker with a demonstrated enforcement posture (2023 aggregator
  cutoff), brittle, and account-ban risk.

### 7.5 Open WebUI
- License (v0.6.6+): BSD-3 **plus branding clause** — rebranding is only
  permitted for deployments ≤50 users per rolling 30 days, a written
  contributor exception, or an enterprise license. A personal desktop app
  qualifies; mass distribution of a rebranded build is legally untested wording
  → keep branding toggleable and get written clarity before any scaled release.
  The AGPL `open-webui/desktop` repo is code-radioactive for us; nothing is
  copied from it.
- Trusted-header SSO is an auth bypass for anything that can reach the port —
  loopback binding + proxy-only access is a hard requirement, and other local
  processes can still reach loopback: the OS user account is the real boundary.
- Fast breaking releases (0.10→0.11 reorganized the whole UI); theme control
  rides an undocumented localStorage contract — pin the version, regression-test
  upgrades, never patch OWUI source.

### 7.6 Claude / Anthropic
- Supported: **Agent SDK authenticated by Kade's subscription**, drawing from
  plan usage limits (credit program paused — re-verify at build time).
- Prohibited and server-enforced: extracting the Claude Code OAuth token into
  other harnesses (OWUI pipes etc.). OWUI-native chat needs a metered API key
  or stays disabled in favor of the sidebar AI.
- Third-party users of the app can never "sign in with Claude" (ToS) — v1 is
  single-user (Kade); multi-user AI would mean per-user API keys.
- Chart images are token-expensive — crop/compress before sending; no secrets
  in prompts, redacted server-side (§6.3).

### 7.7 Distribution
- Unsigned builds: SmartScreen "Unknown publisher" on **every** release
  (reputation is per-file-hash) and electron-updater silently skips signature
  verification — updates ride on HTTPS alone, a code path with past bypass
  CVEs. Azure Artifact Signing ($9.99/mo) is the fix; identity validation takes
  1–20 business days → start before M7.
- **Certificate continuity**: the publisher subject is baked into installed
  apps; changing it (or going signed↔unsigned) strands existing installs on
  manual reinstall. Decide identity once (§12 naming) before the first signed
  release.
- PyInstaller onedir reduces but doesn't eliminate AV false positives —
  Microsoft's portal accepts developer submissions; budget for it.

### 7.8 Data honesty
- POP is model-dependent (delta vs N(d2) vs distribution-integration visibly
  differ, and brokers use house assumptions) — the method is labeled and local
  numbers are never presented as broker numbers (NFR-2).
- RegT formulas are exchange minimums; brokers layer house margin — local BP is
  *estimated* everywhere, *broker* only from TastyTrade dry-run.
- SPAN parameters are proprietary — local futures-option margin is a coarse
  estimate at best, labeled as such.
- Multi-expiration strategies (calendars/diagonals) have no exact expiration
  payoff — model-based, labeled estimate-quality.
- User-indicator code is an arbitrary-code-execution surface — restricted
  subprocess (§6.2), and AI-generated indicators go through the same review
  gate as human ones (FR-AI-7).

### 7.9 Security honesty
- Same-user malware defeats every local scheme (DPAPI, keyring, in-memory
  DEKs are all same-user-readable) — the app resists *offline* theft (synced/
  stolen DB is ciphertext) and *cross-profile* access, not a compromised
  Windows account; the docs say so rather than implying more.
- Password loss = broker-key loss **by design**; the reset flow deletes and
  re-enrolls keys and says so up front.
- All secret-bearing state (DPAPI blob, session artifacts, OWUI data dir) lives
  outside every Drive-synced folder — a directory-placement rule enforced by
  the gate, not a convention.

## 8. Branding & assets

- Placeholder identity: **Grindstone Investments**; main logo = a minimal
  triangle that **spins** (CSS animation) — used on the login screen, idle
  page, and window icon (static frame for `.ico`).
- Every major page type ships a custom logo/icon in the same geometric family:
  Accounts, APIs, AI, Positions, Chart, News/Article, Browser, Settings,
  Trade. Delivered as SVG (theme-aware: `currentColor`) + generated `.ico`/PNG
  sizes for the installer.
- All name/logo references resolve through `branding.json` (FR-SET-2).

## 9. Verification gate

Per workspace rules the project declares one offline gate in
`checkpoint.json` before the first checkpoint:

- `selftest.py` — **offline**, sentinel `SELFTEST OK <n>/<n>`; covers: broker
  adapters' parsers on canned JSON (every broker, every payload shape incl.
  malformed/empty), options math against known-answer fixtures (BS prices,
  POP, BP formulas), search ranking on a fixture corpus, secret-store
  round-trip + "DB stolen" test (ciphertext useless without password), order
  pipeline dry-run (assert no order-submission path is reachable without
  explicit confirm), no-secrets-in-tracked-files scan, `branding.json` schema.
- Live connectivity is a separate diagnostic (`python code/app.py --check`),
  never part of the gate.
- Frontend: `npm test` (vitest) for tab/window state machine + omnibox
  routing; wired into the same gate command.

## 10. Roadmap

| Milestone | Delivers | Proves |
|---|---|---|
| **M0 Scaffold** ✅ | project, venv, git, key storage, this doc | — |
| **M1 Spine** | Electron shell + Python sidecar handshake; login; encrypted key store; Alpaca account connect | gate v1 passes; app boots to idle page |
| **M2 Browser UX** | tabs, tear-off/regroup, windows, context menus, sidebar rail, favorites, session restore | Chrome-parity drag test script |
| **M3 Data + charts** | stream hub, bars cache, chart page, drawings, built-in indicators | 60fps/50k-candle check |
| **M4 Omnibox** | fuzzy+semantic+intent search over tickers/pages/news | latency budget met — *lexical tier + intent grammar + news + recording engine delivered early (2026-08-02); semantic tier rides the AI milestone* |
| **M5 Trading** | chain viewer, trade panel, analytics, order tickets (paper), positions page, chart trade graphics | TastyTrade dry-run parity on BP numbers |
| **M6 AI** | Open WebUI integration, Claude pipe, MCP server, context piping, news KB pipeline | drag-chart-to-AI demo; KB freshness check |
| **M7 Ship** | installer, update script + GitHub Releases channel, logos, theming polish, Webull/Fidelity flags | **installer built and executed on this machine; update applied over old version** |
| **M8 Beyond** | Linux build (AppImage/deb, keyring wrap), extension SDK preview (manifest loader + developer-mode install + one sample extension) | sample extension installs and registers a page on a clean profile |

Each milestone ends with `checkpoint.py` — no milestone is "done" red. Major
completed milestones also push to GitHub (§6.8): source to the repo, installers
to Releases.

## 11. Explicitly out of scope (v1)

- Mobile/web deployment; multi-machine sync; social/copy-trading.
- Extension **features**: marketplace, extension signing, hosted Grindstone
  server, first-party hosted AI. The **seams** for them ship in v1 (§6.11);
  the features do not.
- macOS build (recipe identical to Linux; blocked on the $99/yr Apple
  Developer decision, §12).
- AI-initiated order **submission** (drafts only — a design decision, not a gap).
- Fidelity order entry (impossible without an official API — revisit if one ships).
- Backtesting engine (the workspace's research tooling already covers this;
  revisit as a page type later).

## 12. Open decisions for Kade

1. **Names.** Product name, AI assistant name (currently "the AI" everywhere),
   and the publisher identity for code signing — §7.7 makes this
   hard-to-change later, so it should be settled before the first signed
   release.
2. **Webull:** apply for official OpenAPI access now ($100 min, 1–2 day
   review), or park at Tier 2-when-asked?
3. **Fidelity read-only via SnapTrade:** acceptable third-party dependency and
   cost (individual registration unverified — §7.4), or CSV import only?
4. **Code signing:** enroll in Azure Artifact Signing ($9.99/mo, 1–20 day
   identity validation) now, or ship unsigned initially and accept the
   SmartScreen wall (§7.7)?
5. **OWUI native chat:** budget a metered Anthropic API key for Open WebUI's
   own Anthropic connection, or route all AI through the subscription-billed
   Agent SDK sidebar (§6.3)?
6. **Futures/index history vendor:** Databento (CME) / Polygon (indices) paid
   plan for deep /ES and SPX history, or ship v1 with DXLink candle-replay
   depth only (§6.9)?
7. **Alpaca data tier:** stay free (IEX + indicative options) or add Algo
   Trader Plus $99/mo (SIP + real-time OPRA) for accurate option mids (§7.1)?
8. **macOS:** pay the $99/yr Apple Developer fee for signing/notarization when
   Linux lands, or stay Windows+Linux (§6.8)?
9. **Public repo cadence:** the repo is public — confirm that pushing at every
   major milestone (vs. only at releases) is wanted, since research docs and
   in-progress code become public the moment they land.
10. **Second keyless data source (Kade's ask, 2026-08-02: "yahoo has been very
    off and on").** Yahoo's chart endpoint is unofficial and can change or
    rate-limit without notice, so a second free source is worth having.
    Google Finance via Google Sheets (`GOOGLEFINANCE`) is the proposal:
    genuine coverage, but it needs a Google account and OAuth, a scratch
    spreadsheet to evaluate formulas in, and a round-trip per lookup (~1–2s,
    versus ~0.5s direct) — and it cannot serve options or true real-time. It
    therefore belongs **with the Google integration** (§4.11), where the OAuth
    cost is already being paid, rather than as a standalone provider. Decide
    then whether it is the second source or whether a keyed vendor free tier
    (Finnhub/Tiingo/Alpha Vantage) is the better second leg. The provider
    seam (`backend/market.py: quote_for`) already selects per instrument, so
    adding a leg is local.
