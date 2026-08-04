# Grindstone — the Grindstone Trading dashboard (placeholder name)

A desktop trading platform that looks and behaves like a **web browser**: one
omnibox search bar, everything opens as tabs that tear off and regroup across OS
windows like Chrome, an embedded AI (Claude via Open WebUI + MCP), and
multi-broker trading (Alpaca, TastyTrade; Webull/Fidelity behind flags).

**Start here → [`code/docs/REQUIREMENTS.md`](code/docs/REQUIREMENTS.md)** — the
full requirements document. Research backing its technical decisions:
[`code/docs/RESEARCH.md`](code/docs/RESEARCH.md).

## Layout

| Path | What | In git |
|---|---|---|
| `code/docs/` | requirements + research docs | yes |
| `code/assets/branding/` | logos, icons, branding.json, preview.html | yes |
| `code/` | app source (shell, sidecar, tools) | yes |
| `data/` | bars cache, news store, vector DB | no — Drive-backed |
| `env/` | broker keys (`alpaca.env`, …) | no — never leaves this machine |
| `../../venvs/dashboard/` | virtualenv | no — rebuild from requirements.txt |

## Setup from a fresh clone

Prerequisites (install these first, everything else is automatic):

- **Python 3.12+** — python.org or the Microsoft Store
- **Node.js 20+ (LTS)** — nodejs.org

Then one command from the repo root:

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File setup.ps1
```

```bash
# Linux / macOS
./setup.sh
```

That creates `.venv/` inside the clone, installs the Python and frontend
dependencies, and finishes by running the offline verification gate — if its
last line is `SELFTEST OK …`, the install is complete and correct.

<details><summary>Manual steps (what the script does)</summary>

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
cd code\app
npm install
cd ..
..\.venv\Scripts\python.exe selftest.py
```
</details>

## Run the app

```bash
cd code/app
npm run dev      # dev mode with hot reload
npm run start    # run the production build (electron-vite preview)
```

Electron main finds the backend's Python automatically (`.venv` in the clone
first, then this workspace's venv, then `python` on PATH), spawns the sidecar,
waits for its `{"event":"listening","port":N}` stdout line, health-checks it,
and only then shows the window. First run: create a profile (that password
*is* the vault key — no recovery by design), then Accounts → add your Alpaca
paper key → Test → Save. No broker account? Quotes and daily charts still
work through the keyless delayed fallback.

## Verification gate

Offline, declared in `checkpoint.json`:

```bash
cd code
python selftest.py     # the LAST line must read: SELFTEST OK <n>/<n>
```

The count grows with the project — every production bug becomes a permanent
check. Covered today: secret hygiene, envelope-encryption round-trip with AAD
tamper detection, the full offline auth+accounts API flow with a stolen-DB
plaintext scan, broker parser fixtures, the read-only Alpaca invariant,
session expiry/wipe, search ranking, the gesture-wheel system, split view,
the chart tool engine, and the frontend typecheck. Live connectivity stays a
separate diagnostic (`cd code/app && npm run e2e`), never part of the gate.

## Troubleshooting a fresh install

- **`MISSING: python` / `MISSING: npm`** — install the prerequisites above,
  reopen the terminal so PATH refreshes, rerun the script.
- **The window opens but says "backend not running"** — the sidecar could not
  find a Python with the dependencies. Run the setup script (it creates
  `.venv` where the app looks first), or activate your own venv before
  `npm run start`.
- **Gate fails** — the FAIL lines name the exact check and reason; the
  install is incomplete until it passes.

## Distribution

- **Source**: https://github.com/KadePrice123/Grindstone — pushed at major
  completed milestones. Secrets never leave this machine: `env/` and `data/`
  are gitignored and the gate scans every tracked file before a push.
- **Installers**: GitHub Releases on the same repo (download-and-install
  without the source); the auto-updater reads the same Releases feed.
- **OS targets**: Windows now; Linux committed next; macOS pending the Apple
  Developer decision (REQUIREMENTS.md §6.8).

## Status

M1 (spine) + search/data (early M4 slice) — the app boots and *works*:
- Omnibox with live results: ~14k tickers (Alpaca assets + SPX/VIX//ES
  supplement), fuzzy + prefix matching, news search (FTS5 trigram), page
  routing, and an intent grammar — `SPY news` answers scoped headlines with a
  live-Alpaca fallthrough when the local store is thin.
- Symbol pages: live IEX quote (labeled with its source) + recent news.
- Data management: recording jobs for bars / options-chain snapshots / news
  at chosen intervals with retention pruning; verified live with a 13,897-
  contract SPY chain snapshot including greeks.
- Yahoo Finance keyless fallback (delayed, labeled) for users with no data API.
- Backtesting (`backtest.gs`, 2026-08-03): the workspace's calibrated SPY
  options backtest engine, vendored at `code/backend/bt/` with its 120
  known-answer tests in the gate. Strategy presets in the DB (seeded with the
  three tastytrade calibration references + four showcase strategies), runs in
  a killable subprocess with live progress, full HTML reports in a tab, and a
  "Verify engine" flow that replays the shipped reference exports against the
  exact data — the regression harness for anyone modifying engine code.
- Backtest data is self-serve (2026-08-03): the app owns its store at
  `data/backtest_data/<SYM>.db` (created automatically, both engine tables in
  one file) and fills it from recorded chain/bars snapshots — one click on the
  page wires the recording jobs, every run syncs the newest recordings first,
  and the whole pipeline is gate-checked end to end on synthetic
  arbitrage-clean chains. Machines with a full `spy_options.db` (or a
  Settings path) use that instead; the page always says which source a run
  reads and how much recorded history exists. Specs are editable two ways:
  a form (legs, exit toggles, sizing) for humans, raw JSON for the full spec
  language and future AI agents — both compile to the same spec.
  Pickup notes for in-flight work: `code/docs/NOTES.md`.
Roadmap: REQUIREMENTS.md §10. Alpaca paper key verified working (in
`env/alpaca.env`).
