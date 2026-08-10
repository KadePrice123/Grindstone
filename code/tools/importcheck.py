#!/usr/bin/env python3
"""Can every backend module be IMPORTED on this platform?

The sidecar imports its whole module graph at start-up, so a module that
raises on import does not degrade one feature — it takes the process down
before it can serve anything. That failure is platform-specific by nature
(`ctypes.wintypes` does not exist off Windows), so it cannot be caught on the
machine the code was written on.

    python tools/importcheck.py

Exit 1 if anything fails to import. Run it on every platform the app claims to
support, which is the point.
"""
from __future__ import annotations

import importlib
import platform
import sys
from pathlib import Path

CODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE))

#: Every module under backend/, found rather than listed — a hand-maintained
#: list silently stops covering the module somebody adds next.
def modules() -> list[str]:
    out = []
    for p in sorted((CODE / "backend").rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        rel = p.relative_to(CODE).with_suffix("")
        name = ".".join(rel.parts)
        if name.endswith(".__init__"):
            name = name[: -len(".__init__")]
        out.append(name)
    return out


def main() -> int:
    print(f"python {platform.python_version()} on {sys.platform} "
          f"({platform.system()} {platform.release()})")
    bad: list[tuple[str, str]] = []
    skipped: list[str] = []
    for name in modules():
        # The engine's own subprocess modules are allowed to want numpy; the
        # sidecar never imports them (backend/bt/__init__.py explains why).
        heavy = name.startswith("backend.bt") or name == "backend.bt_runner"
        try:
            importlib.import_module(name)
        except ImportError as e:
            if heavy:
                skipped.append(f"{name} ({e.__class__.__name__}: {e})")
                continue
            bad.append((name, f"{e.__class__.__name__}: {e}"))
        except Exception as e:  # noqa: BLE001 — any raise on import is fatal
            bad.append((name, f"{e.__class__.__name__}: {e}"))
    for name in skipped:
        print(f"skip  {name}")
    for name, err in bad:
        print(f"FAIL  {name}: {err}")
    total = len(modules())
    if bad:
        print(f"IMPORTCHECK FAILED {total - len(bad)}/{total}")
        return 1
    print(f"IMPORTCHECK OK {total}/{total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
