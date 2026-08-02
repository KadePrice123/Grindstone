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

## Setup

```bash
python -m venv ../../venvs/dashboard
../../venvs/dashboard/Scripts/python.exe -m pip install -r requirements.txt
```

Frontend toolchain (Node lives portably in `Claude/runtimes/node`, and
`code/app/node_modules` is a junction into `Claude/venvs/dashboard-node_modules`
so Drive never mirrors it):

```bash
cd code/app
npm install
```

## Run the app

```bash
cd code/app
npm run dev      # dev mode with hot reload
npm run start    # run the production build (electron-vite preview)
```

Electron main spawns the Python sidecar from the venv, waits for its
`{"event":"listening","port":N}` stdout line, health-checks it, and only then
shows the window. First run: create a profile (that password *is* the vault
key — no recovery by design), then Accounts → add your Alpaca paper key →
Test → Save.

## Verification gate

Offline, declared in `checkpoint.json`, run by `checkpoint.py`:

```bash
cd code; python selftest.py        # expect: SELFTEST OK 12/12
```

M0: branding + docs + secret hygiene. M1 adds: envelope-encryption round-trip
with AAD tamper detection, the full offline auth+accounts API flow with a
stolen-DB plaintext scan, Alpaca parser fixtures, the read-only Alpaca
invariant, session expiry/wipe, and the frontend typecheck. Live connectivity
stays a separate diagnostic, never part of the gate.

## Distribution

- **Source**: https://github.com/KadePrice123/Grindstone — pushed at major
  completed milestones. Secrets never leave this machine: `env/` and `data/`
  are gitignored and the gate scans every tracked file before a push.
- **Installers**: GitHub Releases on the same repo (download-and-install
  without the source); the auto-updater reads the same Releases feed.
- **OS targets**: Windows now; Linux committed next; macOS pending the Apple
  Developer decision (REQUIREMENTS.md §6.8).

## Status

M1 (spine) — the app boots: Electron shell + supervised Python sidecar,
login/first-run, idle page, Accounts page storing envelope-encrypted Alpaca
keys with live connection test. Roadmap: REQUIREMENTS.md §10.
Alpaca paper key verified working 2026-08-01 (stored in `env/alpaca.env`).
