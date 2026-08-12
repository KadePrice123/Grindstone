"""THE data layout — every file the app reads or writes, named in one place.

Before this module, data lived in three worlds: the app's own ``data/``
directory, multi-GB deep stores loose in the workspace root
(``<workspace>/spy_options.db``), and the raw vault at
``<workspace>/data/data/``. Three worlds means three resolution rules, and
every tool re-derived them — the bake tool, the backtest engine and the
loader each had a private opinion about where SPY's history lives.

Now the layout is ONE tree under ``data_dir()``:

    data/
      app.db  market.db  options_history.db        (as before)
      backtest_data/<SYM>.db                       (engine stores, as before)
      deep/<SYM>_options.db, <SYM>_bars.db         (the multi-GB read stores)
      vault/archive.zip, options_archive_new/      (the raw daily archive)

``tools/consolidate.py`` MOVES a machine's legacy files into this tree; the
``legacy_*`` helpers here are how callers keep working before it has run —
each names its fallback so the UI can say "found in the legacy workspace —
run tools/consolidate.py" instead of silently depending on a layout this
module exists to retire. Pure stdlib, no app imports beyond db.data_dir.
"""
from __future__ import annotations

from pathlib import Path

from .db import data_dir


def deep_dir() -> Path:
    return data_dir() / "deep"


def vault_dir() -> Path:
    return data_dir() / "vault"


def deep_options(symbol: str) -> Path:
    """The deep chain store (2008-> for SPY): 25M rows, delta on every row,
    the bake's input and the backtest engine's preferred SPY source."""
    return deep_dir() / f"{symbol.upper()}_options.db"


def deep_bars(symbol: str) -> Path:
    return deep_dir() / f"{symbol.upper()}_bars.db"


def workspace() -> Path:
    """The directory the project folder sits in — the LEGACY home of the deep
    stores, kept addressable only so consolidation and fallbacks can name it."""
    return data_dir().parent.parent


def legacy_deep_options(symbol: str) -> Path:
    return workspace() / f"{symbol.lower()}_options.db"


def legacy_deep_bars(symbol: str) -> Path:
    return workspace() / f"{symbol.lower()}_bars.db"


def legacy_vault_dir() -> Path:
    return workspace() / "data" / "data"


def resolve_deep_options(symbol: str) -> tuple[Path | None, str]:
    """The deep store and WHERE it was found: 'uniform' | 'legacy' | absent.

    The legacy rung exists so nothing breaks between pulling this code and
    running the consolidator; the caller must surface the word 'legacy' so
    the migration actually happens instead of becoming a fourth layout."""
    p = deep_options(symbol)
    if p.is_file():
        return p, "uniform"
    q = legacy_deep_options(symbol)
    if q.is_file():
        return q, "legacy"
    return None, "absent"


def resolve_deep_bars(symbol: str) -> tuple[Path | None, str]:
    p = deep_bars(symbol)
    if p.is_file():
        return p, "uniform"
    q = legacy_deep_bars(symbol)
    if q.is_file():
        return q, "legacy"
    return None, "absent"


def resolve_vault_dir() -> tuple[Path | None, str]:
    p = vault_dir()
    if (p / "archive.zip").is_file() or (p / "options_archive_new").is_dir():
        return p, "uniform"
    q = legacy_vault_dir()
    if (q / "archive.zip").is_file() or (q / "options_archive_new").is_dir():
        return q, "legacy"
    return None, "absent"
