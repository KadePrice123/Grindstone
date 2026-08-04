"""Self-contained HTML reports for a backtest run.

Charts are emitted as inline SVG from Python -- no CDN, no build step, no
network -- with a thin vanilla-JS layer adding crosshair and hover tooltips on
top.  Everything a chart shows is also reachable from the trade table and the
labelled heatmap cells, so nothing is gated behind a hover.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import math
import os
from collections import Counter, OrderedDict

from .stats import summarize

# --------------------------------------------------------------------------
# Palette: validated categorical slots 1-2 plus the blue<->red diverging pair.
# Both modes are selected steps, not an automatic flip.
# --------------------------------------------------------------------------
CSS = """
:root{
  color-scheme:light;
  --surface-1:#fcfcfb; --plane:#f9f9f7;
  --text-primary:#0b0b0b; --text-secondary:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,.10);
  --series-1:#2a78d6; --series-2:#eb6834; --series-3:#1baf7a;
  /* Diverging arms for signed returns: red is a loss, blue a gain.  These are
     ramp steps, deliberately not the reserved status colours. */
  --div-neg-1:#f3b6b6; --div-neg-2:#d03b3b; --div-neg-3:#8e2222;
  --div-mid:#f0efec;
  --div-pos-1:#9ec5f4; --div-pos-2:#256abf; --div-pos-3:#0d366b;
  --good:#006300; --bad:#d03b3b;
}
@media (prefers-color-scheme:dark){
  :root:where(:not([data-theme="light"])){
    color-scheme:dark;
    --surface-1:#1a1a19; --plane:#0d0d0d;
    --text-primary:#fff; --text-secondary:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
    --series-1:#3987e5; --series-2:#d95926; --series-3:#199e70;
    --div-neg-1:#7a2020; --div-neg-2:#d03b3b; --div-neg-3:#f3b6b6;
    --div-mid:#383835;
    --div-pos-1:#184f95; --div-pos-2:#3987e5; --div-pos-3:#9ec5f4;
    --good:#0ca30c; --bad:#e66767;
  }
}
:root[data-theme="dark"]{
  color-scheme:dark;
  --surface-1:#1a1a19; --plane:#0d0d0d;
  --text-primary:#fff; --text-secondary:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
  --series-1:#3987e5; --series-2:#d95926; --series-3:#199e70;
  --div-neg-1:#7a2020; --div-neg-2:#d03b3b; --div-neg-3:#f3b6b6;
  --div-mid:#383835;
  --div-pos-1:#184f95; --div-pos-2:#3987e5; --div-pos-3:#9ec5f4;
  --good:#0ca30c; --bad:#e66767;
}
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--text-primary);
  font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;}
.wrap{max-width:1180px;margin:0 auto;padding:32px 20px 72px}
header{margin-bottom:28px}
h1{font-size:24px;font-weight:600;margin:0 0 4px}
.sub{color:var(--text-secondary);font-size:13px}
h2{font-size:15px;font-weight:600;margin:0 0 2px}
.hint{color:var(--muted);font-size:12px;margin:0 0 14px}
.card{background:var(--surface-1);border:1px solid var(--border);border-radius:10px;
  padding:20px;margin-bottom:18px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin-bottom:18px}
.tile{background:var(--surface-1);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.tile .lab{color:var(--text-secondary);font-size:12px;margin-bottom:6px}
.tile .val{font-size:24px;font-weight:600;letter-spacing:-.01em}
.tile .note{color:var(--muted);font-size:11px;margin-top:3px}
/* The hero figure is the widest string on the page, so give it two columns
   rather than let it overflow a single tile. */
.tile.hero{grid-column:span 2}
.hero .val{font-size:clamp(26px,3.1vw,38px)}
@media(max-width:640px){.tile.hero{grid-column:span 1}}
.pos{color:var(--good)} .neg{color:var(--bad)}
.chart{position:relative;width:100%;overflow-x:auto}
svg{display:block;max-width:100%;height:auto}
.legend{display:flex;gap:18px;flex-wrap:wrap;margin:0 0 10px;font-size:12px;color:var(--text-secondary)}
.legend span{display:inline-flex;align-items:center;gap:7px}
.key{width:14px;height:3px;border-radius:2px;display:inline-block}
.tip{position:absolute;pointer-events:none;opacity:0;transition:opacity .08s;
  background:var(--surface-1);border:1px solid var(--border);border-radius:7px;
  padding:8px 10px;font-size:12px;line-height:1.5;white-space:nowrap;z-index:9;
  box-shadow:0 4px 14px rgba(0,0,0,.13)}
.tip b{font-weight:600}
table{width:100%;border-collapse:collapse;font-size:12.5px;
  font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:7px 9px;border-bottom:1px solid var(--grid);white-space:nowrap}
th:first-child,td:first-child,th.l,td.l{text-align:left}
thead th{color:var(--text-secondary);font-weight:600;cursor:pointer;user-select:none;
  position:sticky;top:0;background:var(--surface-1);border-bottom:1px solid var(--axis)}
thead th:hover{color:var(--text-primary)}
thead th::after{content:"";opacity:.4;margin-left:5px}
thead th.asc::after{content:"\\2191";opacity:1}
thead th.desc::after{content:"\\2193";opacity:1}
tbody tr:hover{background:var(--plane)}
.scroll{max-height:560px;overflow:auto;border:1px solid var(--border);border-radius:8px}
.bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
select,input,button,a.btn{font:inherit;font-size:13px;color:var(--text-primary);
  background:var(--surface-1);border:1px solid var(--border);border-radius:7px;padding:6px 10px}
button,a.btn{cursor:pointer;text-decoration:none}
button:hover,a.btn:hover{border-color:var(--axis)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:18px}
@media(max-width:820px){.grid2{grid-template-columns:1fr}}
.kv{display:grid;grid-template-columns:1fr auto;gap:2px 18px;font-size:12.5px;
  font-variant-numeric:tabular-nums}
.kv div:nth-child(odd){color:var(--text-secondary)}
.kv div:nth-child(even){text-align:right}
code{background:var(--plane);border:1px solid var(--border);border-radius:5px;
  padding:1px 5px;font-size:12px}
pre{background:var(--plane);border:1px solid var(--border);border-radius:8px;
  padding:12px 14px;overflow-x:auto;font-size:12px;margin:0}
.foot{color:var(--muted);font-size:12px;margin-top:26px;text-align:center}
"""

_ESC = html.escape


def _fmt(v, kind="num", dp=2):
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "-"
    if kind == "money":
        return f"${v:,.{dp}f}"
    if kind == "money0":
        return f"${v:,.0f}"
    if kind == "pct":
        return f"{v:,.{dp}f}%"
    if kind == "spct":
        return f"{v:+,.{dp}f}%"
    if kind == "int":
        return f"{v:,.0f}"
    return f"{v:,.{dp}f}"


def _cls(v):
    return "pos" if (isinstance(v, (int, float)) and v > 0) else ("neg" if isinstance(v, (int, float)) and v < 0 else "")


def _nice_ticks(lo, hi, n=5):
    """Round axis ticks to human numbers."""
    if not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo:
        return [lo]
    raw = (hi - lo) / max(n, 1)
    mag = 10 ** math.floor(math.log10(raw))
    step = min((m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw), default=mag * 10)
    start = math.ceil(lo / step) * step
    out, v = [], start
    while v <= hi + step * 1e-9:
        out.append(round(v, 10))
        v += step
    return out or [lo, hi]


# ==========================================================================
# Charts
# ==========================================================================
def _time_axis(dates, x_of, y0, y1, w):
    """January gridlines + year labels; falls back to evenly spaced ticks."""
    parts = []
    seen = set()
    step = max(1, (dates[-1].year - dates[0].year) // 12 + 1)
    for i, d in enumerate(dates):
        if d.year in seen or d.year % step:
            continue
        if i and dates[i - 1].year == d.year:
            continue
        seen.add(d.year)
        x = x_of(i)
        if x < 34 or x > w - 14:
            continue
        parts.append(f'<line class="g" x1="{x:.1f}" y1="{y0}" x2="{x:.1f}" y2="{y1}"/>')
        parts.append(f'<text class="ax" x="{x:.1f}" y="{y1+16}" text-anchor="middle">{d.year}</text>')
    return "".join(parts)


def line_chart(dates, series, *, height=270, y_fmt="money0", zero=False,
               chart_id="c", width=1120, pad_l=74):
    """One or two aligned series over time.  `series` is [(label, values, css_var)]."""
    if not dates:
        return "<p class='hint'>no data</p>"
    pad_r, pad_t, pad_b = 20, 14, 30
    x0, x1 = pad_l, width - pad_r
    y0, y1 = pad_t, height - pad_b

    vals = [v for _, ys, _ in series for v in ys if math.isfinite(v)]
    if not vals:
        return "<p class='hint'>no data</p>"
    lo, hi = min(vals), max(vals)
    if zero:
        lo = min(lo, 0.0)
    if hi == lo:
        hi = lo + 1.0
    ticks = _nice_ticks(lo, hi)
    lo, hi = min(lo, ticks[0]), max(hi, ticks[-1])

    n = len(dates)
    def X(i): return x0 + (x1 - x0) * (i / max(n - 1, 1))
    def Y(v): return y1 - (y1 - y0) * ((v - lo) / (hi - lo))

    p = [f'<svg viewBox="0 0 {width} {height}" role="img" preserveAspectRatio="xMidYMid meet">']
    for t in ticks:
        y = Y(t)
        p.append(f'<line class="g" x1="{x0}" y1="{y:.1f}" x2="{x1}" y2="{y:.1f}"/>')
        p.append(f'<text class="ax" x="{x0-10}" y="{y+4:.1f}" text-anchor="end">'
                 f'{_ESC(_fmt(t, y_fmt))}</text>')
    p.append(_time_axis(dates, X, y0, y1, width))
    p.append(f'<line class="base" x1="{x0}" y1="{y1}" x2="{x1}" y2="{y1}"/>')

    for label, ys, var in series:
        d = []
        pen = False
        for i, v in enumerate(ys):
            if not math.isfinite(v):
                pen = False
                continue
            d.append(("L" if pen else "M") + f"{X(i):.1f} {Y(v):.1f}")
            pen = True
        p.append(f'<path class="ln" d="{" ".join(d)}" style="stroke:var({var})"/>')
        # Direct end-label: identity without relying on color matching.
        last = next((i for i in range(len(ys) - 1, -1, -1) if math.isfinite(ys[i])), None)
        if last is not None and len(series) > 1:
            p.append(f'<circle class="dot" cx="{X(last):.1f}" cy="{Y(ys[last]):.1f}" r="4" '
                     f'style="fill:var({var})"/>')

    p.append(f'<g id="{chart_id}-hover" style="opacity:0">'
             f'<line class="cross" x1="0" y1="{y0}" x2="0" y2="{y1}"/></g>')
    p.append(f'<rect id="{chart_id}-hit" x="{x0}" y="{y0}" width="{x1-x0}" '
             f'height="{y1-y0}" fill="transparent"/>')
    p.append("</svg>")

    payload = {"dates": [d.isoformat() for d in dates],
               "series": [{"label": l, "values": [None if not math.isfinite(v) else round(v, 4)
                                                  for v in ys], "var": v}
                          for l, ys, v in series],
               "x0": x0, "x1": x1, "y0": y0, "y1": y1, "lo": lo, "hi": hi,
               "fmt": y_fmt, "width": width, "height": height}
    return (f'<div class="chart" id="{chart_id}">' + "".join(p) +
            f'<div class="tip" id="{chart_id}-tip"></div></div>'
            f'<script type="application/json" id="{chart_id}-data">{json.dumps(payload)}</script>')


def area_chart(dates, values, *, height=180, chart_id="a", width=1120, y_fmt="pct"):
    """Single-series filled area anchored at zero (the underwater plot)."""
    if not dates:
        return ""
    pad_l, pad_r, pad_t, pad_b = 74, 20, 12, 30
    x0, x1, y0, y1 = pad_l, width - pad_r, pad_t, height - pad_b
    lo = min([v for v in values if math.isfinite(v)] + [0.0])
    hi = 0.0
    ticks = _nice_ticks(lo, hi, 3)
    lo = min(lo, ticks[0])
    n = len(dates)
    def X(i): return x0 + (x1 - x0) * (i / max(n - 1, 1))
    def Y(v): return y1 - (y1 - y0) * ((v - lo) / (hi - lo or 1))

    p = [f'<svg viewBox="0 0 {width} {height}" role="img" preserveAspectRatio="xMidYMid meet">']
    for t in ticks:
        y = Y(t)
        p.append(f'<line class="g" x1="{x0}" y1="{y:.1f}" x2="{x1}" y2="{y:.1f}"/>')
        p.append(f'<text class="ax" x="{x0-10}" y="{y+4:.1f}" text-anchor="end">'
                 f'{_ESC(_fmt(t, y_fmt, 0))}</text>')
    p.append(_time_axis(dates, X, y0, y1, width))
    pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(values) if math.isfinite(v))
    p.append(f'<polygon class="fill2" points="{X(0):.1f},{Y(0):.1f} {pts} {X(n-1):.1f},{Y(0):.1f}"/>')
    p.append(f'<polyline class="ln2" points="{pts}"/>')
    p.append(f'<line class="base" x1="{x0}" y1="{Y(0):.1f}" x2="{x1}" y2="{Y(0):.1f}"/>')
    p.append(f'<g id="{chart_id}-hover" style="opacity:0">'
             f'<line class="cross" x1="0" y1="{y0}" x2="0" y2="{y1}"/></g>')
    p.append(f'<rect id="{chart_id}-hit" x="{x0}" y="{y0}" width="{x1-x0}" '
             f'height="{y1-y0}" fill="transparent"/>')
    p.append("</svg>")
    payload = {"dates": [d.isoformat() for d in dates],
               "series": [{"label": "Drawdown", "values": [round(v, 3) for v in values],
                           "var": "--series-1"}],
               "x0": x0, "x1": x1, "y0": y0, "y1": y1, "lo": lo, "hi": hi,
               "fmt": y_fmt, "width": width, "height": height}
    return (f'<div class="chart" id="{chart_id}">' + "".join(p) +
            f'<div class="tip" id="{chart_id}-tip"></div></div>'
            f'<script type="application/json" id="{chart_id}-data">{json.dumps(payload)}</script>')


def bar_chart(labels, values, *, height=None, chart_id="b", width=1120,
              horizontal=True, value_fmt="int", unit=""):
    """Single-series bars, one colour, value labelled at the tip."""
    if not labels:
        return ""
    pad_l = min(230, max(90, 9 * max(len(str(l)) for l in labels) + 16))
    pad_r, pad_t = 90, 8
    row = 30
    height = height or pad_t * 2 + row * len(labels)
    x0, x1 = pad_l, width - pad_r
    top = max(max(values), 1e-9)
    p = [f'<svg viewBox="0 0 {width} {height}" role="img" preserveAspectRatio="xMidYMid meet">']
    for i, (lab, v) in enumerate(zip(labels, values)):
        # 2px surface gap between adjacent bars; cap thickness at 24px.
        bh = min(24, row - 8)
        y = pad_t + i * row + (row - bh) / 2
        w = (x1 - x0) * (v / top)
        p.append(f'<text class="ax lbl" x="{x0-12}" y="{y+bh/2+4:.1f}" text-anchor="end">'
                 f'{_ESC(str(lab))}</text>')
        p.append(f'<rect class="bar" x="{x0}" y="{y:.1f}" width="{max(w,2):.1f}" '
                 f'height="{bh}" rx="4"/>')
        p.append(f'<rect class="barsq" x="{x0}" y="{y:.1f}" width="{min(4, max(w,2)):.1f}" '
                 f'height="{bh}"/>')
        p.append(f'<text class="val" x="{x0+max(w,2)+9:.1f}" y="{y+bh/2+4:.1f}">'
                 f'{_ESC(_fmt(v, value_fmt))}{_ESC(unit)}</text>')
    p.append("</svg>")
    return f'<div class="chart">' + "".join(p) + "</div>"


def histogram(values, *, bins=41, chart_id="h", width=1120, height=250,
              x_fmt="money0", label="trades"):
    """Distribution of a signed quantity; the axis carries the sign, so one colour."""
    vals = [v for v in values if math.isfinite(v)]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    if hi == lo:
        lo, hi = lo - 1, hi + 1
    span = (hi - lo) / bins
    counts = [0] * bins
    for v in vals:
        counts[min(bins - 1, max(0, int((v - lo) / span)))] += 1
    pad_l, pad_r, pad_t, pad_b = 60, 20, 14, 32
    x0, x1, y0, y1 = pad_l, width - pad_r, pad_t, height - pad_b
    top = max(counts)
    ticks = _nice_ticks(0, top, 4)
    slot = (x1 - x0) / bins

    p = [f'<svg viewBox="0 0 {width} {height}" role="img" preserveAspectRatio="xMidYMid meet">']
    for t in ticks:
        y = y1 - (y1 - y0) * (t / (ticks[-1] or 1))
        p.append(f'<line class="g" x1="{x0}" y1="{y:.1f}" x2="{x1}" y2="{y:.1f}"/>')
        p.append(f'<text class="ax" x="{x0-10}" y="{y+4:.1f}" text-anchor="end">{int(t):,}</text>')
    for i, c in enumerate(counts):
        if not c:
            continue
        bw = min(24, slot - 2)                       # 2px surface gap
        x = x0 + i * slot + (slot - bw) / 2
        bh = (y1 - y0) * (c / (ticks[-1] or 1))
        p.append(f'<rect class="bar" x="{x:.1f}" y="{y1-bh:.1f}" width="{bw:.1f}" '
                 f'height="{bh:.1f}" rx="4"><title>{_ESC(_fmt(lo+i*span, x_fmt))} to '
                 f'{_ESC(_fmt(lo+(i+1)*span, x_fmt))}: {c} {_ESC(label)}</title></rect>')
        p.append(f'<rect class="bar" x="{x:.1f}" y="{y1-min(4,bh):.1f}" width="{bw:.1f}" '
                 f'height="{min(4,bh):.1f}"/>')
    if lo < 0 < hi:
        xz = x0 + (x1 - x0) * ((0 - lo) / (hi - lo))
        p.append(f'<line class="zero" x1="{xz:.1f}" y1="{y0}" x2="{xz:.1f}" y2="{y1}"/>')
        p.append(f'<text class="ax" x="{xz:.1f}" y="{y0+11:.1f}" text-anchor="middle">0</text>')
    for f in (0.0, 0.25, 0.5, 0.75, 1.0):
        x = x0 + (x1 - x0) * f
        p.append(f'<text class="ax" x="{x:.1f}" y="{y1+18}" text-anchor="middle">'
                 f'{_ESC(_fmt(lo + (hi-lo)*f, x_fmt))}</text>')
    p.append(f'<line class="base" x1="{x0}" y1="{y1}" x2="{x1}" y2="{y1}"/>')
    p.append("</svg>")
    return f'<div class="chart">' + "".join(p) + "</div>"


def monthly_heatmap(monthly, yearly, *, width=1120):
    """Year x month grid.  Returns have polarity, so a diverging blue<->red ramp
    with a neutral midpoint; every cell is labelled, which is the table view."""
    if not monthly:
        return ""
    years = sorted({y for y, _ in monthly})
    mags = [abs(v) for v in monthly.values()]
    mags.sort()
    cap = mags[int(0.92 * (len(mags) - 1))] or 1.0        # clip outliers, keep contrast

    cw, ch, gap = 76, 30, 2
    lx = 46
    w = lx + 13 * cw + 8
    h = 26 + len(years) * ch + 10
    MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    def bucket(v):
        if v is None:
            return None
        f = min(abs(v) / cap, 1.0)
        step = 1 if f < 0.34 else (2 if f < 0.68 else 3)
        return f"--div-{'pos' if v > 0 else 'neg'}-{step}" if abs(v) > 1e-9 else "--div-mid"

    p = [f'<svg viewBox="0 0 {w} {h}" role="img" preserveAspectRatio="xMidYMid meet">']
    for m in range(12):
        p.append(f'<text class="ax" x="{lx+m*cw+cw/2:.0f}" y="16" text-anchor="middle">{MON[m]}</text>')
    p.append(f'<text class="ax" x="{lx+12*cw+cw/2:.0f}" y="16" text-anchor="middle">Year</text>')
    for r, y in enumerate(years):
        yy = 26 + r * ch
        p.append(f'<text class="ax" x="{lx-10}" y="{yy+ch/2+4:.0f}" text-anchor="end">{y}</text>')
        for m in range(12):
            v = monthly.get((y, m + 1))
            x = lx + m * cw
            if v is None:
                p.append(f'<rect x="{x+gap/2:.0f}" y="{yy+gap/2:.0f}" width="{cw-gap:.0f}" '
                         f'height="{ch-gap:.0f}" rx="4" class="empty"/>')
                continue
            p.append(f'<rect x="{x+gap/2:.0f}" y="{yy+gap/2:.0f}" width="{cw-gap:.0f}" '
                     f'height="{ch-gap:.0f}" rx="4" style="fill:var({bucket(v)})">'
                     f'<title>{y}-{m+1:02d}: {v:+.2f}%</title></rect>')
            p.append(f'<text class="cell" x="{x+cw/2:.0f}" y="{yy+ch/2+4:.0f}" '
                     f'text-anchor="middle">{v:+.1f}</text>')
        tv = yearly.get(y)
        if tv is not None:
            x = lx + 12 * cw
            p.append(f'<rect x="{x+gap/2:.0f}" y="{yy+gap/2:.0f}" width="{cw-gap:.0f}" '
                     f'height="{ch-gap:.0f}" rx="4" class="yearcell"/>')
            p.append(f'<text class="cellstrong {_cls(tv)}" x="{x+cw/2:.0f}" y="{yy+ch/2+4:.0f}" '
                     f'text-anchor="middle">{tv:+.1f}</text>')
    p.append("</svg>")
    return f'<div class="chart">' + "".join(p) + "</div>"


SVG_CSS = """
.g{stroke:var(--grid);stroke-width:1}
.base{stroke:var(--axis);stroke-width:1}
.zero{stroke:var(--axis);stroke-width:1}
.ax{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
.lbl{font-size:12px;fill:var(--text-secondary);font-variant-numeric:normal}
.val{fill:var(--text-secondary);font-size:12px;font-variant-numeric:tabular-nums}
.cell{fill:var(--text-primary);font-size:11px;font-variant-numeric:tabular-nums;opacity:.85}
.cellstrong{font-size:11.5px;font-weight:600;font-variant-numeric:tabular-nums}
.empty{fill:none;stroke:var(--grid);stroke-width:1}
.yearcell{fill:none;stroke:var(--axis);stroke-width:1}
.ln{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.ln2{fill:none;stroke:var(--series-1);stroke-width:2;stroke-linejoin:round}
.fill2{fill:var(--series-1);opacity:.10}
.bar{fill:var(--series-1)}
.barsq{fill:var(--series-1)}
.dot{stroke:var(--surface-1);stroke-width:2}
.cross{stroke:var(--axis);stroke-width:1}
.hdot{stroke:var(--surface-1);stroke-width:2}
"""

JS = """
function fmtv(v,k){ if(v===null||v===undefined) return '-';
  if(k==='money0') return '$'+Math.round(v).toLocaleString();
  if(k==='money')  return '$'+v.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
  if(k==='pct')    return v.toFixed(2)+'%';
  return v.toLocaleString(); }

document.querySelectorAll('script[type="application/json"]').forEach(function(s){
  var id=s.id.replace(/-data$/,''), box=document.getElementById(id);
  if(!box) return;
  var D=JSON.parse(s.textContent), svg=box.querySelector('svg'),
      tip=document.getElementById(id+'-tip'), hov=document.getElementById(id+'-hover'),
      hit=document.getElementById(id+'-hit');
  if(!hit) return;
  var line=hov.querySelector('line'), dots=[];
  D.series.forEach(function(se){
    var c=document.createElementNS('http://www.w3.org/2000/svg','circle');
    c.setAttribute('r',4); c.setAttribute('class','hdot');
    c.style.fill='var('+se.var+')'; hov.appendChild(c); dots.push(c);
  });
  function Y(v){ return D.y1-(D.y1-D.y0)*((v-D.lo)/(D.hi-D.lo||1)); }
  function move(ev){
    var r=svg.getBoundingClientRect(), sx=D.width/r.width;
    var px=(ev.clientX-r.left)*sx;
    var f=(px-D.x0)/(D.x1-D.x0), n=D.dates.length;
    var i=Math.round(f*(n-1)); if(i<0)i=0; if(i>n-1)i=n-1;
    var x=D.x0+(D.x1-D.x0)*(i/Math.max(n-1,1));
    line.setAttribute('x1',x); line.setAttribute('x2',x);
    var rows='<b>'+D.dates[i]+'</b>';
    D.series.forEach(function(se,k){
      var v=se.values[i];
      if(v===null){ dots[k].style.opacity=0; return; }
      dots[k].style.opacity=1; dots[k].setAttribute('cx',x); dots[k].setAttribute('cy',Y(v));
      rows+='<br><span style="display:inline-block;width:9px;height:9px;border-radius:50%;'
          + 'background:var('+se.var+');margin-right:6px"></span>'+se.label+' <b>'+fmtv(v,D.fmt)+'</b>';
    });
    hov.style.opacity=1; tip.style.opacity=1; tip.innerHTML=rows;
    var lx=(x/sx)+14; if(lx+tip.offsetWidth>r.width-6) lx=(x/sx)-tip.offsetWidth-14;
    tip.style.left=Math.max(4,lx)+'px'; tip.style.top='10px';
  }
  hit.addEventListener('mousemove',move);
  hit.addEventListener('mouseleave',function(){hov.style.opacity=0;tip.style.opacity=0;});
});

document.querySelectorAll('table[data-sortable]').forEach(function(t){
  t.querySelectorAll('thead th').forEach(function(th,i){
    th.addEventListener('click',function(){
      var desc=!th.classList.contains('desc');
      t.querySelectorAll('thead th').forEach(function(o){o.classList.remove('asc','desc');});
      th.classList.add(desc?'desc':'asc');
      var rows=Array.from(t.tBodies[0].rows);
      rows.sort(function(a,b){
        var x=a.cells[i].dataset.v!==undefined?a.cells[i].dataset.v:a.cells[i].textContent;
        var y=b.cells[i].dataset.v!==undefined?b.cells[i].dataset.v:b.cells[i].textContent;
        var nx=parseFloat(x), ny=parseFloat(y);
        var c=(!isNaN(nx)&&!isNaN(ny))?nx-ny:String(x).localeCompare(String(y));
        return desc?-c:c;
      });
      rows.forEach(function(r){t.tBodies[0].appendChild(r);});
    });
  });
});

document.querySelectorAll('table[data-filterable]').forEach(function(t){
  wireFilter(t.id+'-reason',t.id+'-q',t.id,t.id+'-count');
});

function wireFilter(selId,inpId,tblId,cntId){
  var sel=document.getElementById(selId), inp=document.getElementById(inpId),
      tbl=document.getElementById(tblId), cnt=document.getElementById(cntId);
  if(!tbl) return;
  function apply(){
    var r=sel?sel.value:'', q=(inp?inp.value:'').toLowerCase(), n=0;
    Array.from(tbl.tBodies[0].rows).forEach(function(row){
      var ok=(!r||row.dataset.reason===r)&&(!q||row.textContent.toLowerCase().indexOf(q)>=0);
      row.style.display=ok?'':'none'; if(ok)n++;
    });
    if(cnt) cnt.textContent=n.toLocaleString()+' shown';
  }
  if(sel) sel.addEventListener('change',apply);
  if(inp) inp.addEventListener('input',apply);
  apply();
}

function downloadCsv(tblId,name){
  var t=document.getElementById(tblId), out=[];
  out.push(Array.from(t.tHead.rows[0].cells).map(function(c){return c.textContent.trim();}).join(','));
  Array.from(t.tBodies[0].rows).forEach(function(r){
    if(r.style.display==='none') return;
    out.push(Array.from(r.cells).map(function(c){
      var v=c.dataset.v!==undefined?c.dataset.v:c.textContent.trim();
      return /[",]/.test(v)?'"'+v.replace(/"/g,'""')+'"':v;
    }).join(','));
  });
  var b=new Blob([out.join('\\n')],{type:'text/csv'}), a=document.createElement('a');
  a.href=URL.createObjectURL(b); a.download=name; a.click(); URL.revokeObjectURL(a.href);
}
"""


# ==========================================================================
# Aggregations
# ==========================================================================
def monthly_returns(daily):
    """Month-end to month-end percentage change in net liquidity."""
    if not daily:
        return {}, {}
    ends: "OrderedDict[tuple, float]" = OrderedDict()
    for d in daily:
        ends[(d.date.year, d.date.month)] = d.net_liq
    keys = list(ends)
    prev = daily[0].net_liq - (daily[0].total_pl if hasattr(daily[0], "total_pl") else 0.0)
    base = prev if prev > 0 else daily[0].net_liq
    monthly, yearly = {}, {}
    ystart = base
    for i, k in enumerate(keys):
        p = ends[keys[i - 1]] if i else base
        if p:
            monthly[k] = 100.0 * (ends[k] / p - 1.0)
        if i == len(keys) - 1 or keys[i + 1][0] != k[0]:
            if ystart:
                yearly[k[0]] = 100.0 * (ends[k] / ystart - 1.0)
            ystart = ends[k]
    return monthly, yearly


# ==========================================================================
# Page assembly
# ==========================================================================
def _tile(label, value, note="", cls="", hero=False):
    return (f'<div class="tile{" hero" if hero else ""}"><div class="lab">{_ESC(label)}</div>'
            f'<div class="val {cls}">{_ESC(str(value))}</div>'
            + (f'<div class="note">{_ESC(note)}</div>' if note else "") + "</div>")


def _trade_table(trades, tid="trades"):
    reasons = sorted({t.reason for t in trades})
    head = ["#", "Opened", "Closed", "DIT", "Qty", "Premium", "Gross", "Fees",
            "P/L", "ROI %", "Reason", "Spot open", "Spot close", "VIX open", "BP", "Legs"]
    rows = []
    for t in trades:
        rows.append(
            f'<tr data-reason="{_ESC(t.reason)}">'
            f'<td data-v="{t.id}">{t.id}</td>'
            f'<td class="l">{t.opened}</td><td class="l">{t.closed}</td>'
            f'<td data-v="{t.dit}">{t.dit}</td><td data-v="{t.contracts}">{t.contracts}</td>'
            f'<td data-v="{t.premium*100:.2f}">{_fmt(t.premium*100, "money")}</td>'
            f'<td data-v="{t.gross:.2f}">{_fmt(t.gross, "money")}</td>'
            f'<td data-v="{t.fees:.3f}">{_fmt(t.fees, "money")}</td>'
            f'<td data-v="{t.pnl:.2f}" class="{_cls(t.pnl)}">{_fmt(t.pnl, "money")}</td>'
            f'<td data-v="{t.roi:.2f}" class="{_cls(t.roi)}">{_fmt(t.roi, "pct")}</td>'
            f'<td class="l">{_ESC(t.reason)}</td>'
            f'<td data-v="{t.spot_open:.2f}">{_fmt(t.spot_open)}</td>'
            f'<td data-v="{t.spot_close:.2f}">{_fmt(t.spot_close)}</td>'
            f'<td data-v="{t.vix_open:.2f}">{_fmt(t.vix_open)}</td>'
            f'<td data-v="{t.buying_power:.0f}">{_fmt(t.buying_power, "money0")}</td>'
            f'<td class="l">{_ESC(t.legs)}</td></tr>')
    opts = "".join(f'<option value="{_ESC(r)}">{_ESC(r)}</option>' for r in reasons)
    return f"""
<div class="bar">
  <select id="{tid}-reason"><option value="">All close reasons</option>{opts}</select>
  <input id="{tid}-q" type="search" placeholder="Filter text..." style="min-width:220px">
  <button onclick="downloadCsv('{tid}','{tid}.csv')">Download CSV</button>
  <span class="hint" id="{tid}-count" style="margin:0"></span>
</div>
<div class="scroll"><table id="{tid}" data-sortable data-filterable>
<thead><tr>{''.join(f'<th>{_ESC(h)}</th>' for h in head)}</tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>"""


def _calibration_section(calib):
    out = []
    for layer in calib.layers:
        kv = "".join(f"<div>{_ESC(str(k))}</div><div>"
                     f"{_ESC(_fmt(v) if isinstance(v, float) else str(v))}</div>"
                     for k, v in layer.metrics.items())
        out.append(f'<div class="card"><h2>{_ESC(layer.name)}</h2>'
                   f'<div class="kv" style="margin-top:10px">{kv}</div></div>')
    return "".join(out)


def build_report(res, path, *, reference=None, calib=None, title=None) -> str:
    """Write a standalone HTML report and return its path."""
    s = summarize(res)
    strat = res.strategy
    daily = res.daily
    dates = [d.date for d in daily]
    name = title or strat.name

    monthly, yearly = monthly_returns(daily)

    series = [("Engine", [d.net_liq for d in daily], "--series-1")]
    if reference is not None:
        ref_map = {x.date: x.net_liq for x in reference.settlement}
        series.append(("tastytrade", [ref_map.get(d, float("nan")) for d in dates], "--series-2"))

    legend = ""
    if len(series) > 1:
        legend = '<div class="legend">' + "".join(
            f'<span><i class="key" style="background:var({v})"></i>{_ESC(l)}</span>'
            for l, _, v in series) + "</div>"

    reasons = Counter(t.reason for t in res.trades)
    rl = [k for k, _ in reasons.most_common()]
    rv = [reasons[k] for k in rl]

    final = s.get("final_net_liq", strat.capital)
    tiles = "".join([
        _tile("Final net liquidity", _fmt(final, "money"),
              f"from {_fmt(strat.capital, 'money0')}", _cls(final - strat.capital), hero=True),
        _tile("CAGR", _fmt(s.get("cagr_pct"), "spct"), f"{s.get('years',0):.1f} years",
              _cls(s.get("cagr_pct"))),
        _tile("Max drawdown", _fmt(s.get("max_drawdown_pct"), "pct"), "peak to trough", "neg"),
        _tile("Sharpe", _fmt(s.get("sharpe")), f"Sortino {_fmt(s.get('sortino'))}"),
        _tile("Win rate", _fmt(s.get("win_rate_pct"), "pct"), f"{s.get('trades',0):,} trades"),
        _tile("Profit factor", _fmt(s.get("profit_factor")),
              f"avg P/L {_fmt(s.get('avg_pnl'), 'money')}"),
    ])

    detail = "".join(f"<div>{_ESC(k)}</div><div>{_ESC(v)}</div>" for k, v in [
        ("Total return", _fmt(s.get("total_return_pct"), "spct")),
        ("Total P/L", _fmt(s.get("total_pnl"), "money")),
        ("Total fees", _fmt(s.get("total_fees"), "money")),
        ("Average win", _fmt(s.get("avg_win"), "money")),
        ("Average loss", _fmt(s.get("avg_loss"), "money")),
        ("Largest win", _fmt(s.get("largest_win"), "money")),
        ("Largest loss", _fmt(s.get("largest_loss"), "money")),
        ("Average days in trade", _fmt(s.get("avg_dit"), "num", 1)),
        ("Average premium", _fmt(s.get("avg_premium"), "money")),
        ("Average buying power", _fmt(s.get("avg_bp"), "money0")),
        ("Annualised volatility", _fmt(s.get("vol_annual_pct"), "pct")),
        ("Calmar", _fmt(s.get("calmar"))),
    ])

    skipped = ""
    if res.skipped:
        rows = "".join(f"<div>{_ESC(k)}</div><div>{v:,}</div>"
                       for k, v in sorted(res.skipped.items(), key=lambda kv: -kv[1]))
        skipped = (f'<div class="card"><h2>Entries not taken</h2>'
                   f'<p class="hint">Days the entry rule fired but no position could be opened.</p>'
                   f'<div class="kv">{rows}</div></div>')

    calib_html = ""
    if calib is not None:
        calib_html = (f'<h2 style="margin:26px 0 4px">Calibration against tastytrade</h2>'
                      f'<p class="hint">Layered comparison; see README section 3 for known limits.</p>'
                      + _calibration_section(calib))

    body = f"""
<div class="wrap">
<header>
  <h1>{_ESC(name)}</h1>
  <div class="sub">{s.get('start')} to {s.get('end')} &middot; {strat.underlying}
    &middot; generated {dt.datetime.now():%Y-%m-%d %H:%M}</div>
</header>

<div class="tiles">{tiles}</div>

<div class="card">
  <h2>Net liquidity</h2>
  <p class="hint">Cash plus the marked value of open positions, at each session's close.</p>
  {legend}
  {line_chart(dates, series, chart_id="eq", y_fmt="money0")}
</div>

<div class="card">
  <h2>Drawdown</h2>
  <p class="hint">Percentage below the running peak.</p>
  {area_chart(dates, [d.drawdown for d in daily], chart_id="dd")}
</div>

<div class="card">
  <h2>Monthly return</h2>
  <p class="hint">Percent change in net liquidity, month end to month end. Red is a
    loss, blue a gain; every cell is labelled, and the right column is the
    calendar year.</p>
  {monthly_heatmap(monthly, yearly)}
</div>

<div class="grid2">
  <div class="card">
    <h2>Trade P/L distribution</h2>
    <p class="hint">Net of fees, one bar per bucket.</p>
    {histogram([t.pnl for t in res.trades], chart_id="pl", width=540, height=230)}
  </div>
  <div class="card">
    <h2>Why trades closed</h2>
    <p class="hint">Count by exit reason.</p>
    {bar_chart(rl, rv, chart_id="rs", width=540)}
  </div>
</div>

<div class="card">
  <h2>Summary</h2>
  <div class="kv" style="margin-top:10px">{detail}</div>
</div>

{skipped}

<div class="card">
  <h2>Strategy</h2>
  <pre>{_ESC(strat.describe())}</pre>
  <div class="kv" style="margin-top:14px">
    <div>Quote model</div><div>{_ESC(strat.costs.quote_model)}</div>
    <div>Slippage (half-spreads)</div><div>{strat.costs.slippage:g}</div>
    <div>Commission per contract (open)</div><div>{_fmt(strat.costs.commission_open,'money')}</div>
    <div>Clearing fee per contract</div><div>{_fmt(strat.costs.clearing_fee,'money',3)}</div>
    <div>Assignment fee per contract</div><div>{_fmt(strat.costs.assignment_fee,'money')}</div>
  </div>
</div>

{calib_html}

<div class="card">
  <h2>Trade log</h2>
  <p class="hint">Sort by clicking a header. Every chart above is derived from this table.</p>
  {_trade_table(res.trades)}
</div>

<div class="foot">Generated by btengine &middot; {len(res.trades):,} trades &middot;
  {len(daily):,} sessions</div>
</div>"""

    doc = (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
           f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>{_ESC(name)} - backtest report</title>"
           f"<style>{CSS}{SVG_CSS}</style></head><body>{body}"
           f"<script>{JS}</script></body></html>")

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return path


# ==========================================================================
# Sweep report
# ==========================================================================
def _metric_scale(values):
    """Return (lo, hi, diverging).  A metric that straddles zero gets the
    diverging ramp; one that does not gets the single-hue sequential ramp."""
    v = [x for x in values if isinstance(x, (int, float)) and math.isfinite(x)]
    if not v:
        return 0.0, 1.0, False
    lo, hi = min(v), max(v)
    if lo == hi:
        hi = lo + 1e-9
    return lo, hi, (lo < 0 < hi)


def _metric_var(value, lo, hi, diverging):
    if value is None or not (isinstance(value, (int, float)) and math.isfinite(value)):
        return None
    if diverging:
        span = max(abs(lo), abs(hi)) or 1.0
        f = min(abs(value) / span, 1.0)
        if f < 1e-6:
            return "--div-mid"
        step = 1 if f < 0.34 else (2 if f < 0.68 else 3)
        return f"--div-{'pos' if value > 0 else 'neg'}-{step}"
    f = (value - lo) / (hi - lo)
    return f"--seq-{min(4, max(0, int(f * 5)) + 1)}"


def sweep_heatmap(result, metric, *, width=1120):
    """Two-axis grid: rows are axis 0, columns axis 1.  Every cell is labelled."""
    ax0, ax1 = result.axes[0], result.axes[1]
    grid = result.grid(metric)
    lo, hi, div = _metric_scale(grid.values())
    best = result.best(metric)
    best_key = tuple(best.overrides.get(a.path) for a in result.axes) if best else None

    cw = max(64, min(110, (width - 130) // max(len(ax1.values), 1)))
    ch, gap, lx, ty = 32, 2, 118, 40
    w = lx + len(ax1.values) * cw + 10
    h = ty + len(ax0.values) * ch + 12

    p = [f'<svg viewBox="0 0 {w} {h}" role="img" preserveAspectRatio="xMidYMid meet">']
    p.append(f'<text class="ax lbl" x="{lx}" y="14">{_ESC(ax1.label)} &#8594;</text>')
    for j, v1 in enumerate(ax1.values):
        p.append(f'<text class="ax" x="{lx+j*cw+cw/2:.0f}" y="32" text-anchor="middle">'
                 f'{_ESC(str(v1))}</text>')
    p.append(f'<text class="ax lbl" x="{lx-10}" y="14" text-anchor="end">'
             f'{_ESC(ax0.label)}</text>')
    for i, v0 in enumerate(ax0.values):
        y = ty + i * ch
        p.append(f'<text class="ax" x="{lx-10}" y="{y+ch/2+4:.0f}" text-anchor="end">'
                 f'{_ESC(str(v0))}</text>')
        for j, v1 in enumerate(ax1.values):
            val = grid.get((v0, v1))
            x = lx + j * cw
            var = _metric_var(val, lo, hi, div)
            if var is None:
                p.append(f'<rect x="{x+gap/2:.0f}" y="{y+gap/2:.0f}" width="{cw-gap:.0f}" '
                         f'height="{ch-gap:.0f}" rx="4" class="empty"/>')
                continue
            p.append(f'<rect x="{x+gap/2:.0f}" y="{y+gap/2:.0f}" width="{cw-gap:.0f}" '
                     f'height="{ch-gap:.0f}" rx="4" style="fill:var({var})">'
                     f'<title>{_ESC(ax0.label)}={v0}, {_ESC(ax1.label)}={v1}: '
                     f'{val:,.2f}</title></rect>')
            if best_key == (v0, v1):
                p.append(f'<rect x="{x+gap/2:.0f}" y="{y+gap/2:.0f}" width="{cw-gap:.0f}" '
                         f'height="{ch-gap:.0f}" rx="4" class="bestring"/>')
            p.append(f'<text class="cell" x="{x+cw/2:.0f}" y="{y+ch/2+4:.0f}" '
                     f'text-anchor="middle">{val:,.1f}</text>')
    p.append("</svg>")
    return '<div class="chart">' + "".join(p) + "</div>"


def sweep_response(result, metric, *, width=1120, height=260):
    """One-axis sweep: the metric as a function of the parameter."""
    ax = result.axes[0]
    grid = result.grid(metric)
    xs = ax.values
    ys = [grid.get((v,)) for v in xs]
    pts = [(i, y) for i, y in enumerate(ys) if isinstance(y, (int, float)) and math.isfinite(y)]
    if not pts:
        return "<p class='hint'>no finite results</p>"
    pad_l, pad_r, pad_t, pad_b = 74, 24, 16, 44
    x0, x1, y0, y1 = pad_l, width - pad_r, pad_t, height - pad_b
    vals = [y for _, y in pts]
    lo, hi = min(vals), max(vals)
    if lo == hi:
        lo, hi = lo - 1, hi + 1
    lo = min(lo, 0.0) if hi > 0 else lo
    ticks = _nice_ticks(lo, hi)
    lo, hi = min(lo, ticks[0]), max(hi, ticks[-1])

    def X(i): return x0 + (x1 - x0) * (i / max(len(xs) - 1, 1))
    def Y(v): return y1 - (y1 - y0) * ((v - lo) / (hi - lo))

    best_i = max(pts, key=lambda p: p[1])[0]
    p = [f'<svg viewBox="0 0 {width} {height}" role="img" preserveAspectRatio="xMidYMid meet">']
    for t in ticks:
        p.append(f'<line class="g" x1="{x0}" y1="{Y(t):.1f}" x2="{x1}" y2="{Y(t):.1f}"/>')
        p.append(f'<text class="ax" x="{x0-10}" y="{Y(t)+4:.1f}" text-anchor="end">'
                 f'{t:,.2f}</text>')
    if lo < 0 < hi:
        p.append(f'<line class="base" x1="{x0}" y1="{Y(0):.1f}" x2="{x1}" y2="{Y(0):.1f}"/>')
    p.append(f'<polyline class="ln2" points="'
             + " ".join(f"{X(i):.1f},{Y(y):.1f}" for i, y in pts) + '"/>')
    for i, y in pts:
        p.append(f'<circle class="dot" cx="{X(i):.1f}" cy="{Y(y):.1f}" r="5" '
                 f'style="fill:var(--series-1)"><title>{_ESC(ax.label)}={xs[i]}: '
                 f'{y:,.2f}</title></circle>')
        p.append(f'<text class="ax" x="{X(i):.1f}" y="{y1+18}" text-anchor="middle">'
                 f'{_ESC(str(xs[i]))}</text>')
    # Direct-label only the best point; the axis carries the rest.
    by = dict(pts)[best_i]
    p.append(f'<text class="val" x="{X(best_i):.1f}" y="{Y(by)-12:.1f}" '
             f'text-anchor="middle">{by:,.2f}</text>')
    p.append(f'<text class="ax lbl" x="{(x0+x1)/2:.0f}" y="{height-6}" '
             f'text-anchor="middle">{_ESC(ax.label)}</text>')
    p.append(f'<line class="base" x1="{x0}" y1="{y1}" x2="{x1}" y2="{y1}"/>')
    p.append("</svg>")
    return '<div class="chart">' + "".join(p) + "</div>"


def _marginals(result, metric):
    """For each axis: the metric at each of its values, aggregated over every
    other axis.  This is the view that answers 'which setting matters' when the
    grid has more dimensions than a heat map can show."""
    out = []
    for ax in result.axes:
        rows = []
        for v in ax.values:
            vals = [c.summary.get(metric) for c in result.ok
                    if c.overrides.get(ax.path) == v
                    and isinstance(c.summary.get(metric), (int, float))
                    and math.isfinite(c.summary.get(metric))]
            if vals:
                vals.sort()
                rows.append((v, vals[len(vals) // 2], vals[-1], vals[0], len(vals)))
            else:
                rows.append((v, None, None, None, 0))
        spread = [r[1] for r in rows if r[1] is not None]
        out.append((ax, rows, (max(spread) - min(spread)) if len(spread) > 1 else 0.0))
    return out


def marginal_chart(ax, rows, metric, *, width=352):
    """Median metric per axis value, as bars from a zero baseline."""
    vals = [r[1] for r in rows if r[1] is not None]
    if not vals:
        return ""
    lo, hi = min(vals + [0.0]), max(vals + [0.0])
    if hi == lo:
        hi = lo + 1.0
    lx, pad_r, row_h, top = 78, 58, 30, 26
    x0, x1 = lx, width - pad_r
    height = top + len(rows) * row_h + 8
    zero = x0 + (x1 - x0) * ((0.0 - lo) / (hi - lo))

    p = [f'<svg viewBox="0 0 {width} {height}" role="img" preserveAspectRatio="xMidYMid meet">']
    p.append(f'<text class="ax lbl" x="4" y="15">{_ESC(ax.label)}</text>')
    p.append(f'<line class="base" x1="{zero:.1f}" y1="{top-4}" x2="{zero:.1f}" '
             f'y2="{top + len(rows)*row_h:.0f}"/>')
    for i, (v, med, best, worst, n) in enumerate(rows):
        y = top + i * row_h
        bh = min(20, row_h - 8)
        p.append(f'<text class="ax" x="{lx-10}" y="{y+bh/2+4:.0f}" text-anchor="end">'
                 f'{_ESC(str(v))}</text>')
        if med is None:
            continue
        bx = x0 + (x1 - x0) * ((med - lo) / (hi - lo))
        left, w = (min(zero, bx), abs(bx - zero))
        p.append(f'<rect class="bar" x="{left:.1f}" y="{y+(row_h-bh)/2:.1f}" '
                 f'width="{max(w,2):.1f}" height="{bh}" rx="4">'
                 f'<title>{_ESC(ax.label)}={v}: median {med:,.2f}, '
                 f'best {best:,.2f}, worst {worst:,.2f} over {n} runs</title></rect>')
        tx = (left + max(w, 2) + 6) if med >= 0 else (left - 6)
        p.append(f'<text class="val" x="{tx:.1f}" y="{y+bh/2+4:.0f}" '
                 f'text-anchor="{"start" if med >= 0 else "end"}">{med:,.2f}</text>')
    p.append("</svg>")
    return '<div class="chart">' + "".join(p) + "</div>"


def sweep_slice(result, metric, ax_a, ax_b, fixed: dict, *, width=1120):
    """Heat grid over two axes, holding the others at `fixed` values."""
    sub = [c for c in result.ok
           if all(c.overrides.get(p) == v for p, v in fixed.items())]
    grid = {(c.overrides.get(ax_a.path), c.overrides.get(ax_b.path)): c.summary.get(metric)
            for c in sub}
    lo, hi, div = _metric_scale(grid.values())
    best_key = None
    good = [(k, v) for k, v in grid.items() if isinstance(v, (int, float)) and math.isfinite(v)]
    if good:
        best_key = max(good, key=lambda kv: kv[1])[0]

    cw = max(64, min(110, (width - 130) // max(len(ax_b.values), 1)))
    ch, gap, lx, ty = 32, 2, 118, 40
    w = lx + len(ax_b.values) * cw + 10
    h = ty + len(ax_a.values) * ch + 12
    p = [f'<svg viewBox="0 0 {w} {h}" role="img" preserveAspectRatio="xMidYMid meet">']
    p.append(f'<text class="ax lbl" x="{lx}" y="14">{_ESC(ax_b.label)} &#8594;</text>')
    p.append(f'<text class="ax lbl" x="{lx-10}" y="14" text-anchor="end">{_ESC(ax_a.label)}</text>')
    for j, vb in enumerate(ax_b.values):
        p.append(f'<text class="ax" x="{lx+j*cw+cw/2:.0f}" y="32" text-anchor="middle">'
                 f'{_ESC(str(vb))}</text>')
    for i, va in enumerate(ax_a.values):
        y = ty + i * ch
        p.append(f'<text class="ax" x="{lx-10}" y="{y+ch/2+4:.0f}" text-anchor="end">'
                 f'{_ESC(str(va))}</text>')
        for j, vb in enumerate(ax_b.values):
            val = grid.get((va, vb))
            x = lx + j * cw
            var = _metric_var(val, lo, hi, div)
            if var is None:
                p.append(f'<rect x="{x+gap/2:.0f}" y="{y+gap/2:.0f}" width="{cw-gap:.0f}" '
                         f'height="{ch-gap:.0f}" rx="4" class="empty"/>')
                continue
            p.append(f'<rect x="{x+gap/2:.0f}" y="{y+gap/2:.0f}" width="{cw-gap:.0f}" '
                     f'height="{ch-gap:.0f}" rx="4" style="fill:var({var})">'
                     f'<title>{_ESC(ax_a.label)}={va}, {_ESC(ax_b.label)}={vb}: '
                     f'{val:,.2f}</title></rect>')
            if best_key == (va, vb):
                p.append(f'<rect x="{x+gap/2:.0f}" y="{y+gap/2:.0f}" width="{cw-gap:.0f}" '
                         f'height="{ch-gap:.0f}" rx="4" class="bestring"/>')
            p.append(f'<text class="cell" x="{x+cw/2:.0f}" y="{y+ch/2+4:.0f}" '
                     f'text-anchor="middle">{val:,.1f}</text>')
    p.append("</svg>")
    return '<div class="chart">' + "".join(p) + "</div>"


SWEEP_CSS = """
.marg{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:8px}

:root{ --seq-1:#cde2fb; --seq-2:#86b6ef; --seq-3:#3987e5; --seq-4:#1c5cab; --seq-5:#0d366b;}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){
  --seq-1:#184f95; --seq-2:#256abf; --seq-3:#3987e5; --seq-4:#86b6ef; --seq-5:#cde2fb;}}
:root[data-theme="dark"]{
  --seq-1:#184f95; --seq-2:#256abf; --seq-3:#3987e5; --seq-4:#86b6ef; --seq-5:#cde2fb;}
.bestring{fill:none;stroke:var(--text-primary);stroke-width:2}
"""


def build_sweep_report(result, path, *, title=None, page_of=None) -> str:
    """Write the sweep grid, the response surface and the ranked table."""
    metric = result.metric
    higher_better = True
    ok = result.ok
    failed = [c for c in result.cells if c.error]
    best = result.best(metric)
    name = title or f"{result.base_name} sweep"

    axes_desc = " x ".join(f"{a.label} ({len(a.values)})" for a in result.axes)
    marg_html = ""
    if len(result.axes) == 2:
        chart = sweep_heatmap(result, metric)
        chart_hint = ("Colour and the printed value are the same number, so the grid "
                      "reads without hovering. The outlined cell is the best in sample.")
    elif len(result.axes) == 1:
        chart = sweep_response(result, metric)
        chart_hint = "Only the best point is labelled; hover any marker for its value."
    else:
        # More dimensions than a grid can show: lead with each axis's marginal
        # effect, then slice the two that move the metric most.
        marg = _marginals(result, metric)
        cards = "".join(marginal_chart(ax, rows, metric) for ax, rows, _ in marg)
        ranked_ax = sorted(marg, key=lambda x: -x[2])
        top = [ranked_ax[0][0], ranked_ax[1][0]]
        fixed = {}
        if best is not None:
            fixed = {a.path: best.overrides.get(a.path)
                     for a in result.axes if a not in top}
        slice_desc = ", ".join(f"{a.label}={best.overrides.get(a.path)}"
                               for a in result.axes if a not in top) if best else ""
        marg_html = f"""
<div class="card">
  <h2>Effect of each setting</h2>
  <p class="hint">Median {_ESC(metric)} at each value of an axis, taken over every
    combination of the others &mdash; so a long bar means that setting moves the
    result on its own, and a flat set of bars means it does not. Hover a bar for
    its best and worst.</p>
  <div class="marg">{cards}</div>
  <div class="kv" style="margin-top:14px">
    {''.join(f"<div>spread from {_ESC(ax.label)}</div><div>{sp:,.2f}</div>"
             for ax, _, sp in ranked_ax)}
  </div>
</div>"""
        chart = sweep_slice(result, metric, top[0], top[1], fixed)
        chart_hint = (f"The two axes that move {_ESC(metric)} most, sliced at the best "
                      f"cell's other settings ({_ESC(slice_desc)}). "
                      f"The outlined cell is the best in sample.")

    # Equity comparison: best / median / worst only.  More than a few curves on
    # one plot stops being readable and breaks the categorical palette rules.
    curves = ""
    ranked = sorted([c for c in ok if isinstance(c.summary.get(metric), (int, float))],
                    key=lambda c: c.summary[metric])
    if len(ranked) >= 2:
        picks = [("Worst", ranked[0], "--series-2"), ("Best", ranked[-1], "--series-1")]
        if len(ranked) >= 3:
            picks.insert(1, ("Median", ranked[len(ranked) // 2], "--series-3"))
        dates = sorted({d for _, c, _ in picks for d, _ in c.daily})
        series = []
        for label, c, var in picks:
            m = dict(c.daily)
            series.append((f"{label}: {c.label()}",
                           [m.get(d, float("nan")) for d in dates], var))
        legend = '<div class="legend">' + "".join(
            f'<span><i class="key" style="background:var({v})"></i>{_ESC(l)}</span>'
            for l, _, v in series) + "</div>"
        curves = f"""
<div class="card">
  <h2>Equity curves: best, median and worst</h2>
  <p class="hint">Ranked by {_ESC(metric)}. Only three of {len(ok)} runs are drawn -- more
     lines on one plot stop being readable.</p>
  {legend}
  {line_chart(dates, series, chart_id="sw", y_fmt="money0")}
</div>"""

    # Ranked table
    # `avg_premium` is here on purpose: a cell whose net basis is tiny makes
    # "% of premium" exits meaningless, and that has to be visible in the table.
    cols = ["cagr_pct", "total_return_pct", "max_drawdown_pct", "sharpe", "calmar",
            "win_rate_pct", "profit_factor", "trades", "avg_dit", "avg_premium",
            "total_fees"]
    heads = ["#"] + [a.label for a in result.axes] + \
            ["CAGR %", "Return %", "Max DD %", "Sharpe", "Calmar", "Win %",
             "Profit factor", "Trades", "Avg DIT", "Avg premium $", "Fees"]
    rows = []
    for c in sorted(ok, key=lambda c: -(c.summary.get(metric) or -1e18)):
        cells = "".join(f'<td data-v="{_ESC(str(c.overrides.get(a.path)))}">'
                        f'{_ESC(str(c.overrides.get(a.path)))}</td>' for a in result.axes)
        for k in cols:
            v = c.summary.get(k)
            fv = _fmt(v, "num", 0 if k == "trades" else 2)
            cls = _cls(v) if k in ("cagr_pct", "total_return_pct") else ""
            cells += f'<td data-v="{v if isinstance(v,(int,float)) else ""}" class="{cls}">{fv}</td>'
        mark = ' style="font-weight:600"' if best is not None and c is best else ""
        rows.append(f'<tr{mark}><td>{c.index}</td>{cells}</tr>')
    for c in failed:
        rows.append(f'<tr><td>{c.index}</td>'
                    + "".join(f'<td>{_ESC(str(c.overrides.get(a.path)))}</td>' for a in result.axes)
                    + f'<td class="l" colspan="{len(cols)}">{_ESC(c.error)}</td></tr>')

    tiles = ""
    if best is not None:
        b = best.summary
        tiles = "".join([
            _tile(f"Best {metric}", _fmt(b.get(metric), "num"), best.label(),
                  _cls(b.get(metric)), hero=True),
            _tile("Its CAGR", _fmt(b.get("cagr_pct"), "spct"), "", _cls(b.get("cagr_pct"))),
            _tile("Its max drawdown", _fmt(b.get("max_drawdown_pct"), "pct"), "", "neg"),
            _tile("Its Sharpe", _fmt(b.get("sharpe"))),
            _tile("Runs", f"{len(ok):,}",
                  f"{len(failed)} failed" if failed else "all completed"),
        ])

    spread = ""
    vals = [c.summary.get(metric) for c in ok
            if isinstance(c.summary.get(metric), (int, float))]
    if len(vals) > 2:
        vals_s = sorted(vals)
        spread = f"""
<div class="card">
  <h2>How much did the setting actually matter?</h2>
  <div class="kv" style="margin-top:10px">
    <div>Best {_ESC(metric)}</div><div>{_fmt(vals_s[-1])}</div>
    <div>Median</div><div>{_fmt(vals_s[len(vals_s)//2])}</div>
    <div>Worst</div><div>{_fmt(vals_s[0])}</div>
    <div>Spread (best - worst)</div><div>{_fmt(vals_s[-1]-vals_s[0])}</div>
  </div>
  <p class="hint" style="margin:14px 0 0">Every figure here is in sample. The best cell
    was chosen by looking at the same data it is scored on, so it is an optimistic
    estimate of what that setting would have delivered live. A broad, flat region of
    good values is more trustworthy than a lone peak beside poor neighbours.</p>
</div>"""

    body = f"""
<div class="wrap">
<header>
  <h1>{_ESC(name)}</h1>
  <div class="sub">{_ESC(axes_desc)} = {len(result.cells)} runs
    &middot; ranked by {_ESC(metric)}
    &middot; generated {dt.datetime.now():%Y-%m-%d %H:%M}</div>
</header>

<div class="tiles">{tiles}</div>

{marg_html}

<div class="card">
  <h2>{_ESC(metric)} across the grid</h2>
  <p class="hint">{chart_hint}</p>
  {chart}
</div>

{curves}
{spread}

<div class="card">
  <h2>All runs</h2>
  <p class="hint">Sort by any column. {len(failed)} run(s) failed.</p>
  <div class="bar">
    <button onclick="downloadCsv('sweep','sweep.csv')">Download CSV</button>
  </div>
  <div class="scroll"><table id="sweep" data-sortable>
    <thead><tr>{''.join(f'<th>{_ESC(h)}</th>' for h in heads)}</tr></thead>
    <tbody>{''.join(rows)}</tbody></table></div>
</div>

<div class="foot">{_ESC(result.base_name)} &middot; {len(result.cells)} parameter
  combinations</div>
</div>"""

    doc = (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
           f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>{_ESC(name)}</title>"
           f"<style>{CSS}{SVG_CSS}{SWEEP_CSS}</style></head><body>{body}"
           f"<script>{JS}</script></body></html>")
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return path


MANIFEST = "manifest.json"


def update_manifest(out_dir: str, entries) -> list:
    """Merge freshly generated reports into the directory's manifest and return
    every report still on disk, newest first, so the index stays complete
    across separate `run` / `report` / `calibrate` invocations."""
    path = os.path.join(out_dir, MANIFEST)
    store = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                store = json.load(fh)
        except (ValueError, OSError):
            store = {}
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    for name, file, s in entries:
        store[file] = {"name": name, "generated": stamp,
                       "summary": {k: (v.isoformat() if isinstance(v, dt.date) else v)
                                   for k, v in s.items() if not isinstance(v, dict)}}
    store = {f: v for f, v in store.items() if os.path.exists(os.path.join(out_dir, f))}
    os.makedirs(out_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(store, fh, indent=1)
    return [(v["name"], f, v["summary"], v.get("generated", ""))
            for f, v in sorted(store.items(), key=lambda kv: kv[1].get("generated", ""),
                               reverse=True)]


def build_index(entries, path, title="Backtest reports") -> str:
    """Landing page listing generated reports.  `entries` is [(name, file, summary)]
    or [(name, file, summary, generated)]."""
    cards = []
    for name, file, s, *rest in entries:
        when = rest[0] if rest else ""
        ret = s.get("total_return_pct")
        cards.append(f"""
<a class="card" href="{_ESC(file)}" style="display:block;text-decoration:none;color:inherit">
  <h2>{_ESC(name)}</h2>
  <p class="hint" style="margin-bottom:10px">{s.get('start')} to {s.get('end')}
     &middot; {s.get('trades',0):,} trades{(' &middot; ' + when.replace('T',' ')) if when else ''}</p>
  <div class="kv">
    <div>Final net liquidity</div><div>{_ESC(_fmt(s.get('final_net_liq'),'money'))}</div>
    <div>Total return</div><div class="{_cls(ret)}">{_ESC(_fmt(ret,'spct'))}</div>
    <div>CAGR</div><div class="{_cls(s.get('cagr_pct'))}">{_ESC(_fmt(s.get('cagr_pct'),'spct'))}</div>
    <div>Max drawdown</div><div class="neg">{_ESC(_fmt(s.get('max_drawdown_pct'),'pct'))}</div>
    <div>Win rate</div><div>{_ESC(_fmt(s.get('win_rate_pct'),'pct'))}</div>
  </div>
</a>""")
    doc = (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
           f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>{_ESC(title)}</title><style>{CSS}{SVG_CSS}</style></head><body>"
           f"<div class='wrap'><header><h1>{_ESC(title)}</h1>"
           f"<div class='sub'>generated {dt.datetime.now():%Y-%m-%d %H:%M}</div></header>"
           f"<div class='grid2'>{''.join(cards)}</div></div></body></html>")
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return path
