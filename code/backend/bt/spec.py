"""Strategy specification: the declarative surface of the backtester.

A strategy is a YAML/JSON document (or a plain dict) with four parts:

    legs    - what to trade, and how each strike/expiration is chosen
    entry   - when a new position may be opened, and how big
    exits   - ordered rules; the first whose condition is met closes the trade
    costs   - fill model and commission schedule

Every condition is an expression in the language of `expr.py`, so arbitrary
combinations of market state, position state and portfolio state are expressible
without touching engine code.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass, field
from typing import Any

from .expr import Rule, compile_rule

try:
    import yaml
except ImportError:                                   # pragma: no cover
    yaml = None

# ---------------------------------------------------------------------------
# Names a rule may reference.  Kept explicit so a typo fails loudly at load.
# ---------------------------------------------------------------------------
MARKET_VARS = {
    "date", "year", "month", "day", "dow", "dom", "week", "is_month_end", "is_month_start",
    "spot", "vix", "vix_prev", "vix_chg", "vix_rank", "vix_percentile",
    "iv30", "iv_rank", "iv_percentile", "term_slope",
    "ret_1d", "ret_5d", "ret_20d", "ret_60d", "realized_vol_10", "realized_vol_20", "realized_vol_60",
    "sma_20", "sma_50", "sma_200", "above_sma_20", "above_sma_50", "above_sma_200",
    "rsi_14", "atr_14", "drawdown_from_high", "high_252", "low_252",
}
PORTFOLIO_VARS = {
    "active_trades", "net_liq", "cash", "bp_used", "bp_free", "bp_pct",
    "equity_peak", "equity_dd", "day_index", "total_trades", "open_delta", "open_theta", "open_vega",
}
POSITION_VARS = {
    # `dte` is the NEAREST expiration in the structure.  For a multi-expiry
    # position (a calendar, or a short put bolted onto a longer call spread)
    # use `dte_max`, or `dte_of("leg")` to name the leg you mean.
    "dte", "dte_min", "dte_max", "dit", "pnl", "pnl_pct", "pnl_dollars", "premium", "value", "credit", "debit",
    "is_credit", "max_profit", "max_loss", "pct_max_profit", "pct_max_loss",
    "delta", "gamma", "theta", "vega", "short_delta", "long_delta",
    "underlying_move", "underlying_move_pct", "spot_at_open", "vix_at_open",
    "short_strike", "long_strike", "width", "itm", "any_itm", "all_itm", "moneyness",
    "touched_short", "contracts", "open_price", "mtm_high", "mtm_low",
}
ENTRY_NAMES = MARKET_VARS | PORTFOLIO_VARS
EXIT_NAMES = MARKET_VARS | PORTFOLIO_VARS | POSITION_VARS


class SpecError(ValueError):
    pass


def _as_date(v) -> dt.date | None:
    if v is None or isinstance(v, dt.date):
        return v
    if isinstance(v, dt.datetime):
        return v.date()
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y%m%d", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise SpecError(f"cannot parse date {v!r}")


# ---------------------------------------------------------------------------
# Legs
# ---------------------------------------------------------------------------
SELECTORS = ("delta", "strike", "offset", "offset_pct", "moneyness", "premium",
             "width", "atm", "otm_pct", "strike_of", "iv")


@dataclass
class LegSpec:
    """One leg of the structure, and the rule that picks its contract."""
    action: str                     # 'sell' | 'buy'
    right: str                      # 'C' | 'P'
    ratio: int = 1                  # contracts per unit of position size
    name: str | None = None

    # expiration
    dte: float | None = None        # target days to expiration
    dte_min: float | None = None
    dte_max: float | None = None
    dte_tie: str = "longer"         # 'longer' | 'shorter' — tie-break on |dte - target|
    expiration_of: str | None = None   # copy another leg's expiration (calendars)

    # strike
    selector: str = "delta"
    target: float | None = None
    anchor: str | None = None       # leg name that `width`/`strike_of` is measured from
    strike_round: str = "nearest"   # 'nearest' | 'up' | 'down'
    require_quote: bool = True      # skip strikes with no live bid
    min_open_interest: int = 0
    max_spread_pct: float | None = None   # reject if (ask-bid)/mark exceeds this

    @property
    def is_short(self) -> bool:
        return self.action == "sell"

    @property
    def signed_ratio(self) -> int:
        return self.ratio * (-1 if self.is_short else 1)

    @classmethod
    def from_dict(cls, d: dict, index: int) -> "LegSpec":
        d = dict(d)
        action = str(d.pop("action", d.pop("side", ""))).lower()
        if action in ("short", "s"):
            action = "sell"
        if action in ("long", "b"):
            action = "buy"
        if action not in ("buy", "sell"):
            raise SpecError(f"leg {index}: action must be buy or sell, got {action!r}")
        right = str(d.pop("right", d.pop("type", ""))).upper()[:1]
        if right not in ("C", "P"):
            raise SpecError(f"leg {index}: right must be call or put")

        sel_key = next((k for k in SELECTORS if k in d), None)
        if sel_key is None:
            raise SpecError(f"leg {index}: needs one of {SELECTORS}")
        target = d.pop(sel_key)
        anchor = d.pop("anchor", d.pop("from", None))
        if sel_key in ("width", "strike_of") and anchor is None:
            anchor = "$prev"

        leg = cls(action=action, right=right,
                  ratio=int(d.pop("ratio", d.pop("qty", 1))),
                  name=d.pop("name", None) or f"leg{index}",
                  dte=d.pop("dte", None), dte_min=d.pop("dte_min", None),
                  dte_max=d.pop("dte_max", None), dte_tie=str(d.pop("dte_tie", "longer")),
                  expiration_of=d.pop("expiration_of", None),
                  selector=sel_key, target=float(target) if sel_key != "strike_of" else target,
                  anchor=anchor, strike_round=str(d.pop("round", "nearest")),
                  require_quote=bool(d.pop("require_quote", True)),
                  min_open_interest=int(d.pop("min_open_interest", 0)),
                  max_spread_pct=d.pop("max_spread_pct", None))
        if d:
            raise SpecError(f"leg {index}: unknown key(s) {sorted(d)}")
        return leg


# ---------------------------------------------------------------------------
# Entry / exit / sizing
# ---------------------------------------------------------------------------
@dataclass
class EntrySpec:
    frequency: str = "daily"        # daily | weekly | monthly | <n>d
    weekdays: list[int] | None = None       # 0=Mon .. 4=Fri
    day_of_month: int | None = None
    #: Either a fixed count or an expression evaluated each day, so the number
    #: of concurrent positions can widen in a high-VIX or washed-out tape:
    #:     max_active: "3 if vix >= 30 else (2 if vix >= 20 else 1)"
    max_active: int | Rule = 1
    max_new_per_day: int = 1
    on_max: str = "skip"            # 'skip' only; queueing is deliberately absent
    min_days_between: int = 0
    condition: Rule | None = None
    allow_same_day_reentry: bool = True
    #: Refuse to open a position whose own exit rules are already satisfied at
    #: entry.  This is what makes "exit when VIX >= 25" also keep the strategy
    #: flat while VIX stays there -- tastytrade opened 0 of 1180 reference
    #: trades on a day whose exit condition was already true.
    skip_if_exit_met: bool = True

    @classmethod
    def from_dict(cls, d: dict, extra: set[str] | None = None) -> "EntrySpec":
        names = ENTRY_NAMES | (extra or set())
        d = dict(d or {})
        cond = d.pop("when", d.pop("condition", None))
        wd = d.pop("weekdays", None)
        if isinstance(wd, str):
            names = ["mon", "tue", "wed", "thu", "fri"]
            wd = [names.index(x.strip().lower()[:3]) for x in wd.split(",")]
        ma = d.pop("max_active", 1)
        ma = int(ma) if isinstance(ma, (int, float)) else compile_rule(ma, names)
        e = cls(frequency=str(d.pop("frequency", d.pop("enter", "daily"))).lower(),
                weekdays=wd, day_of_month=d.pop("day_of_month", None),
                max_active=ma,
                max_new_per_day=int(d.pop("max_new_per_day", 1)),
                on_max=str(d.pop("on_max", "skip")).lower(),
                min_days_between=int(d.pop("min_days_between", 0)),
                condition=compile_rule(cond, names),
                allow_same_day_reentry=bool(d.pop("allow_same_day_reentry", True)),
                skip_if_exit_met=bool(d.pop("skip_if_exit_met", True)))
        if d:
            raise SpecError(f"entry: unknown key(s) {sorted(d)}")
        return e


@dataclass
class ExitRule:
    condition: Rule
    reason: str
    priority: int = 0

    def __repr__(self) -> str:
        return f"ExitRule({self.reason}: {self.condition.source})"


#: Shorthand exit keys -> (expression template, reason).  These reproduce the
#: tastytrade backtester's own exit vocabulary exactly.
EXIT_SUGAR = {
    "dte":            ("dte <= {v}", "reached days to expiration limit"),
    "dit":            ("dit >= {v}", "reached days in trade limit"),
    "take_profit":    ("pnl_pct >= {v}", "take profit"),
    "stop_loss":      ("pnl_pct <= -({v})", "stop loss"),
    "profit_target":  ("pnl_dollars >= {v}", "profit target"),
    "loss_limit":     ("pnl_dollars <= -({v})", "loss limit"),
    "vix_above":      ("vix >= {v}", "reached minimum exit VIX"),
    "vix_below":      ("vix <= {v}", "reached maximum exit VIX"),
    "delta_above":    ("abs(short_delta) >= {v}", "short delta breach"),
    "touched_short":  ("touched_short", "underlying touched short strike"),
    "pct_max_profit": ("pct_max_profit >= {v}", "percent of max profit"),
}


@dataclass
class ScaleRule:
    """Adjust position size when a market condition holds.  First match wins."""
    condition: Rule
    factor: float | None = None     # multiply the base size
    contracts: int | None = None    # or set it outright
    label: str = ""

    def apply(self, base: int) -> int:
        if self.contracts is not None:
            return int(self.contracts)
        return int(round(base * (self.factor if self.factor is not None else 1.0)))


@dataclass
class SizingSpec:
    """How many contracts to trade.

    `mode` sets the base size; `scale` then adjusts it from market state, which
    is how "trade bigger when RSI is washed out or VIX is elevated" is
    expressed.  Rules are tested in order and the first match wins, so write
    them most-extreme first.
    """
    mode: str = "fixed"             # fixed | bp_fraction | risk_fraction | notional | expression
    contracts: int = 1
    fraction: float = 0.0
    expression: Rule | None = None  # mode='expression': evaluates to a contract count
    scale: list[ScaleRule] = field(default_factory=list)
    max_contracts: int = 10_000
    min_contracts: int = 1
    round_down: bool = True

    @classmethod
    def from_dict(cls, d, extra: set[str] | None = None) -> "SizingSpec":
        names = ENTRY_NAMES | (extra or set())
        if d is None:
            return cls()
        if isinstance(d, int):
            return cls(contracts=d)
        d = dict(d)
        scale = []
        for i, raw in enumerate(d.pop("scale", None) or []):
            raw = dict(raw)
            cond = raw.pop("when", raw.pop("condition", None))
            if cond is None:
                raise SpecError(f"sizing.scale[{i}]: needs 'when'")
            factor = raw.pop("factor", raw.pop("multiplier", None))
            contracts = raw.pop("contracts", None)
            if factor is None and contracts is None:
                raise SpecError(f"sizing.scale[{i}]: needs 'factor' or 'contracts'")
            if factor is not None and contracts is not None:
                raise SpecError(f"sizing.scale[{i}]: give 'factor' or 'contracts', not both")
            label = raw.pop("label", "") or str(cond)
            if raw:
                raise SpecError(f"sizing.scale[{i}]: unknown key(s) {sorted(raw)}")
            scale.append(ScaleRule(compile_rule(cond, names),
                                   None if factor is None else float(factor),
                                   None if contracts is None else int(contracts), label))
        expr = d.pop("expression", None)
        s = cls(mode=str(d.pop("mode", "expression" if expr else "fixed")).lower(),
                contracts=int(d.pop("contracts", 1)),
                fraction=float(d.pop("fraction", 0.0)),
                expression=compile_rule(expr, names) if expr else None,
                scale=scale,
                max_contracts=int(d.pop("max_contracts", 10_000)),
                min_contracts=int(d.pop("min_contracts", 1)),
                round_down=bool(d.pop("round_down", True)))
        if s.mode == "expression" and s.expression is None:
            raise SpecError("sizing: mode 'expression' needs an 'expression'")
        if d:
            raise SpecError(f"sizing: unknown key(s) {sorted(d)}")
        return s


@dataclass
class CostSpec:
    """tastytrade's actual schedule is the default; every number is overridable."""
    quote_model: str = "mark"          # mark | natural | worst | best
    slippage: float = 0.0              # fraction of the half-spread, paid against you
    round_to_penny: bool = True        # options trade in whole cents
    commission_open: float = 1.00      # per contract, opening only
    commission_close: float = 0.00
    clearing_fee: float = 0.127        # per contract, every option transaction
    regulatory_fee_sell: float = 0.015  # per contract, additional on sells
    assignment_fee: float = 5.00       # per ITM contract at expiration
    share_commission: float = 0.00

    @classmethod
    def from_dict(cls, d) -> "CostSpec":
        d = dict(d or {})
        c = cls(**{k: d.pop(k) for k in list(d) if k in cls.__dataclass_fields__})
        if d:
            raise SpecError(f"costs: unknown key(s) {sorted(d)}")
        return c


@dataclass
class Strategy:
    name: str
    legs: list[LegSpec]
    entry: EntrySpec = field(default_factory=EntrySpec)
    exits: list[ExitRule] = field(default_factory=list)
    sizing: SizingSpec = field(default_factory=SizingSpec)
    costs: CostSpec = field(default_factory=CostSpec)
    underlying: str = "SPY"
    start: dt.date | None = None
    end: dt.date | None = None
    capital: float = 100_000.0
    #: Named constants usable inside any rule.  They exist so a threshold can be
    #: swept by path (`params.rsi_gate=50,60,70`) instead of being buried in an
    #: expression string.
    params: dict = field(default_factory=dict)
    bp_limit: float | None = None       # max fraction of net liq committed
    expire_in_the_money: bool = True    # model assignment rather than closing at expiry
    close_at_expiry: bool = False       # if True, flatten at the expiration-day mark
    notes: str = ""

    # ---------------------------------------------------------------- loading
    @classmethod
    def from_dict(cls, raw: dict) -> "Strategy":
        d = dict(raw)
        legs_raw = d.pop("legs", None)
        if not legs_raw:
            raise SpecError("strategy needs at least one leg")
        legs = [LegSpec.from_dict(x, i) for i, x in enumerate(legs_raw)]
        names = [l.name for l in legs]
        if len(set(names)) != len(names):
            raise SpecError("leg names must be unique")

        # A leg without its own DTE inherits the first leg that has one.
        default_dte = next((l.dte for l in legs if l.dte is not None), None)
        for l in legs:
            if l.dte is None and l.dte_min is None and l.expiration_of is None:
                l.dte = default_dte

        params = dict(d.pop("params", None) or {})
        reserved = params.keys() & (EXIT_NAMES | {"rsi", "sma", "ema", "ret", "rvol",
                                                  "high", "low", "vix_avg", "percentile_of"})
        if reserved:
            raise SpecError(f"params: {sorted(reserved)} shadow built-in names; rename them")
        pnames = set(params)
        exits = cls._parse_exits(d.pop("exits", None) or d.pop("exit", None), pnames)

        s = cls(name=str(d.pop("name", "strategy")),
                legs=legs,
                entry=EntrySpec.from_dict(d.pop("entry", None), pnames),
                exits=exits,
                sizing=SizingSpec.from_dict(d.pop("sizing", None), pnames),
                params=params,
                costs=CostSpec.from_dict(d.pop("costs", None)),
                underlying=str(d.pop("underlying", "SPY")).upper(),
                start=_as_date(d.pop("start", None)), end=_as_date(d.pop("end", None)),
                capital=float(d.pop("capital", 100_000.0)),
                bp_limit=d.pop("bp_limit", None),
                expire_in_the_money=bool(d.pop("expire_in_the_money", True)),
                close_at_expiry=bool(d.pop("close_at_expiry", False)),
                notes=str(d.pop("notes", "")))
        if d:
            raise SpecError(f"strategy: unknown key(s) {sorted(d)}")
        return s

    @staticmethod
    def _parse_exits(raw, extra: set[str] | None = None) -> list[ExitRule]:
        names = EXIT_NAMES | (extra or set())
        if raw is None:
            return []
        out: list[ExitRule] = []
        if isinstance(raw, dict):
            extra = raw.get("rules") or []
            for key, val in raw.items():
                if key == "rules":
                    continue
                if key not in EXIT_SUGAR:
                    raise SpecError(f"exit: unknown shorthand {key!r}; "
                                    f"known: {sorted(EXIT_SUGAR)} or use 'rules'")
                tpl, reason = EXIT_SUGAR[key]
                out.append(ExitRule(compile_rule(tpl.format(v=val), names), reason))
            raw = extra
        for i, item in enumerate(raw or []):
            if isinstance(item, str):
                out.append(ExitRule(compile_rule(item, names), item))
                continue
            item = dict(item)
            cond = item.pop("when", item.pop("condition", None))
            if cond is None:
                sug = next((k for k in item if k in EXIT_SUGAR), None)
                if sug is None:
                    raise SpecError(f"exit rule {i}: needs 'when' or a shorthand key")
                tpl, reason = EXIT_SUGAR[sug]
                cond = tpl.format(v=item.pop(sug))
                item.setdefault("reason", reason)
            out.append(ExitRule(compile_rule(cond, names),
                                str(item.pop("reason", cond)),
                                int(item.pop("priority", 0))))
            if item:
                raise SpecError(f"exit rule {i}: unknown key(s) {sorted(item)}")
        out.sort(key=lambda r: -r.priority)
        return out

    @classmethod
    def load(cls, path: str) -> "Strategy":
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        if path.lower().endswith((".yaml", ".yml")):
            if yaml is None:
                raise SpecError("PyYAML is required to read .yaml strategy files")
            raw = yaml.safe_load(text)
        else:
            raw = json.loads(text)
        s = cls.from_dict(raw)
        if s.name == "strategy":
            s.name = os.path.splitext(os.path.basename(path))[0]
        return s

    # ---------------------------------------------------------------- helpers
    def leg(self, name: str) -> LegSpec | None:
        return next((l for l in self.legs if l.name == name), None)

    def describe(self) -> str:
        lines = [f"{self.name}  [{self.underlying}]"]
        for l in self.legs:
            when = f"{l.dte:g} DTE" if l.dte is not None else \
                   (f"exp of {l.expiration_of}" if l.expiration_of else
                    f"DTE {l.dte_min}-{l.dte_max}")
            lines.append(f"  {l.action:4s} {l.ratio} {'call' if l.right=='C' else 'put':4s} "
                         f"{l.selector}={l.target}  {when}")
        ma = self.entry.max_active
        ma_s = ma.source if isinstance(ma, Rule) else str(ma)
        lines.append(f"  entry: {self.entry.frequency}, max_active={ma_s}"
                     + (f", max {self.entry.max_new_per_day}/day"
                        if self.entry.max_new_per_day != 1 else "")
                     + (f", when {self.entry.condition.source}" if self.entry.condition else ""))
        s = self.sizing
        if s.mode == "expression":
            lines.append(f"  size : {s.expression.source}")
        elif s.mode != "fixed" or s.contracts != 1 or s.scale:
            base = f"{s.mode}" + (f" {s.fraction:g}" if s.mode != "fixed" else f" {s.contracts}")
            lines.append(f"  size : {base}"
                         + (f", max {s.max_contracts}" if s.max_contracts < 10_000 else ""))
        for r in s.scale:
            what = f"x{r.factor:g}" if r.factor is not None else f"={r.contracts}"
            lines.append(f"  scale: {r.condition.source}   -> {what}")
        for r in self.exits:
            lines.append(f"  exit : {r.condition.source}   -> {r.reason}")
        return "\n".join(lines)
