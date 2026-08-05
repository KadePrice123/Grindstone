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
| `tools/installer/` | the double-click installers, per platform | yes |
| `tools/icons/` | regenerates `app.ico` / `app.icns` / PNGs from `logo.svg` | yes |
| `data/` | bars cache, news store, vector DB | no — Drive-backed |
| `env/` | broker keys (`alpaca.env`, …) | no — never leaves this machine |
| `.venv/` | virtualenv, created by the installer | no — rebuild from requirements.txt |

## Install

Clone the repo, then **double-click the installer in the repo root**:

| | |
|---|---|
| **Windows** | `Install.cmd` |
| **macOS** | `Install.command` |
| **Linux** | `./install.sh` (most file managers will not run a `.sh` on double-click) |

A window opens, asks which shortcuts you want, and does the rest: it installs
**Python 3.12+ and Node.js 20+ if they are missing**, creates `.venv/` inside
the clone, installs the Python and frontend dependencies, builds the app, runs
the verification gate, and creates the shortcuts you ticked. Nothing needs to
be installed beforehand — the installer is built on what each OS already ships
(WinForms, `osascript`, `zenity`/`kdialog`, or a plain terminal prompt).

Afterwards Grindstone launches from its desktop icon or your applications menu
like any other program: no terminal, no `npm`. The shortcut points straight at
the Electron binary in the clone, so the app stays wherever you cloned it —
move the clone and you will need to rerun the installer.

<details><summary>Unattended / CI</summary>

The same work with no window and no shortcuts:

```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1   # Windows
```

```bash
./setup.sh                                           # Linux / macOS
```

`Install.cmd -NoUi` and `./install.sh --no-ui` do the same but keep the
shortcuts. Both paths write a full log to your temp directory
(`grindstone-setup.log`).
</details>

<details><summary>What it does, by hand</summary>

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
cd code\app
npm install
npm run build          # `npm run start` is preview: it serves out/, never builds it
cd ..
..\.venv\Scripts\python.exe selftest.py
```
</details>

## Run the app

From the desktop shortcut or applications menu, or from a terminal:

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
the chart tool engine, the installer surface (shebangs, exec bits, line
endings, icon integrity, and that the shortcut target matches what the build
produces), and the frontend typecheck. Live connectivity stays a separate
diagnostic (`cd code/app && npm run e2e`), never part of the gate.

## Troubleshooting a fresh install

Every install writes `grindstone-setup.log` to the temp directory; it has the
full output of every step.

- **"Python installed but is still not on PATH"** — a new interpreter is not
  visible to already-running processes. Sign out and back in, then rerun.
- **The window opens but says "backend not running"** — the sidecar could not
  find a Python with the dependencies. Rerun the installer (it creates `.venv`
  where the app looks first), or activate your own venv before `npm run start`.
- **Gate fails** — the FAIL line names the exact check and reason; the install
  is incomplete until it passes.
- **The gate is skipped** — it shells out to `git ls-files` to scan tracked
  files for secrets, so it needs git and a real clone. A ZIP download runs the
  app fine but cannot run the gate.
- **macOS: "Grindstone.app cannot be opened"** — the wrapper bundle is
  unsigned. Right-click it and choose Open once, or `xattr -dr
  com.apple.quarantine ~/Applications/Grindstone.app`. The Dock tile may read
  "Electron": the wrapper runs Electron's own binary, and only real packaging
  (REQUIREMENTS.md §6.8) fixes that.
- **Linux: the desktop icon does nothing** — GNOME will not run a launcher it
  does not trust. Right-click it and choose "Allow launching".
- **Windows: "Pin to taskbar" did not happen** — Windows 10 1809 removed the
  API that let installers do it. The Start Menu shortcut is created; right-click
  it and pin from there.

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
