"""A rule-driven options backtest engine, calibrated against tastytrade.

Vendored from the workspace's standalone btengine project (2026-08-03); this
package is now the engine's sole home.  Two deliberate deviations from the
original:

- No re-exports here.  The original __init__ imported every submodule, which
  pulls numpy into any process that touches the package.  The sidecar imports
  only the pure-Python validation surface (`spec`, `expr`, `tastyio`); the
  numpy-heavy modules load in the runner subprocess (backend/bt_runner.py)
  where a freeze or a kill cannot take the API down with it.
- Strategy presets ship as JSON (presets/), not YAML, so the engine's optional
  PyYAML dependency stays optional.

Import submodules explicitly: `from backend.bt import spec`.
"""

__version__ = "1.0.0"
