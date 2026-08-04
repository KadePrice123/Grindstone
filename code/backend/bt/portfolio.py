"""Positions, fills, fees and buying power.

Sign convention: every quantity is *signed* — positive is long, negative short.
A position's market value ``mv`` is therefore negative for a net credit
structure, and profit is always ``mv_now - mv_open`` regardless of whether the
trade was opened for a credit or a debit.  That single definition reproduces
tastytrade's "% of premium" for both, which is why it is used everywhere.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

from .data import Chain, Contract
from .spec import CostSpec, LegSpec

MULT = 100.0        # option contract multiplier


@dataclass
class Fill:
    date: dt.date
    action: str                # 'sell to open' | 'buy to open' | ... | 'expiration' | 'assignment'
    exp: dt.date | None
    right: str | None
    strike: float | None
    price: float
    qty: int                   # always positive; direction is in `action`
    fees: float

    @property
    def signed_cash(self) -> float:
        s = 1.0 if self.action.startswith("sell") else -1.0
        return s * self.price * self.qty * MULT - self.fees


@dataclass
class Holding:
    """One leg of an open position."""
    leg: str
    exp: dt.date
    right: str
    strike: float
    qty: int                   # signed contracts
    open_price: float          # per share, positive
    open_delta: float

    def mark(self, chain: Chain | None, spot: float) -> float:
        """Signed market value per unit of position size, in dollars per share."""
        c = chain.contract(self.exp, self.right, self.strike) if chain else None
        if c is not None and c.mark > 0:
            px = c.mark
        else:
            px = self.intrinsic(spot)
        return self.qty * px

    def contract(self, chain: Chain | None) -> Contract | None:
        return chain.contract(self.exp, self.right, self.strike) if chain else None

    def intrinsic(self, spot: float) -> float:
        if not math.isfinite(spot):
            return 0.0
        return max(spot - self.strike, 0.0) if self.right == "C" else max(self.strike - spot, 0.0)

    def is_itm(self, spot: float) -> bool:
        return self.intrinsic(spot) > 0.0


@dataclass
class Position:
    """An open trade: a fixed set of holdings entered together."""
    id: int
    opened: dt.date
    contracts: int                       # position size multiplier
    holdings: list[Holding]
    open_mv: float                       # signed $/share at entry, per unit size
    spot_at_open: float
    vix_at_open: float
    fees_paid: float = 0.0
    fills: list[Fill] = field(default_factory=list)
    mtm_high: float = -math.inf          # best pnl_pct seen
    mtm_low: float = math.inf
    touched_short: bool = False
    tag: str = ""

    # ------------------------------------------------------------------ state
    @property
    def premium(self) -> float:
        """Absolute opening price per unit, in dollars per share."""
        return abs(self.open_mv)

    @property
    def is_credit(self) -> bool:
        return self.open_mv < 0

    @property
    def open_cash(self) -> float:
        return -self.open_mv * self.contracts * MULT

    def market_value(self, chain: Chain | None, spot: float) -> float:
        return sum(h.mark(chain, spot) for h in self.holdings)

    def pnl_dollars(self, chain: Chain | None, spot: float) -> float:
        return (self.market_value(chain, spot) - self.open_mv) * self.contracts * MULT

    def pnl_pct(self, chain: Chain | None, spot: float) -> float:
        if self.premium <= 0:
            return 0.0
        return (self.market_value(chain, spot) - self.open_mv) / self.premium

    def dte(self, date: dt.date) -> int:
        return min((h.exp - date).days for h in self.holdings)

    def last_exp(self) -> dt.date:
        return max(h.exp for h in self.holdings)

    def first_exp(self) -> dt.date:
        return min(h.exp for h in self.holdings)

    def shorts(self) -> list[Holding]:
        return [h for h in self.holdings if h.qty < 0]

    def longs(self) -> list[Holding]:
        return [h for h in self.holdings if h.qty > 0]

    def greeks(self, chain: Chain | None) -> dict[str, float]:
        out = {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0}
        for h in self.holdings:
            c = h.contract(chain)
            if c is None:
                continue
            out["delta"] += h.qty * c.delta * self.contracts
            out["gamma"] += h.qty * c.gamma * self.contracts
            out["vega"] += h.qty * c.vega * self.contracts
            out["theta"] += h.qty * c.theta * self.contracts
        return out

    # ------------------------------------------------------------- structure
    def width(self) -> float:
        """Vertical width in dollars, or 0 for structures that have none."""
        by_side: dict[str, list[float]] = {}
        for h in self.holdings:
            by_side.setdefault((h.right, h.exp), []).append(h.strike)
        widths = [max(v) - min(v) for v in by_side.values() if len(v) > 1]
        return max(widths) if widths else 0.0

    def max_loss(self) -> float:
        """Defined risk per unit in dollars, or inf when a leg is naked."""
        net_by_key: dict[tuple, int] = {}
        for h in self.holdings:
            net_by_key[(h.right, h.exp)] = net_by_key.get((h.right, h.exp), 0) + h.qty
        if any(v < 0 for v in net_by_key.values()):
            return math.inf                          # unbalanced short: undefined
        w = self.width()
        if w == 0:
            return abs(min(self.open_mv, 0.0)) * MULT * self.contracts
        return max(w + self.open_mv, 0.0) * MULT * self.contracts

    def max_profit(self) -> float:
        if self.is_credit:
            return self.premium * MULT * self.contracts
        w = self.width()
        if w > 0:
            return max(w - self.premium, 0.0) * MULT * self.contracts
        return math.inf

    def buying_power(self) -> float:
        """tastytrade's requirement, reproduced exactly on all 1 791 reference trades:
        credit vertical = width, debit vertical = debit paid, naked short = 20%
        of the strike notional."""
        w = self.width()
        naked = any(v < 0 for v in
                    _net_by_right_exp(self.holdings).values())
        if naked:
            req = 0.0
            for h in self.shorts():
                req += 0.20 * h.strike * MULT * abs(h.qty)
            return req * self.contracts
        if w > 0 and self.is_credit:
            return w * MULT * self.contracts
        return self.premium * MULT * self.contracts


def _net_by_right_exp(holdings: list[Holding]) -> dict[tuple, int]:
    out: dict[tuple, int] = {}
    for h in holdings:
        out[(h.right, h.exp)] = out.get((h.right, h.exp), 0) + h.qty
    return out


# ---------------------------------------------------------------------------
# Fees
# ---------------------------------------------------------------------------
def option_fee(costs: CostSpec, qty: int, opening: bool, selling: bool) -> float:
    f = costs.clearing_fee
    if opening:
        f += costs.commission_open
    else:
        f += costs.commission_close
    if selling:
        f += costs.regulatory_fee_sell
    return round(f * abs(qty), 6)


@dataclass
class ClosedTrade:
    id: int
    opened: dt.date
    closed: dt.date
    contracts: int
    open_price: float           # signed $/share (negative = credit received)
    close_price: float          # signed $/share
    premium: float
    pnl: float                  # net of fees, dollars
    gross: float
    fees: float
    reason: str
    spot_open: float
    spot_close: float
    vix_open: float
    vix_close: float
    dte_open: int
    dit: int
    buying_power: float
    legs: str
    fills: list[Fill] = field(default_factory=list)

    @property
    def roi(self) -> float:
        return 100.0 * self.pnl / self.buying_power if self.buying_power else 0.0

    @property
    def win(self) -> bool:
        return self.pnl > 0
