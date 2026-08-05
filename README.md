# Grindstone

A desktop market-research app that behaves like a **web browser**: one omnibox
that searches tickers, news and pages alike; everything opens as tabs that tear
off and regroup across OS windows like Chrome; split view; and a charting
surface with drawing and measuring tools.

It runs as a real desktop app — Electron shell, Python (FastAPI) sidecar,
SQLite storage. Everything lives inside the folder you clone it into.

> **Read this before you install.**
>
> - **It cannot place trades.** Every broker adapter is read-only today
>   (`order_entry: False`). It reads quotes, news, positions and account data.
>   Order entry is a later milestone. Nothing here can move your money.
> - **It is one person's project, early in its life.** Expect rough edges and
>   breaking changes. "Grindstone" is a placeholder name.
> - **Verified on Windows and Linux. macOS is untested** — the installer is
>   written but has never run on a Mac. See [Platform support](#platform-support).

## What works today

- **Omnibox search** over ~14k tickers (fuzzy + prefix), news (full-text), and
  app pages, with an intent grammar — typing `SPY news` returns scoped
  headlines.
- **Symbol pages** — live quote labelled with its source, plus recent news.
- **Charts** — candles with trend/horizontal/vertical/circle drawing, snap
  measurements, candle inspect, indicator settings, per-ticker visibility.
- **News reader** — article content extracted and rendered in-app.
- **Backtesting** — a calibrated SPY options engine with 120 known-answer tests
  running in the verification gate. Strategies are editable as a form or as raw
  JSON; runs happen in a killable subprocess with live progress and an HTML
  report.
- **Data recording** — scheduled jobs capturing bars, option-chain snapshots and
  news, with retention pruning.
- **Favorites and gesture wheels** — a starred home grid and radial menus.

**Not built yet:** order entry, the AI assistant, TastyTrade, Webull and
Fidelity adapters, and session restore. Roadmap: `code/docs/REQUIREMENTS.md` §10.

## Do I need a broker account?

**No.** Without any credentials you still get quotes and daily charts through a
keyless, delayed fallback (clearly labelled as delayed in the UI), plus search,
news and backtesting.

With a free **Alpaca paper** account you additionally get real-time IEX quotes,
option chains, richer news, and your paper positions. Paper keys cannot touch
real money, and the app is read-only regardless.

## Requirements

| | |
|---|---|
| **OS** | Windows 10/11, or Linux with a desktop environment. macOS untested. |
| **Disk** | ~650 MB installed (437 MB Node modules, 181 MB Python venv) |
| **Prerequisites** | None — the installer fetches Python 3.12+ and Node.js 20+ if missing |
| **Internet** | Needed to install, and for live data afterwards |

## Install

Clone the repo, then run the installer in the repo root:

| | |
|---|---|
| **Windows** | double-click `Install.cmd` |
| **Linux** | `./install.sh` — most file managers won't run a `.sh` on double-click |
| **macOS** | double-click `Install.command` *(untested)* |

```bash
git clone https://github.com/KadePrice123/Grindstone.git
cd Grindstone
```

A window opens, asks which shortcuts you want, and does everything else:
installs Python and Node if they're missing, creates `.venv/` inside the clone,
installs dependencies, builds the app, runs the verification gate, and creates
your shortcuts. It's built on what each OS already ships (WinForms, `osascript`,
`zenity`/`kdialog`, or a plain terminal prompt), so it has no prerequisite of
its own.

Measured on clean machines: **about 40 seconds** if you already have Python and
Node, **3–4 minutes** if the installer has to fetch them first.

**Windows:** installing Node.js raises a UAC prompt, because its installer is
machine-scope. That one is unavoidable.

**Linux:** Debian and Ubuntu ship Python without `ensurepip`, so creating a
virtualenv needs one system package. The installer handles it when it can do so
without stopping to ask — when you're root, or `sudo` is already authorized.
Otherwise it exits in a couple of seconds and prints the command to run. An
installer can't answer a password prompt, so it refuses to start one rather than
appear to hang:

```bash
sudo apt-get install python3-venv     # then run ./install.sh again
```

Afterwards Grindstone opens from its desktop icon or applications menu like any
other program — no terminal, no `npm`. The shortcut points at the Electron
binary inside the clone, so **the app stays wherever you cloned it**. Move the
folder and you'll need to rerun the installer.

## First run

1. **Create a profile.** Your password *is* the encryption key for the local
   vault. There is **no recovery** — this is deliberate, not an oversight. Lose
   it and the stored credentials are unrecoverable.
2. **Optionally add a broker.** Accounts → add your Alpaca paper key → Test →
   Save. Skip this and everything keyless still works.
3. **Search something.** Type a ticker in the omnibox.

## Your data and your keys

Everything stays on your machine. There is no telemetry and no account system.

- **Where:** inside the clone — `data/` for the databases, `.venv/` for Python.
  Override the data location with the `GRINDSTONE_DATA_DIR` environment
  variable.
- **Broker keys:** encrypted with a key derived from your profile password
  (envelope encryption; each secret is bound to its user, account and field, so
  rows can't be swapped between accounts). The database is scanned in the test
  suite to prove no plaintext key is recoverable from the file.
- **There is no config file to edit and no environment variable to set.**
  Credentials go in through Accounts, nowhere else.

## Updating

```bash
git pull
```

Then rerun the installer to pick up new dependencies and rebuild. It skips
whatever is already present, so it's much faster than the first run. Your
`data/` and profile are untouched.

## Uninstalling

Everything lives in the clone, so there's little to clean up:

1. Delete the `Grindstone` folder — this removes the app, its virtualenv, its
   dependencies and your local data.
2. Delete the shortcuts: the desktop icon, and `Grindstone.lnk` from the Start
   Menu (Windows) or `~/.local/share/applications/grindstone.desktop` (Linux).

Python and Node, if the installer added them, are normal system installs — keep
them or remove them via your usual package manager.

## Troubleshooting

Every install writes `grindstone-setup.log` to your temp directory with the full
output of every step.

- **"Python installed but is still not on PATH"** — a newly installed
  interpreter isn't visible to already-running processes. Sign out and back in,
  then rerun.
- **The window opens but says "backend not running"** — the sidecar couldn't
  find a Python with the dependencies. Rerun the installer; it creates `.venv`
  where the app looks first.
- **Gate fails** — the `FAIL` line names the exact check and reason. The install
  is incomplete until it passes.
- **The gate is skipped** — it shells out to `git ls-files` to scan for secrets,
  so it needs git and a real clone. A ZIP download runs the app but can't run
  the gate.
- **`market refresh skipped — alpaca data: keys rejected (401/403)` during the
  gate** — expected, and not your key. The gate creates a throwaway profile with
  fixture credentials and the background refresh tries them for real.
- **Linux: "Could not create a virtualenv"** — see the Linux note above. Run
  `sudo apt-get install python3-venv` (Ubuntu 26.04 also accepts the versioned
  `python3.14-venv`), then rerun.
- **Linux: the desktop icon does nothing** — GNOME won't run a launcher it
  doesn't trust. Right-click → "Allow launching".
- **Linux: no desktop icon appeared** — there was no `~/Desktop` (common on
  servers, containers and WSL). The applications-menu entry is still created.
- **Windows: "Pin to taskbar" didn't happen** — Windows 10 1809 removed the API
  that let installers do it. Right-click the Start Menu shortcut and pin it.
- **macOS: "Grindstone.app cannot be opened"** — the wrapper bundle is unsigned.
  Right-click → Open once, or `xattr -dr com.apple.quarantine
  ~/Applications/Grindstone.app`. The Dock tile may read "Electron"; only real
  packaging fixes that (REQUIREMENTS.md §6.8).

## Platform support

| Platform | Status |
|---|---|
| **Windows 11** | Verified end to end 2026-08-05 from a clean machine with neither Python nor Node installed. Gate green. |
| **Linux** | Verified end to end 2026-08-05 on a freshly created Ubuntu 26.04 (Python 3.14.4, no `ensurepip`). Gate green. |
| **macOS** | **Untested.** Written but never run on real hardware. Treat `Install.command` as unverified. |

---

## For developers

<details><summary>Running from source</summary>

```bash
cd code/app
npm run dev      # hot reload
npm run start    # run the production build (electron-vite preview, serves out/)
```

`npm run start` is *preview* — it serves `out/`, it never builds it. Run
`npm run build` first on a fresh clone.

Electron main locates the backend's Python automatically (`.venv` in the clone
first, then `python` on PATH), spawns the sidecar, waits for its
`{"event":"listening","port":N}` line, health-checks it, and only then shows the
window.
</details>

<details><summary>Verification gate</summary>

```bash
cd code
python selftest.py     # the LAST line must read: SELFTEST OK <n>/<n>
```

Declared in `checkpoint.json`. The count grows with the project — every
production bug becomes a permanent check. Covers secret hygiene, envelope
encryption with AAD tamper detection, the auth and accounts API flow with a
stolen-database plaintext scan, broker parser fixtures, the read-only broker
invariant, session expiry, search ranking, gesture wheels, split view, the chart
tool engine, the backtest engine's 120 known-answer tests, the installer surface
(shebangs, exec bits, line endings, icon integrity, and that the shortcut target
matches what the build produces), and the frontend typecheck.

Known gap: the gate is described as offline but currently makes one live call to
Alpaca, because booting the app triggers a background market refresh. See
`code/docs/NOTES.md`.

Live connectivity is a separate diagnostic: `cd code/app && npm run e2e`.
</details>

<details><summary>Unattended / CI install</summary>

No window, no shortcuts:

```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1   # Windows
```

```bash
./setup.sh                                           # Linux / macOS
```

`Install.cmd -NoUi` and `./install.sh --no-ui` do the same but keep the
shortcuts.
</details>

<details><summary>Doing it by hand</summary>

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
cd code\app
npm install
npm run build
cd ..
..\.venv\Scripts\python.exe selftest.py
```
</details>

<details><summary>Repository layout</summary>

| Path | What | In git |
|---|---|---|
| `code/` | app source — Electron shell, Python sidecar | yes |
| `code/docs/` | requirements, research, working notes | yes |
| `code/assets/branding/` | logos, icons, `branding.json` | yes |
| `tools/installer/` | the double-click installers, per platform | yes |
| `tools/icons/` | regenerates `app.ico` / `app.icns` / PNGs from `logo.svg` | yes |
| `data/` | databases: quotes cache, news, backtest data | no |
| `.venv/` | Python virtualenv, created by the installer | no |

Branding is swappable: every name, colour and icon resolves through
`code/assets/branding/branding.json`.
</details>

## Documentation

- [`code/docs/REQUIREMENTS.md`](code/docs/REQUIREMENTS.md) — the full
  requirements document and roadmap.
- [`code/docs/RESEARCH.md`](code/docs/RESEARCH.md) — the research and sources
  behind the technical decisions.
- [`code/docs/NOTES.md`](code/docs/NOTES.md) — **the open-work list** (every
  known issue in one table at the top), plus session-by-session working notes
  and the gotchas worth not rediscovering.

## License

MIT — see [LICENSE](LICENSE).
