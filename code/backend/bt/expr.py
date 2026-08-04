"""A small, safe expression language for strategy rules.

Rules are ordinary Python expressions ("dte <= 21 and pnl_pct >= 0.5"), compiled
once and evaluated against a context dict.  Only a whitelisted subset of the AST
is allowed, so a spec file cannot import, call arbitrary code, or reach
attributes.  Unknown names raise at compile time rather than silently
evaluating to false, which is what makes a typo in a rule findable.
"""
from __future__ import annotations

import ast
import math
from typing import Any, Callable

_ALLOWED_NODES = (
    ast.Expression, ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare, ast.Call,
    ast.IfExp, ast.Name, ast.Load, ast.Constant, ast.Tuple, ast.List, ast.Set,
    ast.Dict, ast.Subscript, ast.Slice, ast.And, ast.Or, ast.Not,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.USub, ast.UAdd, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.In, ast.NotIn, ast.Is, ast.IsNot,
)


def _safe_abs(x):
    return abs(x) if x is not None and _finite(x) else float("nan")


def _finite(x) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


FUNCTIONS: dict[str, Callable] = {
    "abs": _safe_abs, "min": min, "max": max, "round": round,
    "floor": math.floor, "ceil": math.ceil, "sqrt": math.sqrt,
    "log": math.log, "exp": math.exp, "sign": lambda x: (x > 0) - (x < 0),
    "isfinite": _finite, "clamp": lambda x, lo, hi: min(max(x, lo), hi),
    "between": lambda x, lo, hi: lo <= x <= hi,
    "any": any, "all": all, "len": len, "sum": sum, "int": int, "float": float,
    "bool": bool, "str": str,
}

CONSTANTS: dict[str, Any] = {"True": True, "False": False, "None": None,
                             "true": True, "false": False, "nan": float("nan")}

#: Indicators supplied by the market context as callables, so a rule can ask for
#: any lookback rather than only the handful precomputed as scalars:
#: ``rsi(2) < 10``, ``spot > sma(200)``, ``rvol(10) > vix``.
#: They are permitted at compile time and bound per-date at evaluation time.
CONTEXT_FUNCTIONS: dict[str, str] = {
    "rsi": "Wilder RSI over n sessions",
    "sma": "simple moving average of the underlying over n sessions",
    "ema": "exponential moving average over n sessions",
    "ret": "percentage change in the underlying over n sessions",
    "rvol": "annualised realised volatility over n sessions, in points",
    "high": "highest close over the trailing n sessions",
    "low": "lowest close over the trailing n sessions",
    "vix_avg": "mean VIX over the trailing n sessions",
    "percentile_of": "percentile rank of today's value of a named series over n sessions",
    "dte_of": "days to expiration of a named leg (exit rules only)",
}


class RuleError(ValueError):
    pass


class Rule:
    """A compiled boolean/numeric expression."""

    __slots__ = ("source", "_code", "names")

    def __init__(self, source: str, allowed_names: set[str] | None = None):
        self.source = source.strip()
        try:
            tree = ast.parse(self.source, mode="eval")
        except SyntaxError as exc:
            raise RuleError(f"cannot parse rule {source!r}: {exc}") from exc

        names: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, _ALLOWED_NODES):
                raise RuleError(
                    f"rule {source!r} uses {type(node).__name__}, which is not permitted")
            if isinstance(node, ast.Call):
                if not isinstance(node.func, ast.Name) or \
                        (node.func.id not in FUNCTIONS and node.func.id not in CONTEXT_FUNCTIONS):
                    known = sorted(set(FUNCTIONS) | set(CONTEXT_FUNCTIONS))
                    raise RuleError(f"rule {source!r} calls an unknown function; "
                                    f"available: {', '.join(known)}")
                if node.keywords:
                    raise RuleError(f"rule {source!r}: keyword arguments are not supported")
            elif isinstance(node, ast.Name):
                names.add(node.id)

        self.names = {n for n in names if n not in FUNCTIONS and n not in CONSTANTS
                      and n not in CONTEXT_FUNCTIONS}
        if allowed_names is not None:
            unknown = self.names - allowed_names
            if unknown:
                raise RuleError(
                    f"rule {source!r} references unknown variable(s): {', '.join(sorted(unknown))}")
        self._code = compile(tree, f"<rule:{self.source}>", "eval")

    def __call__(self, ctx: dict) -> Any:
        env = dict(CONSTANTS)
        env.update(FUNCTIONS)
        env.update(ctx)
        try:
            return eval(self._code, {"__builtins__": {}}, env)   # noqa: S307 - AST whitelisted above
        except ZeroDivisionError:
            return False
        except (TypeError, ValueError):
            # A rule touching an unavailable quantity (nan/None) is simply not met.
            return False
        except NameError as exc:
            raise RuleError(f"rule {self.source!r}: {exc}") from exc

    def test(self, ctx: dict) -> bool:
        v = self(ctx)
        if isinstance(v, float) and not math.isfinite(v):
            return False
        return bool(v)

    def __repr__(self) -> str:
        return f"Rule({self.source!r})"


def compile_rule(src, allowed_names: set[str] | None = None) -> Rule | None:
    if src is None:
        return None
    if isinstance(src, Rule):
        return src
    if isinstance(src, bool):
        return Rule("True" if src else "False", allowed_names)
    return Rule(str(src), allowed_names)
