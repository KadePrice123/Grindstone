# dashboard — Grindstone Investments (placeholder name)

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

## Verification gate

Offline, declared in `checkpoint.json`, run by `checkpoint.py`:

```bash
cd code; python selftest.py        # expect: SELFTEST OK 6/6
```

M0 scope: branding assets valid + themable, requirements doc complete, no
credential-shaped strings in tracked files, env hygiene. The count grows with
each milestone (see REQUIREMENTS.md §9-§10); live connectivity will be a
separate diagnostic, never part of the gate.

## Status

M0 (scaffold + requirements + branding) — see roadmap in REQUIREMENTS.md §10.
Alpaca paper key verified working 2026-08-01 (stored in `env/alpaca.env`).
