# dashboard

Browser-style multi-broker trading platform (Grindstone Investments placeholder) - desktop app, Python backend, web frontend, AI via OpenWebUI/MCP

## Layout

| Path | What | In git |
|---|---|---|
| `code/` | scripts | yes |
| `data/` | bulk data | no — backed up by Google Drive |
| `env/` | secrets, loaded by path from code | no — never leaves this machine |
| `../../venvs/dashboard/` | virtualenv | no — rebuild from requirements.txt |

## Setup

```bash
python -m venv ../../venvs/dashboard
../../venvs/dashboard/Scripts/python.exe -m pip install -r requirements.txt
```

## Run

```bash
../../venvs/dashboard/Scripts/python.exe code/<entrypoint>.py
```

## Verification gate

TODO: one command with a written-down expected output, run after any structural
change. Until this is filled in, there is no way to know a move broke something.

```bash
python ../../tools/checkpaths.py dashboard    # all hardcoded paths still resolve
```
