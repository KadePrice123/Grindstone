"""Compare an engine run against a tastytrade reference export.

The comparison is layered, because the two runs cannot be bit-identical: the
databases hold one end-of-day snapshot while tastytrade decides at 15:45, so
individual strikes occasionally differ by one increment.  What must agree is the
*mechanics* — selection rule, fill convention, fee schedule, exit logic — and
that shows up as agreement in the layers below.

    L1  structure   expiration and strike chosen on the same day
    L2  fills       transaction prices against the reference's own prices
    L3  trades      round-trip dates, premium, P/L, close reason
    L4  equity      the daily settlement curve
"""
from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass, field

import numpy as np

from .engine import BacktestResult
from .tastyio import TastyRun


def _pct(n, d) -> float:
    return 100.0 * n / d if d else float("nan")


def _q(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, max(0, int(p * len(v))))] if v else float("nan")


@dataclass
class Layer:
    name: str
    metrics: dict = field(default_factory=dict)
    detail: list[str] = field(default_factory=list)


@dataclass
class CalibrationReport:
    run: str
    layers: list[Layer] = field(default_factory=list)

    def get(self, name: str) -> Layer | None:
        return next((l for l in self.layers if l.name == name), None)

    def text(self, verbose: bool = False) -> str:
        out = [f"=== calibration: {self.run} " + "=" * max(0, 50 - len(self.run))]
        for l in self.layers:
            out.append(f"  [{l.name}]")
            for k, v in l.metrics.items():
                if isinstance(v, float):
                    out.append(f"      {k:.<34} {v:>12,.3f}")
                else:
                    out.append(f"      {k:.<34} {v:>12}")
            if verbose:
                out.extend("      " + d for d in l.detail)
        return "\n".join(out)


# ---------------------------------------------------------------------------
def compare(res: BacktestResult, ref: TastyRun, verbose_limit: int = 25) -> CalibrationReport:
    rep = CalibrationReport(run=ref.name)
    rep.layers.append(_structure(res, ref, verbose_limit))
    rep.layers.append(_fills(res, ref, verbose_limit))
    rep.layers.append(_trades(res, ref, verbose_limit))
    rep.layers.append(_equity(res, ref))
    return rep


# ------------------------------------------------------------------- layer 1
def _structure(res: BacktestResult, ref: TastyRun, limit: int) -> Layer:
    L = Layer("L1 structure")
    ref_open = {o.date: o for o in ref.opening_orders()}
    exp_hit = k_hit = k_tot = both = 0
    k_off = []
    for t in res.trades:
        o = ref_open.get(t.opened)
        if o is None:
            continue
        both += 1
        ours = _legs_of(t)
        theirs = sorted((l.right, l.strike, l.exp) for l in o.legs)
        if ours and theirs and ours[0][2] == theirs[0][2]:
            exp_hit += 1
        for a, b in zip(ours, theirs):
            k_tot += 1
            if abs(a[1] - b[1]) < 1e-6:
                k_hit += 1
            k_off.append(a[1] - b[1])

    L.metrics["reference openings"] = len(ref_open)
    L.metrics["engine openings"] = len(res.trades)
    L.metrics["same-day openings"] = both
    L.metrics["same-day open rate %"] = _pct(both, len(ref_open))
    L.metrics["expiration match %"] = _pct(exp_hit, both)
    L.metrics["strike match %"] = _pct(k_hit, k_tot)
    if k_off:
        L.metrics["strike offset median $"] = statistics.median(k_off)
        L.metrics["strike within $1 %"] = _pct(sum(1 for x in k_off if abs(x) <= 1.0), len(k_off))
        L.metrics["strike within $2 %"] = _pct(sum(1 for x in k_off if abs(x) <= 2.0), len(k_off))
    return L


def _legs_of(trade) -> list[tuple[str, float, dt.date]]:
    out = []
    for f in trade.fills:
        if f.action.endswith("to open") and f.right:
            out.append((f.right, f.strike, f.exp))
    return sorted(out)


# ------------------------------------------------------------------- layer 2
def _fills(res: BacktestResult, ref: TastyRun, limit: int) -> Layer:
    L = Layer("L2 fills")
    idx: dict[tuple, list[float]] = {}
    for tx in ref.transactions:
        if tx.right is None or tx.type in ("expiration", "assignment", "exercise"):
            continue
        idx.setdefault((tx.date, tx.right, tx.strike, tx.exp), []).append(tx.price)

    err = []
    matched = 0
    for t in res.trades:
        for f in t.fills:
            if f.right is None or "to " not in f.action:
                continue
            got = idx.get((f.date, f.right, f.strike, f.exp))
            if not got:
                continue
            matched += 1
            err.append(f.price - min(got, key=lambda p: abs(p - f.price)))
    L.metrics["fills matched to reference"] = matched
    if err:
        L.metrics["price error median $"] = statistics.median(err)
        L.metrics["price error mean $"] = statistics.fmean(err)
        L.metrics["price error sd $"] = statistics.pstdev(err)
        L.metrics["within 1c %"] = _pct(sum(1 for x in err if abs(x) <= 0.0101), len(err))
        L.metrics["within 5c %"] = _pct(sum(1 for x in err if abs(x) <= 0.0501), len(err))
    return L


# ------------------------------------------------------------------- layer 3
def _trades(res: BacktestResult, ref: TastyRun, limit: int) -> Layer:
    L = Layer("L3 trades")
    L.metrics["reference trades"] = len(ref.trades)
    L.metrics["engine trades"] = len(res.trades)

    ref_reasons: dict[str, int] = {}
    for t in ref.trades:
        ref_reasons[t.reason] = ref_reasons.get(t.reason, 0) + 1
    our_reasons: dict[str, int] = {}
    for t in res.trades:
        our_reasons[t.reason] = our_reasons.get(t.reason, 0) + 1
    for k in sorted(set(ref_reasons) | set(our_reasons)):
        L.metrics[f"reason '{k}' (ref/eng)"] = f"{ref_reasons.get(k,0)} / {our_reasons.get(k,0)}"

    L.metrics["reference total P/L $"] = sum(t.pnl for t in ref.trades)
    L.metrics["engine total P/L $"] = sum(t.pnl for t in res.trades)
    L.metrics["reference win rate %"] = _pct(sum(1 for t in ref.trades if t.pnl > 0), len(ref.trades))
    L.metrics["engine win rate %"] = _pct(sum(1 for t in res.trades if t.pnl > 0), len(res.trades))
    L.metrics["reference avg P/L $"] = statistics.fmean([t.pnl for t in ref.trades]) if ref.trades else 0.0
    L.metrics["engine avg P/L $"] = statistics.fmean([t.pnl for t in res.trades]) if res.trades else 0.0
    L.metrics["reference fees $"] = sum(t.fees for t in ref.trades)
    L.metrics["engine fees $"] = sum(t.fees for t in res.trades)

    # Pair trades that opened on the same day; that is the fair like-for-like set.
    by_open: dict[dt.date, list] = {}
    for t in ref.trades:
        by_open.setdefault(t.opened, []).append(t)
    paired = []
    for t in res.trades:
        cand = by_open.get(t.opened)
        if cand:
            paired.append((t, cand[0]))
    L.metrics["paired trades"] = len(paired)
    if paired:
        dpnl = [a.pnl - b.pnl for a, b in paired]
        dprem = [abs(a.open_price) * 100 - abs(b.premium) for a, b in paired]
        dclose = [(a.closed - b.closed).days for a, b in paired if b.closed]
        same_reason = sum(1 for a, b in paired if a.reason == b.reason)
        L.metrics["premium error median $"] = statistics.median(dprem)
        L.metrics["premium error mean $"] = statistics.fmean(dprem)
        L.metrics["P/L error median $"] = statistics.median(dpnl)
        L.metrics["P/L error mean $"] = statistics.fmean(dpnl)
        L.metrics["P/L error sd $"] = statistics.pstdev(dpnl)
        L.metrics["same close date %"] = _pct(sum(1 for x in dclose if x == 0), len(dclose))
        L.metrics["close within 1 day %"] = _pct(sum(1 for x in dclose if abs(x) <= 1), len(dclose))
        L.metrics["same close reason %"] = _pct(same_reason, len(paired))
        L.detail = [f"{a.opened} pnl {a.pnl:+8.2f} vs {b.pnl:+8.2f}  "
                    f"({a.reason} / {b.reason})" for a, b in paired[:limit]]
    return L


# ------------------------------------------------------------------- layer 4
def _equity(res: BacktestResult, ref: TastyRun) -> Layer:
    L = Layer("L4 equity")
    ours = {d.date: d.net_liq for d in res.daily}
    theirs = {s.date: s.net_liq for s in ref.settlement}
    common = sorted(set(ours) & set(theirs))
    L.metrics["common days"] = len(common)
    if common:
        # The option database stops before the reference export does, so every
        # comparison is made on the overlapping window, not on each run's own end.
        L.metrics["compared through"] = str(common[-1])
        if common[-1] != ref.settlement[-1].date:
            L.metrics["reference runs to"] = str(ref.settlement[-1].date)
        L.metrics["reference net liq $"] = theirs[common[-1]]
        L.metrics["engine net liq $"] = ours[common[-1]]
    if len(common) > 10:
        a = np.array([ours[d] for d in common])
        b = np.array([theirs[d] for d in common])
        L.metrics["final difference $"] = float(a[-1] - b[-1])
        L.metrics["final difference %"] = float(100.0 * (a[-1] / b[-1] - 1.0)) if b[-1] else float("nan")
        L.metrics["curve correlation"] = float(np.corrcoef(a, b)[0, 1])
        da, db = np.diff(a), np.diff(b)
        m = np.isfinite(da) & np.isfinite(db)
        if m.sum() > 10 and da[m].std() > 0 and db[m].std() > 0:
            L.metrics["daily change correlation"] = float(np.corrcoef(da[m], db[m])[0, 1])
        L.metrics["mean abs difference $"] = float(np.mean(np.abs(a - b)))
        cap = res.strategy.capital
        L.metrics["reference return %"] = float(100.0 * (b[-1] / cap - 1.0))
        L.metrics["engine return %"] = float(100.0 * (a[-1] / cap - 1.0))
        pk = np.maximum.accumulate(b)
        L.metrics["reference max DD %"] = float(100.0 * (b / pk - 1.0).min())
        pk = np.maximum.accumulate(a)
        L.metrics["engine max DD %"] = float(100.0 * (a / pk - 1.0).min())
    return L
