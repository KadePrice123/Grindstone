"""Performance statistics for a backtest result."""
from __future__ import annotations

import math
import statistics

import numpy as np


def summarize(res) -> dict:
    trades = res.trades
    daily = res.daily
    cap = res.strategy.capital
    out: dict = {"trades": len(trades), "capital": cap}
    if not daily:
        return out

    nl = np.array([d.net_liq for d in daily], dtype=float)
    years = max((daily[-1].date - daily[0].date).days / 365.25, 1e-9)
    out["start"] = daily[0].date
    out["end"] = daily[-1].date
    out["years"] = years
    out["final_net_liq"] = float(nl[-1])
    out["total_return_pct"] = 100.0 * (nl[-1] / cap - 1.0)
    out["cagr_pct"] = 100.0 * ((nl[-1] / cap) ** (1.0 / years) - 1.0) if nl[-1] > 0 else float("nan")

    peak = np.maximum.accumulate(nl)
    dd = nl / peak - 1.0
    out["max_drawdown_pct"] = 100.0 * float(dd.min())

    r = np.diff(nl) / np.where(peak[:-1] == 0, 1.0, peak[:-1])
    r = r[np.isfinite(r)]
    if len(r) > 2 and r.std() > 0:
        out["sharpe"] = float(r.mean() / r.std() * math.sqrt(252.0))
        downside = r[r < 0]
        out["sortino"] = float(r.mean() / downside.std() * math.sqrt(252.0)) if len(downside) > 2 \
            and downside.std() > 0 else float("nan")
        out["vol_annual_pct"] = float(r.std() * math.sqrt(252.0) * 100.0)
    out["calmar"] = (out["cagr_pct"] / abs(out["max_drawdown_pct"])
                     if out.get("max_drawdown_pct") else float("nan"))

    if trades:
        pnl = [t.pnl for t in trades]
        wins = [p for p in pnl if p > 0]
        losses = [p for p in pnl if p <= 0]
        out["win_rate_pct"] = 100.0 * len(wins) / len(pnl)
        out["avg_pnl"] = statistics.fmean(pnl)
        out["total_pnl"] = sum(pnl)
        out["avg_win"] = statistics.fmean(wins) if wins else 0.0
        out["avg_loss"] = statistics.fmean(losses) if losses else 0.0
        out["largest_win"] = max(pnl)
        out["largest_loss"] = min(pnl)
        out["profit_factor"] = (sum(wins) / abs(sum(losses))) if losses and sum(losses) else float("inf")
        out["expectancy"] = out["avg_pnl"]
        out["avg_dit"] = statistics.fmean(t.dit for t in trades)
        out["total_fees"] = sum(t.fees for t in trades)
        out["avg_premium"] = statistics.fmean(t.premium * 100 for t in trades)
        bp = [t.buying_power for t in trades if t.buying_power > 0]
        out["avg_bp"] = statistics.fmean(bp) if bp else 0.0
        out["avg_roi_pct"] = statistics.fmean(t.roi for t in trades)
        reasons: dict[str, int] = {}
        for t in trades:
            reasons[t.reason] = reasons.get(t.reason, 0) + 1
        out["close_reasons"] = dict(sorted(reasons.items(), key=lambda kv: -kv[1]))
    return out


def format_summary(s: dict) -> str:
    def g(k, fmt="{:.2f}", default="-"):
        v = s.get(k)
        if v is None or (isinstance(v, float) and not math.isfinite(v)):
            return default
        return fmt.format(v) if not isinstance(v, (str, dict)) else str(v)

    lines = [
        f"  period            {s.get('start')} .. {s.get('end')}  ({g('years','{:.1f}')} yrs)",
        f"  capital           {g('capital','${:,.0f}')}  ->  {g('final_net_liq','${:,.2f}')}",
        f"  total return      {g('total_return_pct','{:+.2f}%')}",
        f"  CAGR              {g('cagr_pct','{:+.2f}%')}",
        f"  max drawdown      {g('max_drawdown_pct','{:.2f}%')}",
        f"  Sharpe / Sortino  {g('sharpe')} / {g('sortino')}",
        f"  trades            {s.get('trades', 0)}   win rate {g('win_rate_pct','{:.1f}%')}",
        f"  avg P/L           {g('avg_pnl','${:,.2f}')}   "
        f"avg win {g('avg_win','${:,.2f}')}   avg loss {g('avg_loss','${:,.2f}')}",
        f"  profit factor     {g('profit_factor','{:.2f}')}   avg days in trade {g('avg_dit','{:.1f}')}",
        f"  total fees        {g('total_fees','${:,.2f}')}",
    ]
    if s.get("close_reasons"):
        lines.append("  close reasons     " + ", ".join(f"{k}={v}" for k, v in s["close_reasons"].items()))
    return "\n".join(lines)
