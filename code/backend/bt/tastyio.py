"""Readers for tastytrade backtester exports.

A tastytrade run exports four CSVs into one folder:

    ... Orders.csv             one row per order, legs in free text
    ... Trades.csv             one row per round trip
    ... Transactions.csv       one row per fill, with fees
    ... Daily Settlement.csv   daily net liquidity and drawdown

These are the reference the engine is calibrated against.
"""
from __future__ import annotations

import csv
import datetime as dt
import os
import re
from dataclasses import dataclass, field

LEG_RE = re.compile(
    r'(?P<act>buy to open|sell to open|buy to close|sell to close)\s+'
    r'(?P<qty>\d+)\s*x\s*\$(?P<strike>[\d.]+)\s+(?P<cp>put|call)\s+exp\.\s+'
    r'(?P<exp>\d+/\d+/\d+)\s*\((?P<dte>\d+)\s*DTE\)', re.I)
SHARE_RE = re.compile(r'(?P<act>buy to open|sell to open|buy to close|sell to close)\s+'
                      r'(?P<qty>\d+)\s+shares', re.I)
INSTR_RE = re.compile(r'\$(?P<strike>[\d.]+)\s+(?P<cp>put|call)\s+exp\.\s+(?P<exp>\d+/\d+/\d+)', re.I)


def _d(s: str) -> dt.date:
    m, d, y = s.strip().split("/")
    return dt.date(int(y), int(m), int(d))


def _find(folder: str, suffix: str) -> str:
    for f in sorted(os.listdir(folder)):
        if f.endswith(suffix):
            return os.path.join(folder, f)
    raise FileNotFoundError(f"no *{suffix} in {folder}")


def _rows(path: str):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        yield from csv.DictReader(fh)


@dataclass
class TastyLeg:
    action: str
    qty: int
    strike: float
    right: str          # 'C' | 'P'
    exp: dt.date
    dte: int


@dataclass
class TastyOrder:
    date: dt.date
    time: str
    trade: int
    price: float
    effect: str         # credit | debit
    legs: list[TastyLeg] = field(default_factory=list)
    shares: int = 0
    raw: str = ""

    @property
    def is_open(self) -> bool:
        return any(l.action.endswith("open") for l in self.legs)

    @property
    def signed_price(self) -> float:
        """Same convention as the engine: negative for a credit."""
        return -self.price if self.effect == "credit" else self.price


@dataclass
class TastyTrade:
    no: int
    opened: dt.date
    closed: dt.date | None
    premium: float          # dollars, signed: positive = credit
    pnl: float
    spot_open: float
    spot_close: float
    reason: str
    bp: float
    fees: float
    roi: float


@dataclass
class TastyTx:
    date: dt.date
    trade: int
    type: str
    instrument: str
    strike: float | None
    right: str | None
    exp: dt.date | None
    price: float            # dollars per share
    qty: int
    value: float
    effect: str
    fees: float


@dataclass
class TastySettle:
    date: dt.date
    pnl: float
    net_liq: float
    drawdown: float
    roi: float


@dataclass
class TastyRun:
    folder: str
    orders: list[TastyOrder]
    trades: list[TastyTrade]
    transactions: list[TastyTx]
    settlement: list[TastySettle]

    @property
    def name(self) -> str:
        return os.path.basename(os.path.normpath(self.folder))

    @property
    def capital(self) -> float:
        s = self.settlement[0]
        return round(s.net_liq - s.pnl, 2)

    @property
    def start(self) -> dt.date:
        return self.settlement[0].date

    @property
    def end(self) -> dt.date:
        return self.settlement[-1].date

    def opening_orders(self) -> list[TastyOrder]:
        return [o for o in self.orders if o.is_open]


def load_run(folder: str) -> TastyRun:
    orders = []
    for r in _rows(_find(folder, "Orders.csv")):
        legs = [TastyLeg(m["act"].lower(), int(m["qty"]), float(m["strike"]),
                         m["cp"].upper()[0], _d(m["exp"]), int(m["dte"]))
                for m in LEG_RE.finditer(r["Legs"])]
        shares = sum(int(m["qty"]) for m in SHARE_RE.finditer(r["Legs"]))
        orders.append(TastyOrder(_d(r["Date"]), r["Time"], int(r["Trade No."]),
                                 float(r["Price"]), r["Effect"], legs, shares, r["Legs"]))

    trades = []
    for r in _rows(_find(folder, "Trades.csv")):
        trades.append(TastyTrade(
            int(r["No."]), _d(r["Opened"]), _d(r["Closed"]) if r["Closed"] else None,
            float(r["Premium"]), float(r["Profit/loss"]),
            float(r["Underlying Price at Open"]),
            float(r["Underlying Price at Close"] or "nan"),
            r["Close reason"], float(r["Buying power"]), float(r["Fees"]), float(r["ROI"])))

    txs = []
    for r in _rows(_find(folder, "Transactions.csv")):
        m = INSTR_RE.search(r["Instrument"])
        txs.append(TastyTx(
            _d(r["Date"]), int(r["Trade No."]), r["Type"], r["Instrument"],
            float(m["strike"]) if m else None,
            m["cp"].upper()[0] if m else None,
            _d(m["exp"]) if m else None,
            float(r["Price"]) / 100.0, int(r["Quantity"]), float(r["Value"]),
            r["Effect"], float(r["Fees"])))

    settle = [TastySettle(_d(r["Date"]), float(r["Total profit/loss"]), float(r["Net liquidity"]),
                          float(r["Drawdown"]), float(r["ROI"]))
              for r in _rows(_find(folder, "Daily Settlement.csv"))]

    return TastyRun(folder, orders, trades, txs, settle)


def discover(root: str) -> list[str]:
    """Every subfolder of `root` that looks like a tastytrade export."""
    out = []
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        if os.path.isdir(p) and any(f.endswith("Trades.csv") for f in os.listdir(p)):
            out.append(p)
    return out
