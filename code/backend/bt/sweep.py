"""Parameter sweeps: run one strategy across a grid of settings.

Overrides are applied to the *raw* spec dictionary before it is compiled, so a
swept parameter is anything a strategy file can express -- a leg's delta, a DTE
target, an exit threshold, a sizing fraction, a cost assumption:

    legs[0].delta          leg by position
    legs.short_put.delta   leg by name
    exits.take_profit      exit shorthand
    entry.max_active       any nested key
    costs.slippage

Paths are validated against the base spec before a single backtest runs, so a
typo fails in a second rather than forty minutes in.
"""
from __future__ import annotations

import copy
import datetime as dt
import itertools
import json
import os
import re
from dataclasses import dataclass, field

from .engine import Backtester
from .spec import SpecError, Strategy
from .stats import summarize

_IDX = re.compile(r"^(\w+)\[(\d+)\]$")

#: Metrics a sweep can be ranked or coloured by.  True == higher is better.
METRICS = {
    "cagr_pct": True, "total_return_pct": True, "final_net_liq": True,
    "sharpe": True, "sortino": True, "calmar": True, "profit_factor": True,
    "win_rate_pct": True, "avg_pnl": True, "total_pnl": True, "expectancy": True,
    "max_drawdown_pct": True,          # negative; closer to zero is better
    "vol_annual_pct": False, "trades": True, "avg_dit": False, "total_fees": False,
}


class SweepError(ValueError):
    pass


# ---------------------------------------------------------------------- paths
def _walk(node, parts, path):
    """Return (container, key) for the final path element."""
    for i, raw in enumerate(parts):
        last = i == len(parts) - 1
        m = _IDX.match(raw)
        if m:
            name, idx = m.group(1), int(m.group(2))
            if not isinstance(node, dict) or name not in node:
                raise SweepError(f"{path}: no key {name!r} at this level")
            seq = node[name]
            if not isinstance(seq, list) or idx >= len(seq):
                raise SweepError(f"{path}: {name}[{idx}] is out of range")
            if last:
                return seq, idx
            node = seq[idx]
            continue
        if isinstance(node, list):
            # Address a list element by its `name` field, e.g. legs.short_put
            hit = next((j for j, e in enumerate(node)
                        if isinstance(e, dict) and e.get("name") == raw), None)
            if hit is None:
                names = [e.get("name") for e in node if isinstance(e, dict)]
                raise SweepError(f"{path}: no entry named {raw!r} (have {names})")
            if last:
                return node, hit
            node = node[hit]
            continue
        if not isinstance(node, dict):
            raise SweepError(f"{path}: cannot descend into {type(node).__name__} at {raw!r}")
        if last:
            return node, raw
        if raw not in node:
            raise SweepError(f"{path}: no key {raw!r}")
        node = node[raw]
    raise SweepError(f"{path}: empty path")


def set_path(spec: dict, path: str, value) -> None:
    container, key = _walk(spec, path.split("."), path)
    if isinstance(container, dict) and key not in container:
        raise SweepError(f"{path}: {key!r} is not set in the base strategy; "
                         f"add it there first so the sweep varies something real")
    container[key] = value


def get_path(spec: dict, path: str):
    container, key = _walk(spec, path.split("."), path)
    try:
        return container[key]
    except (KeyError, IndexError):
        opts = sorted(container) if isinstance(container, dict) else []
        raise SweepError(f"{path}: {key!r} is not set in the base strategy"
                         + (f" (available here: {opts})" if opts else "")) from None


def _coerce(raw: str):
    for cast in (int, float):
        try:
            return cast(raw)
        except ValueError:
            pass
    low = raw.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    return raw.strip()


def parse_param(text: str) -> tuple[str, list]:
    """`legs[0].delta=0.16,0.20,0.25` or `exits.dte=7:28:7` (start:stop:step)."""
    if "=" not in text:
        raise SweepError(f"--param needs path=values, got {text!r}")
    path, rhs = text.split("=", 1)
    path = path.strip()
    rhs = rhs.strip()
    if ":" in rhs and "," not in rhs:
        bits = [float(x) for x in rhs.split(":")]
        if len(bits) != 3 or bits[2] == 0:
            raise SweepError(f"{text!r}: range must be start:stop:step")
        start, stop, step = bits
        vals, v = [], start
        n = 0
        while (v <= stop + 1e-9 if step > 0 else v >= stop - 1e-9) and n < 500:
            vals.append(round(v, 10))
            v += step
            n += 1
        if all(float(x).is_integer() for x in vals):
            vals = [int(x) for x in vals]
    else:
        vals = [_coerce(x) for x in rhs.split(",") if x.strip() != ""]
    if not vals:
        raise SweepError(f"{text!r}: no values")
    return path, vals


# ---------------------------------------------------------------------- grid
#: Path tails that do not identify an axis on their own -- two legs both swept on
#: `delta` would otherwise share a label and be indistinguishable in the report.
_AMBIGUOUS_TAILS = {"delta", "dte", "strike", "width", "premium", "iv", "ratio",
                    "qty", "contracts", "factor", "fraction", "offset"}


@dataclass
class Axis:
    path: str
    values: list
    label: str = ""

    def __post_init__(self):
        if not self.label:
            self.label = self.auto_label(self.path)

    @staticmethod
    def auto_label(path: str) -> str:
        parts = path.replace("[", ".").replace("]", "").split(".")
        parts = [p for p in parts if p]
        tail = parts[-1]
        if len(parts) >= 2 and tail in _AMBIGUOUS_TAILS:
            return f"{parts[-2]} {tail}".replace("_", " ")
        return tail.replace("_", " ")


@dataclass
class Cell:
    """One point in the grid."""
    index: int
    overrides: dict            # path -> value
    summary: dict = field(default_factory=dict)
    error: str = ""
    daily: list = field(default_factory=list)     # (date, net_liq), thinned
    name: str = ""

    def label(self) -> str:
        return ", ".join(f"{k.split('.')[-1]}={v}" for k, v in self.overrides.items())


@dataclass
class SweepResult:
    base_name: str
    axes: list
    cells: list
    metric: str = "cagr_pct"

    @property
    def ok(self):
        return [c for c in self.cells if not c.error]

    def best(self, metric: str | None = None) -> Cell | None:
        m = metric or self.metric
        good = [c for c in self.ok if isinstance(c.summary.get(m), (int, float))]
        if not good:
            return None
        return max(good, key=lambda c: c.summary[m])

    def grid(self, metric: str | None = None) -> dict:
        """(value_axis0, value_axis1 or None) -> metric value."""
        m = metric or self.metric
        out = {}
        for c in self.ok:
            k = tuple(c.overrides.get(a.path) for a in self.axes)
            out[k] = c.summary.get(m)
        return out


def expand(axes: list[Axis], limit: int = 400) -> list[dict]:
    combos = list(itertools.product(*[a.values for a in axes]))
    if len(combos) > limit:
        raise SweepError(f"{len(combos)} combinations exceeds the limit of {limit}; "
                         f"narrow the axes or raise --limit")
    return [{a.path: v for a, v in zip(axes, combo)} for combo in combos]


def validate(base_spec: dict, axes: list[Axis]) -> None:
    """Fail fast on a bad path, before any backtest runs."""
    probe = copy.deepcopy(base_spec)
    for a in axes:
        current = get_path(probe, a.path)          # raises if the path is wrong
        for v in a.values:
            if current is not None and not isinstance(v, type(current)):
                if not (isinstance(current, (int, float)) and isinstance(v, (int, float))):
                    raise SweepError(f"{a.path}: base value is {type(current).__name__}, "
                                     f"sweep value {v!r} is {type(v).__name__}")
        set_path(probe, a.path, a.values[0])
    Strategy.from_dict(probe)                      # must still compile


# ------------------------------------------------------------------ running
def apply(base_spec: dict, overrides: dict) -> Strategy:
    spec = copy.deepcopy(base_spec)
    for path, value in overrides.items():
        set_path(spec, path, value)
    return Strategy.from_dict(spec)


def _thin(daily, keep=900):
    """Down-sample an equity curve for the report without moving its extremes."""
    if len(daily) <= keep:
        return [(d.date, d.net_liq) for d in daily]
    step = len(daily) / keep
    idx = sorted({int(i * step) for i in range(keep)} | {len(daily) - 1})
    return [(daily[i].date, daily[i].net_liq) for i in idx]


def run_cell(base_spec: dict, overrides: dict, index: int, md, vix,
             start=None, end=None) -> Cell:
    cell = Cell(index=index, overrides=overrides)
    try:
        strat = apply(base_spec, overrides)
        if start:
            strat.start = start
        if end:
            strat.end = end
        cell.name = strat.name
        res = Backtester(md, vix).run(strat)
        cell.summary = summarize(res)
        cell.daily = _thin(res.daily)
    except (SpecError, SweepError, ValueError, KeyError) as exc:
        cell.error = f"{type(exc).__name__}: {exc}"
    return cell


# --- worker-process entry point (must be importable at module level) --------
_W = {}


def _worker_init(options_db, bars_db, vix_csv, cache_db):
    from .data import MarketData
    from .vix import VixSeries
    md = MarketData(options_db, bars_db, cache_db=cache_db)
    _W["md"] = md
    _W["vix"] = VixSeries(md, csv_path=vix_csv, cache_db=cache_db)


def _worker_run(payload):
    base_spec, overrides, index, start, end = payload
    return run_cell(base_spec, overrides, index, _W["md"], _W["vix"], start, end)


def run_sweep(base_spec: dict, axes: list[Axis], md, vix, *, metric="cagr_pct",
              jobs: int = 1, paths: dict | None = None, start=None, end=None,
              limit: int = 400, progress=None) -> SweepResult:
    validate(base_spec, axes)
    combos = expand(axes, limit)
    cells: list[Cell] = []

    if jobs > 1 and paths:
        from concurrent.futures import ProcessPoolExecutor
        payloads = [(base_spec, ov, i, start, end) for i, ov in enumerate(combos)]
        with ProcessPoolExecutor(
                max_workers=jobs, initializer=_worker_init,
                initargs=(paths["options_db"], paths["bars_db"],
                          paths["vix_csv"], paths["cache"])) as pool:
            for done, cell in enumerate(pool.map(_worker_run, payloads), 1):
                cells.append(cell)
                if progress:
                    progress(done, len(combos), cell)
    else:
        for i, ov in enumerate(combos):
            cell = run_cell(base_spec, ov, i, md, vix, start, end)
            cells.append(cell)
            if progress:
                progress(i + 1, len(combos), cell)

    cells.sort(key=lambda c: c.index)
    name = next((c.name for c in cells if c.name), base_spec.get("name", "sweep"))
    return SweepResult(base_name=name, axes=axes, cells=cells, metric=metric)


# ------------------------------------------------------------------ loading
def save_result(result: SweepResult, path: str) -> str:
    """Persist a finished grid so the report can be re-rendered without re-running."""
    payload = {
        "base_name": result.base_name, "metric": result.metric,
        "axes": [{"path": a.path, "values": a.values, "label": a.label} for a in result.axes],
        "cells": [{"index": c.index, "overrides": c.overrides, "summary":
                   {k: (v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else v)
                    for k, v in c.summary.items() if not isinstance(v, dict)},
                   "error": c.error, "name": c.name,
                   "daily": [[d.isoformat(), v] for d, v in c.daily]}
                  for c in result.cells],
    }
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    return path


def load_result(path: str) -> SweepResult:
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    axes = [Axis(a["path"], a["values"], a["label"]) for a in raw["axes"]]
    cells = []
    for c in raw["cells"]:
        cells.append(Cell(index=c["index"], overrides=c["overrides"], summary=c["summary"],
                          error=c.get("error", ""), name=c.get("name", ""),
                          daily=[(dt.date.fromisoformat(d), v) for d, v in c.get("daily", [])]))
    return SweepResult(raw["base_name"], axes, cells, raw.get("metric", "cagr_pct"))


def load_sweep(path: str) -> tuple[dict, list[Axis], str]:
    """Read a sweep file: {base, axes, metric}.  Returns (base_spec, axes, metric)."""
    import yaml
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) if path.lower().endswith((".yaml", ".yml")) \
            else json.load(fh)
    base = raw.get("base")
    if not base:
        raise SweepError(f"{path}: needs a 'base' pointing at a strategy file")
    base_path = base if os.path.isabs(base) else os.path.join(os.path.dirname(path), base)
    if not os.path.exists(base_path):
        base_path = base
    with open(base_path, encoding="utf-8") as fh:
        import yaml as _y
        base_spec = _y.safe_load(fh) if base_path.lower().endswith((".yaml", ".yml")) \
            else json.load(fh)
    if raw.get("name"):
        base_spec["name"] = raw["name"]

    axes = []
    for key, val in (raw.get("axes") or {}).items():
        if isinstance(val, dict):
            axes.append(Axis(path=val["path"], values=list(val["values"]),
                             label=val.get("label", key)))
        else:
            axes.append(Axis(path=key, values=list(val)))
    if not axes:
        raise SweepError(f"{path}: needs at least one axis")
    return base_spec, axes, raw.get("metric", "cagr_pct")
