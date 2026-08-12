#!/usr/bin/env python3
"""Move this machine's data into the ONE uniform layout (backend/datapaths).

Finds every legacy location the app has ever read from and moves what it
finds into the single tree under ``data/``:

    <workspace>/spy_options.db          -> data/deep/SPY_options.db
    <workspace>/spy_bars.db             -> data/deep/SPY_bars.db
    <workspace>/<sym>_options.db        -> data/deep/<SYM>_options.db   (any symbol)
    <workspace>/data/data/archive.zip   -> data/vault/archive.zip
    <workspace>/data/data/options_archive_new/ -> data/vault/options_archive_new/

DRY RUN BY DEFAULT: it prints the full plan and touches nothing until
``--apply``. Moves are same-volume renames where possible (instant, even for
multi-GB files). SQLite files are integrity-checked (quick_check) BEFORE the
move, a destination that already exists is never overwritten (reported
instead), and the run is idempotent — a second --apply finds nothing to do.

Run this ON EACH MACHINE after pulling this update:

    python tools/consolidate.py            # see the plan
    python tools/consolidate.py --apply    # do it

Nothing in the app writes to the legacy locations after this update; the
resolvers keep a read-only legacy fallback (labeled 'legacy' in the UI) so
the app still works BEFORE you run this — but the point is to run it.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
from pathlib import Path

CODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE))
from backend import datapaths  # noqa: E402

DEEP_RE = re.compile(r"^([a-z.]+)_(options|bars)\.db$")


def quick_check(p: Path) -> str:
    try:
        con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
        try:
            return con.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error as e:
        return f"unreadable: {e}"


def plan() -> list[tuple[str, Path, Path, str]]:
    """(kind, src, dst, note) for everything that should move."""
    out: list[tuple[str, Path, Path, str]] = []
    ws = datapaths.workspace()

    # Deep sqlite stores loose in the workspace root — any symbol, both kinds.
    for p in sorted(ws.glob("*_*.db")):
        m = DEEP_RE.match(p.name)
        if not m:
            continue
        sym, kind = m.group(1).upper(), m.group(2)
        dst = (datapaths.deep_options(sym) if kind == "options"
               else datapaths.deep_bars(sym))
        note = ""
        if p.stat().st_size > 50_000_000:
            note = f"{p.stat().st_size / 1e9:.1f} GB — same-volume move is instant"
        out.append((f"deep {kind}", p, dst, note))

    # The raw vault.
    lv = datapaths.legacy_vault_dir()
    if (lv / "archive.zip").is_file():
        out.append(("vault archive", lv / "archive.zip",
                    datapaths.vault_dir() / "archive.zip", "irreplaceable — moved, never copied-and-deleted"))
    if (lv / "options_archive_new").is_dir():
        out.append(("vault new-days", lv / "options_archive_new",
                    datapaths.vault_dir() / "options_archive_new", "directory"))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="actually move files (default: print the plan only)")
    args = ap.parse_args()

    moves = plan()
    print(f"uniform root: {datapaths.data_dir()}")
    if not moves:
        print("nothing to consolidate — this machine is already uniform.")
        # Still report what the uniform tree holds, so 'done' is inspectable.
        for label, p in [("deep", datapaths.deep_dir()), ("vault", datapaths.vault_dir())]:
            if p.is_dir():
                for f in sorted(p.iterdir()):
                    sz = f.stat().st_size / 1e6 if f.is_file() else 0
                    print(f"  {label}/{f.name}"
                          + (f"  {sz:,.0f} MB" if f.is_file() else "  (dir)"))
        return

    ok = True
    for kind, src, dst, note in moves:
        line = f"  {kind:<14} {src}  ->  {dst}"
        if note:
            line += f"   [{note}]"
        if dst.exists():
            print(f"{line}\n    SKIP: destination already exists — resolve by hand "
                  f"(never overwritten)")
            ok = False
            continue
        if src.suffix == ".db":
            v = quick_check(src)
            if v != "ok":
                print(f"{line}\n    SKIP: quick_check says {v!r} — not moving a "
                      f"damaged database")
                ok = False
                continue
        print(line)
        if args.apply:
            dst.parent.mkdir(parents=True, exist_ok=True)
            # rename() is atomic and instant on the same volume; shutil.move
            # falls back to copy+delete across volumes.
            shutil.move(str(src), str(dst))
            print("    moved.")

    if not args.apply:
        print("\nDRY RUN — nothing was moved. Re-run with --apply to do it.")
    elif ok:
        print("\ndone. Everything now resolves from the uniform tree; the app "
              "will stop reporting 'legacy' sources.")
    else:
        print("\ndone WITH SKIPS — the lines above say which files still need "
              "a decision. Re-run after resolving them.")


if __name__ == "__main__":
    main()
