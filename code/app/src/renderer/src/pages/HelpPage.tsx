/**
 * help.gs — the manual. Every section id here is a SEARCHABLE DESTINATION:
 * backend/search.py's HELP_TOPICS points at these ids, so searching
 * "drawing" opens this page scrolled to #drawing (Kade's spec). The gate
 * cross-checks that the two lists never drift apart.
 *
 * Content rule: every button name, wheel slot and setting written here is
 * checked against the real UI by the gate where practical — a manual that
 * lies is worse than none.
 */
import { useEffect, useRef } from 'react'
import { PageMiniIcon } from '../components/icons'
import '../help.css'

export const HELP_SECTIONS = [
  'getting-started',
  'search',
  'tabs',
  'split-view',
  'wheels',
  'favorites',
  'charts',
  'drawing',
  'measuring',
  'multi-charts',
  'data',
  'backtest-help',
  'settings-help',
  'troubleshooting',
] as const

/* ---------- tiny inline illustrations (theme-aware, no assets) ---------- */

function WheelSvg() {
  const seg = (i: number) => {
    const a0 = ((-90 - 22.5 + i * 45) * Math.PI) / 180
    const a1 = a0 + (41 * Math.PI) / 180
    const p = (r: number, a: number) => `${r * Math.cos(a)},${r * Math.sin(a)}`
    return (
      <path
        key={i}
        d={`M ${p(60, a0)} A 60 60 0 0 1 ${p(60, a1)} L ${p(26, a1)} A 26 26 0 0 0 ${p(26, a0)} Z`}
        className={i === 0 ? 'hl-seg on' : 'hl-seg'}
      />
    )
  }
  return (
    <svg viewBox="-70 -70 140 140" className="hl-fig" aria-label="Gesture wheel diagram">
      {Array.from({ length: 8 }, (_, i) => seg(i))}
      <circle r="18" className="hl-hub" />
      <text y="4" className="hl-t">🔒</text>
      <text y="-63" x="0" className="hl-t sm">Draw</text>
    </svg>
  )
}

function SplitSvg() {
  return (
    <svg viewBox="0 0 140 80" className="hl-fig" aria-label="Split view diagram">
      <rect x="2" y="2" width="136" height="76" rx="4" className="hl-frame" />
      <rect x="6" y="14" width="60" height="60" rx="2" className="hl-pane" />
      <rect x="74" y="14" width="60" height="60" rx="2" className="hl-pane" />
      <rect x="67" y="14" width="6" height="60" className="hl-div" />
      <text x="36" y="47" className="hl-t sm">A</text>
      <text x="104" y="47" className="hl-t sm">B</text>
      <path d="M 70 40 l -6 -4 v 8 z M 70 40 l 6 -4 v 8 z" className="hl-arrow" />
    </svg>
  )
}

function TrimSvg() {
  return (
    <svg viewBox="0 0 150 70" className="hl-fig" aria-label="Trim diagram">
      <line x1="10" y1="55" x2="140" y2="12" className="hl-line" />
      <line x1="10" y1="20" x2="140" y2="20" className="hl-line" />
      <line x1="10" y1="55" x2="86" y2="30" className="hl-cut" />
      <circle cx="86" cy="30" r="3.5" className="hl-x" />
      <text x="40" y="66" className="hl-t sm">click here → this span goes</text>
    </svg>
  )
}

function MeasureSvg() {
  return (
    <svg viewBox="0 0 150 70" className="hl-fig" aria-label="Measure diagram">
      <line x1="25" y1="55" x2="120" y2="18" className="hl-dim" />
      <circle cx="25" cy="55" r="3" className="hl-x" />
      <circle cx="120" cy="18" r="3" className="hl-x" />
      <rect x="52" y="28" width="52" height="18" rx="3" className="hl-chip" />
      <text x="78" y="40" className="hl-t sm">+$12 · 9 bars</text>
    </svg>
  )
}

function FavSvg() {
  return (
    <svg viewBox="0 0 150 98" className="hl-fig" aria-label="Favorites diagram">
      {/* the address bar, star at its end */}
      <rect x="2" y="2" width="146" height="22" rx="5" className="hl-frame" />
      <text x="12" y="17" className="hl-t sm" textAnchor="start">spy.gs</text>
      <text x="135" y="18" className="hl-t">★</text>
      {/* the home grid the star feeds: ticker · page · website */}
      <rect x="7" y="34" width="41" height="58" rx="5" className="hl-pane" />
      <rect x="54" y="34" width="41" height="58" rx="5" className="hl-pane" />
      <rect x="101" y="34" width="41" height="58" rx="5" className="hl-pane" />
      <text x="27.5" y="56" className="hl-t">SPY</text>
      <text x="27.5" y="70" className="hl-t sm">+1.24%</text>
      <text x="27.5" y="85" className="hl-t sm">SPY</text>
      <rect x="68" y="46" width="13" height="13" rx="2" className="hl-btn" />
      <text x="74.5" y="85" className="hl-t sm">Data</text>
      <circle cx="121.5" cy="52.5" r="7" className="hl-btn" />
      <text x="121.5" y="85" className="hl-t sm">Site</text>
    </svg>
  )
}

function StripSvg() {
  return (
    <svg viewBox="0 0 170 34" className="hl-fig" aria-label="Tab strip diagram">
      <rect x="2" y="2" width="166" height="30" rx="4" className="hl-frame" />
      <rect x="8" y="8" width="34" height="20" rx="3" className="hl-pane" />
      <rect x="45" y="8" width="34" height="20" rx="3" className="hl-pane dim" />
      <rect x="120" y="8" width="16" height="20" rx="3" className="hl-btn" />
      <rect x="139" y="8" width="16" height="20" rx="3" className="hl-btn" />
      <text x="128" y="22" className="hl-t sm">+</text>
      <text x="147" y="22" className="hl-t sm">⇄</text>
    </svg>
  )
}

/* ------------------------------------------------------------------------ */

interface Sec {
  id: (typeof HELP_SECTIONS)[number]
  title: string
  fig?: () => React.JSX.Element
  body: React.JSX.Element
}

const SECTIONS: Sec[] = [
  {
    id: 'getting-started',
    title: 'Getting started',
    body: (
      <>
        <ol>
          <li>First run: create a profile. <strong>Your password is the vault key</strong> that
            encrypts your broker credentials — there is no recovery by design.</li>
          <li>Open <code>accounts.gs</code> (or search “accounts”) → add your Alpaca paper key →
            <em> Test</em> → <em>Save</em>. Keys are encrypted per-user; the app never stores
            them in plain text.</li>
          <li>No broker account? Quotes and daily charts still work through the keyless
            delayed fallback — everything is labeled with its source.</li>
        </ol>
      </>
    ),
  },
  {
    id: 'search',
    title: 'Search & addresses',
    body: (
      <>
        <p>The bar at the top is a browser omnibox for two worlds:</p>
        <ul>
          <li><strong>The platform</strong> — every page is a <code>.gs</code> address:
            <code> spy.gs</code>, <code>charts.gs</code>, <code>settings.gs</code>,
            <code> help.gs</code>. Bare names work too: typing <code>settings</code> or
            <code> charts</code> just goes there.</li>
          <li><strong>The web</strong> — <code>google.com</code> opens the real site as an
            in-app browser tab.</li>
          <li>Anything else searches: tickers, news, pages, help topics and web results,
            ranked together. The <em>In-house result boost</em> setting decides how strongly
            platform results outrank the web.</li>
        </ul>
        <p>Typing a bare ticker (<code>SPY</code>) opens its chart page directly.</p>
      </>
    ),
  },
  {
    id: 'tabs',
    title: 'Tabs & windows',
    fig: StripSvg,
    body: (
      <>
        <ul>
          <li><strong>+</strong> and <strong>⇄</strong> sit right of the tab list and never
            scroll away. <strong>+</strong> opens a tab; <strong>⇄</strong> jumps to the
            previously active tab — press it twice to bounce between two tabs.</li>
          <li><strong>Drag a tab off the strip</strong> to tear it into its own window;
            drag it onto another window’s strip to move it there. The page keeps running —
            nothing reloads.</li>
          <li>Middle-click a tab to close it. Right-click a tab for the tab menu
            (split view, close others, move to new window).</li>
          <li>The triangle at the far left is Home.</li>
        </ul>
      </>
    ),
  },
  {
    id: 'split-view',
    title: 'Split view',
    fig: SplitSvg,
    body: (
      <>
        <ol>
          <li>Right-click a tab → <em>Split with …</em> → pick any other open tab.</li>
          <li>The invoked tab goes <strong>left</strong>, the chosen one <strong>right</strong>;
            drag the divider between them to resize (20–80%).</li>
          <li>Clicking either paired tab shows the pair; other tabs show alone and the
            split waits. Closing or tearing off a member dissolves it, or use
            <em> Close split</em> in the tab menu.</li>
        </ol>
      </>
    ),
  },
  {
    id: 'wheels',
    title: 'Gesture wheels',
    fig: WheelSvg,
    body: (
      <>
        <p>Right-click anywhere for a radial menu. Two ways to use it:</p>
        <ul>
          <li><strong>Click</strong> (press and release): the wheel stays; left-click a
            segment; left-click outside closes; right-click moves it; Escape closes.</li>
          <li><strong>Hold and drag</strong> over a segment, release to act — releasing on
            a wheel segment (like <em>Draw</em>) opens that wheel and keeps it up.</li>
        </ul>
        <ul>
          <li>The <strong>center hub</strong> (click mode) locks the shown wheel as your
            default spawn; click again to unlock.</li>
          <li><strong>What you right-click matters</strong>: any chart spawns the Chart
            wheel; a tab opens the tab menu; everywhere else spawns your default wheel.</li>
          <li>Edit wheels in <code>settings.gs</code> → <em>Gesture wheels</em>: click a
            segment on the preview, then pick its meaning from the searchable catalog.</li>
        </ul>
      </>
    ),
  },
  {
    id: 'favorites',
    title: 'Favorites & apps',
    fig: FavSvg,
    body: (
      <>
        <ul>
          <li>The <strong>☆</strong> at the end of the address bar stars whatever you
            are on — a platform page, a ticker, a website. It shows <strong>★</strong> when
            the current page is already a favorite; click again to remove it.</li>
          <li><strong>Home is your favorites grid.</strong> Each tile carries its
            identity — a website its captured tab image, a platform page its glyph,
            a ticker its symbol — with the short name underneath. Ticker tiles add
            today’s move (green up, red down); <em>—</em> means no quote source is
            available right now.</li>
          <li>Provider and system apps moved off the home page into the
            <strong> Apps</strong> launcher — the 3×3-dot button right of the address
            bar. It holds every platform app; the ones announced but not built yet
            are dimmed.</li>
          <li>The <strong>Favorites wheel</strong> (Main wheel → <em>Favorites</em>)
            builds itself from your stars: ticker favorites keep the price/percent
            display and day-direction colors — frozen while the wheel is open,
            re-open to refresh — pages and sites simply open. More than eight and it
            pages through them, like Tabs.</li>
          <li>In <code>settings.gs</code> → <em>Gesture wheels</em>, the segment
            picker’s <em>Favorites</em> category offers everything you have
            starred — it replaced the free-typed ticker slot, so a wheel segment can
            only point at something that exists.</li>
        </ul>
      </>
    ),
  },
  {
    id: 'charts',
    title: 'Charts',
    body: (
      <>
        <ul>
          <li><strong>Timeframes</strong>: 1m · 5m · 15m · 1H · 1D from the toolbar, or the
            Chart wheel → <em>Timeframe</em>.</li>
          <li><strong>Indicators</strong>: Volume, two SMAs, EMA, RSI as toggles; the
            <strong> ⚙</strong> button (or wheel → <em>Indicators</em> → <em>Settings…</em>)
            edits their periods (1–400) for this session.</li>
          <li><strong>History depth</strong>: <code>settings.gs</code> → <em>Candles per
            chart</em>. Default <em>all</em> loads everything the source has (SPY daily
            reaches 1993); a number caps it for speed on intraday timeframes.</li>
          <li>The metrics row above the chart shows last, today’s move, OHLC, volume and
            the 52-week range, each labeled with its data source.</li>
        </ul>
      </>
    ),
  },
  {
    id: 'drawing',
    title: 'Drawing tools',
    fig: TrimSvg,
    body: (
      <>
        <p>Toolbar: <em>Ptr · Line · H · V · Circle | Del · Trim | Clear</em> —
          the same tools live on the Chart wheel → <em>Draw</em>.</p>
        <ul>
          <li><strong>Selecting needs no tool.</strong> In <strong>Ptr</strong> — the
            default — just left-click a drawing, measurement or pinned candle. It
            highlights as you hover so you can see what you are about to pick, and the
            cursor turns into a hand. Clicking picks that one object and drops whatever
            was held before; hold <kbd>Shift</kbd> (or <kbd>Ctrl</kbd>) to add to the
            selection instead. Clicking empty space clears it, and so does <kbd>Esc</kbd>.
            Picking <em>one</em> object opens exact-value boxes: price for an H-line, date
            for a V-line, price + date per endpoint for lines and circles. Enter commits.</li>
          <li><strong>Line</strong> (any angle): click two anchors — a dashed preview with
            live Δprice/Δtime follows your cursor between them.</li>
          <li><strong>H / V</strong>: one click places a horizontal price line or vertical
            time line; the preview tracks the crosshair before you click.</li>
          <li><strong>Circle</strong>: click the center, then an edge point.</li>
          <li><strong>Del</strong>: with a selection, deletes all of it; without one, deletes
            the drawing you click. <kbd>Delete</kbd> works too.</li>
          <li><strong>Trim</strong>: like SolidWorks — click a span of a line and only that
            span is removed, cut back to the nearest intersections with other drawings.
            The hover shows exactly what will go, in red. A line with no intersections is
            removed whole.</li>
          <li><kbd>Esc</kbd> cancels a half-placed drawing. <em>View → Drawings</em> hides
            or shows every drawing without deleting anything.</li>
        </ul>
        <p>Drawings are kept per symbol + timeframe for your session.</p>
      </>
    ),
  },
  {
    id: 'measuring',
    title: 'Measuring',
    fig: MeasureSvg,
    body: (
      <>
        <ul>
          <li><strong>Measure</strong>: click two points — each snaps to a nearby candle or
            drawn line (the snap target lights up first). The label shows Δprice ($ and %)
            and Δtime (days and bar count): candle↔candle emphasizes time, line↔line
            emphasizes price.</li>
          <li><strong>Inspect</strong>: hover any candle for its OHLC, range, body and
            volume; click to pin the readout to that bar.</li>
          <li><strong>Clear M</strong> removes all measurements and pins; drawings stay.</li>
        </ul>
      </>
    ),
  },
  {
    id: 'multi-charts',
    title: 'Multi-symbol charts',
    body: (
      <>
        <p><code>charts.gs</code> (Main wheel → <em>Charts</em>) overlays tickers as lines:</p>
        <ul>
          <li><strong>Add</strong> symbols with the input, or from the Chart wheel →
            <em> Tickers → Add symbol</em> (it offers your open ticker tabs).</li>
          <li>Each legend chip has an <strong>eye</strong> (hide/show) and a
            <strong> ⦿ solo</strong> — solo isolates that ticker; your eye states are kept
            and restored exactly when you disable isolation.</li>
          <li><strong>% change</strong> rebases every line to percent from its first
            close — the honest way to compare differently priced symbols.</li>
          <li>Layout (symbols, timeframe, hidden, isolation) persists per user.</li>
        </ul>
      </>
    ),
  },
  {
    id: 'data',
    title: 'Data recording',
    body: (
      <>
        <p><code>data.gs</code> records market data on your machine, on your schedule:</p>
        <ul>
          <li>Jobs capture <strong>bars</strong>, <strong>options-chain snapshots</strong> or
            <strong> news</strong> for a symbol at a chosen interval, with per-job
            retention. When jobs share a table, the longest retention wins — one job can
            never delete another’s history.</li>
          <li>Recorded bars serve your charts when they cover the request — labeled
            <em> your recorded data</em>.</li>
          <li>The page shows per-job status and total store size.</li>
        </ul>
        <p><strong>Import a file</strong> takes a CSV or JSON export from any broker — an
          archived chain file imports unchanged. Pick the kind yourself: it is never
          guessed, because a chain file and a bar file are both “a CSV with numbers in
          it”, and a wrong guess surfaces months later inside a backtest. The whole file
          is validated before a single row is written, and a refusal names the line and
          the reason. An empty bid is stored as <em>not quoted</em>, never as zero.</p>
        <p><strong>Chain puller</strong> downloads end-of-day option chains one
          ticker-day every 6 seconds. It is off until you start it. The free source only
          serves a rolling ~6 months and has no greeks until a session closes, so the
          date range is clamped to what can actually be fetched and says so.</p>
        <p>Imported and pulled rows land in the <strong>backtest store</strong>, so data
          you bring is immediately usable by <code>backtest.gs</code> — the Backtest page
          names which source filled it rather than calling everything “recorded”.</p>
        <p><strong>Keep chart data for</strong> (Settings → Data) controls how long bars
          fetched for a chart are kept, so a chart still draws when the provider is
          unreachable. It never touches your recorded data.</p>
      </>
    ),
  },
  {
    id: 'backtest-help',
    title: 'Backtesting',
    body: (
      <>
        <p><code>backtest.gs</code> runs the SPY options backtest engine against recorded
          chain data. Every number it shows is <em>model-derived from end-of-day
          snapshots</em> — labeled estimated, never broker truth.</p>
        <ul>
          <li><strong>Data</strong>: the app owns its backtest store and fills it from
            your recorded chain snapshots — <em>Set up recording</em> creates the right
            jobs (hourly chains + daily bars), every run syncs the latest recordings in
            first, and <em>Sync recorded data</em> does it on demand. Machines with a
            full chain database (or a path set in Settings) use that instead; the Data
            card always says which source a run would read.</li>
          <li><strong>Presets</strong>: strategy specs (legs, entry, exits, sizing, costs)
            saved by name. The seeded built-ins include the three calibration references;
            built-ins are read-only — <em>Duplicate</em> one to make it yours. Edit with
            the <em>Form</em> (legs, exit toggles, sizing — it writes the spec for you)
            or as raw <em>JSON</em> for the full spec language; both compile to the same
            thing, and the editor validates as you type, naming exactly what it does not
            recognize.</li>
          <li><strong>Run</strong>: pick a preset (or edit a spec inline), optionally narrow
            the date window, and <em>Run backtest</em>. Runs execute in their own process —
            60–110 seconds for the full 13 years — and <em>Cancel</em> stops them cold.
            One run at a time.</li>
          <li><strong>Report</strong>: each finished run opens a full report in a browser
            tab — equity curve, drawdown, monthly heatmap, P/L distribution and the
            complete trade log with CSV export.</li>
          <li><strong>Verify engine</strong>: replays the three tastytrade reference
            backtests shipped with the app against the same data and compares fills,
            trades and the equity curve layer by layer. If you change engine code, this
            is how you prove it still makes the trades it is known to have made.</li>
          <li><strong>Data</strong>: the chain database (<code>spy_options.db</code>,
            multi-GB) does not ship with the app; the page says honestly whether it is
            present, and Settings holds its path.</li>
        </ul>
      </>
    ),
  },
  {
    id: 'settings-help',
    title: 'Settings explained',
    body: (
      <>
        <ul>
          <li><strong>Search the web / Web news</strong>: blend web results into search.</li>
          <li><strong>Web search engine</strong>: which backend answers; <em>brave</em> is
            the measured default.</li>
          <li><strong>In-house result boost</strong> (0–4): how strongly platform results
            outrank the web.</li>
          <li><strong>Candles per chart</strong>: chart history depth — <em>all</em> or a cap.</li>
          <li><strong>Theme</strong>: dark or light, including opened web pages.</li>
          <li><strong>Gesture wheels</strong>: the wheel editor lives at the bottom of the
            settings page.</li>
        </ul>
      </>
    ),
  },
  {
    id: 'troubleshooting',
    title: 'Troubleshooting',
    body: (
      <>
        <ul>
          <li><strong>“backend not running”</strong>: the Python sidecar could not start.
            From a clone, run the setup script (it creates <code>.venv</code> where the app
            looks first) and restart.</li>
          <li><strong>“no data source available”</strong>: add an Alpaca account, record
            bars from <code>data.gs</code>, or rely on the keyless daily fallback
            (1D timeframe).</li>
          <li><strong>A chart tool feels stuck</strong>: press <kbd>Esc</kbd> to cancel any
            half-placed drawing, or click <em>Ptr</em> to disarm.</li>
          <li><strong>Locked out?</strong> There is no password recovery — the password IS
            the encryption key. A new profile starts clean.</li>
        </ul>
      </>
    ),
  },
]

export function HelpPage({ section }: { section?: string }) {
  const scrolled = useRef<string | null>(null)

  useEffect(() => {
    if (!section || scrolled.current === section) return
    const el = document.getElementById(`help-${section}`)
    if (el) {
      scrolled.current = section
      el.scrollIntoView({ block: 'start' })
      el.classList.add('help-flash')
      const t = window.setTimeout(() => el.classList.remove('help-flash'), 1600)
      return () => window.clearTimeout(t)
    }
  }, [section])

  return (
    <div className="page help-page" data-help-current={section ?? ''}>
      <div className="page-head">
        <PageMiniIcon />
        <h1>Help</h1>
        <span className="dim">search any topic — “drawing”, “trim”, “isolate” — to jump straight here</span>
      </div>

      <div className="card help-toc">
        {SECTIONS.map((s) => (
          <a key={s.id} href={`#help-${s.id}`} onClick={(e) => {
            e.preventDefault()
            document.getElementById(`help-${s.id}`)?.scrollIntoView({ block: 'start' })
          }}>
            {s.title}
          </a>
        ))}
      </div>

      {SECTIONS.map((s) => (
        <div className="card help-sec" id={`help-${s.id}`} key={s.id}>
          <h2>{s.title}</h2>
          <div className="help-body">
            <div className="help-text">{s.body}</div>
            {s.fig ? <div className="help-figwrap">{s.fig()}</div> : null}
          </div>
        </div>
      ))}
    </div>
  )
}
