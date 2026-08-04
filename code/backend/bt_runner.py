"""Backtest runner — the subprocess that owns numpy.

Spawned by backend/backtests.py as `python -m backend.bt_runner <job.json>`.
Runs in its own process for three reasons the sidecar cannot work around:
the engine has no cancellation hook (kill is the only stop), its imports are
exactly the kind that froze the sidecar once before (GIL + import lock), and
a 13-year run holds hundreds of MB of chain data the API process should
never carry.

Protocol: JSON lines on stdout —
    {"event": "progress", "phase": "...", "i": N, "n": N, "pct": F}
then a result file (result.json in the job's out_dir) holding either the run
payload or an {"error": ...}. Exit 0 means the result file is authoritative;
any other exit means a crash the parent reports from stderr.
"""
from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

# Heavy on purpose — this is the process that pays the numpy import, not the
# sidecar (see backend/bt/__init__.py).
from backend.bt.calibrate import compare
from backend.bt.data import MarketData
from backend.bt.engine import Backtester
from backend.bt.report import build_report
from backend.bt.spec import SpecError, Strategy
from backend.bt.stats import summarize
from backend.bt.tastyio import discover, load_run
from backend.bt.expr import RuleError
from backend.bt.vix import VixSeries


def _emit(**evt) -> None:
    print(json.dumps(evt), flush=True)


def _progress(phase: str):
    def cb(i: int, n: int) -> None:
        _emit(event="progress", phase=phase, i=i, n=n,
              pct=round(100.0 * i / max(n, 1), 1))
    return cb


def _slug(s: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in s).strip("_").lower()


def _jsonable(obj):
    """Dates and dataclasses -> JSON, recursively."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return obj


def _market(paths: dict) -> tuple[MarketData, VixSeries]:
    md = MarketData(paths["options_db"], paths["bars_db"], cache_db=paths["cache"])
    vix = VixSeries(md, csv_path=paths["vix_csv"], cache_db=paths["cache"])
    return md, vix


def _apply_dates(strat: Strategy, start: str | None, end: str | None) -> None:
    import datetime as dt
    if start:
        strat.start = dt.date.fromisoformat(start)
    if end:
        strat.end = dt.date.fromisoformat(end)


def run_single(job: dict) -> dict:
    strat = Strategy.from_dict(job["spec"])
    if job.get("name"):
        strat.name = job["name"]
    _apply_dates(strat, job.get("start"), job.get("end"))
    md, vix = _market(job["paths"])
    res = Backtester(md, vix, progress=_progress("loading")).run(strat)
    out_dir = Path(job["out_dir"])
    report = "report.html"
    build_report(res, str(out_dir / report), title=strat.name)
    return {
        "summary": _jsonable(summarize(res)),
        "trades": _jsonable(res.trades),
        "daily": _jsonable(res.daily),
        "skipped": dict(res.skipped),
        "report_files": [report],
    }


def run_calibration(job: dict) -> dict:
    """Verify the engine against every bundled tastytrade reference: the
    known trades it should have made from the exact data. The strategy spec
    comes from the BUNDLED preset files, never a user's edited copy."""
    paths = job["paths"]
    cal_map: dict[str, str] = job["calibration_map"]
    presets_dir = Path(job["presets_dir"])
    folders = discover(paths["calibration"])
    if not folders:
        return {"error": f"no reference runs found under {paths['calibration']}"}
    md, vix = _market(paths)
    out_dir = Path(job["out_dir"])
    references, report_files = [], []
    for folder in folders:
        base = Path(folder).name
        preset = cal_map.get(base)
        if preset is None:
            continue
        raw = json.loads((presets_dir / f"{preset}.json").read_text(encoding="utf-8"))
        strat = Strategy.from_dict(raw)
        ref = load_run(folder)
        # Compare like with like: the reference's own capital and window.
        strat.capital = ref.capital
        strat.start, strat.end = ref.start, ref.end
        res = Backtester(md, vix, progress=_progress(base)).run(strat)
        rep = compare(res, ref)
        page = f"{_slug(base)}.html"
        build_report(res, str(out_dir / page), reference=ref, calib=rep,
                     title=f"{base} (vs tastytrade)")
        report_files.append(page)
        references.append({
            "reference": base,
            "preset": preset,
            "summary": _jsonable(summarize(res)),
            "layers": {layer.name: _jsonable(layer.metrics) for layer in rep.layers},
            "report": page,
        })
    if not references:
        return {"error": "no reference folder matched the calibration map"}
    return {
        "summary": {"references": len(references)},
        "calib": references,
        "report_files": report_files,
    }


def main() -> int:
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out_dir = Path(job["out_dir"])
    try:
        if job["kind"] == "calibration":
            result = run_calibration(job)
        else:
            result = run_single(job)
    except (SpecError, RuleError, FileNotFoundError, ValueError) as exc:
        result = {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — report, then die loudly
        result = {"error": f"{type(exc).__name__}: {exc}"}
        (out_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
        raise
    (out_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    _emit(event="done", error=result.get("error"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
