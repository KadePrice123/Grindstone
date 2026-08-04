"""Market-data access layer over spy_options.db / spy_bars.db.

Everything is lazy.  A day's rows are fetched with one query; an expiration's
contracts are materialised only when a rule or a leg actually looks at them; and
implied vol / gamma / vega / theta are solved per contract on first access.
Over a 4 600-day backtest that is the difference between minutes and hours,
because a typical strategy touches two expirations out of the thirty listed.

Delta policy
------------
The database's ``delta`` column was validated against tastytrade's own strike
selections and reproduces them to a median error of ~0.001, so it is the source
of truth wherever it is present.  A Black-76 delta solved from the mark is the
fallback for the rows that hold a sentinel value.
"""
from __future__ import annotations

import datetime as dt
import math
import os
import sqlite3
from bisect import bisect_left, bisect_right
from collections import OrderedDict

import numpy as np

from .pricing import black76_greeks, forward_and_discount, implied_vol

CALL, PUT = 0, 1
_MIN_TENOR_YEARS = 0.5 / 365.0
_DELTA_SENTINEL = 9.0
_NAN = float("nan")


def to_ymd(d: dt.date) -> int:
    return d.year * 10000 + d.month * 100 + d.day


def from_ymd(n: int) -> dt.date:
    return dt.date(n // 10000, (n // 100) % 100, n % 100)


# ---------------------------------------------------------------------------
class Contract:
    """One option quote.  Delta is eager (it comes straight from the row);
    the model greeks are solved on first access and then cached."""

    __slots__ = ("date", "exp", "right", "strike", "bid", "ask", "mark", "volume",
                 "open_interest", "dte", "forward", "discount", "tenor",
                 "_delta", "_db_iv", "_g")

    def __init__(self, date, exp, right, strike, bid, ask, mark, volume, open_interest,
                 dte, tenor, forward, discount, db_delta, db_iv):
        self.date = date
        self.exp = exp
        self.right = right
        self.strike = strike
        self.bid = bid
        self.ask = ask
        self.mark = mark
        self.volume = volume
        self.open_interest = open_interest
        self.dte = dte
        self.tenor = tenor
        self.forward = forward
        self.discount = discount
        self._db_iv = db_iv
        self._g = None
        self._delta = (float(db_delta)
                       if db_delta is not None and abs(db_delta) < _DELTA_SENTINEL else None)

    # ------------------------------------------------------------- analytics
    def _solve(self) -> dict:
        if self._g is None:
            iv = implied_vol(self.mark, self.forward, self.strike, self.tenor,
                             self.right == "C", self.discount)
            if not math.isfinite(iv) and self._db_iv and 0 < self._db_iv < 6:
                iv = float(self._db_iv)
            g = black76_greeks(self.forward, self.strike, self.tenor,
                               iv if math.isfinite(iv) else 0.0, self.right == "C", self.discount)
            g["iv"] = iv
            self._g = g
        return self._g

    @property
    def iv(self) -> float:
        return self._solve()["iv"]

    @property
    def delta(self) -> float:
        if self._delta is None:
            self._delta = self._solve()["delta"]
        return self._delta

    @property
    def gamma(self) -> float:
        return self._solve()["gamma"]

    @property
    def vega(self) -> float:
        return self._solve()["vega"]

    @property
    def theta(self) -> float:
        return self._solve()["theta"]

    # ----------------------------------------------------------------- quote
    @property
    def spread(self) -> float:
        return max(self.ask - self.bid, 0.0)

    @property
    def is_call(self) -> bool:
        return self.right == "C"

    @property
    def abs_delta(self) -> float:
        return abs(self.delta)

    @property
    def has_quote(self) -> bool:
        return self.bid > 0 and self.ask >= self.bid

    @property
    def symbol(self) -> str:
        return f"SPY {self.exp:%y%m%d}{self.right}{self.strike:g}"

    def fill_price(self, side: str, model: str = "mark", slippage: float = 0.0,
                   round_to_penny: bool = True) -> float:
        """Transaction price for `side` in {'buy','sell'}.

        `slippage` is a fraction of the half-spread paid against the trader, so
        model='mark' with slippage=1.0 is the same as crossing to the natural.
        Options trade in whole cents, and a mid on a penny-wide market lands on a
        half cent half the time, so the result is rounded half-up by default --
        which is what tastytrade's own fills do.
        """
        buying = side == "buy"
        if not self.has_quote:
            px = max(self.mark, 0.0)
        else:
            half = 0.5 * (self.ask - self.bid)
            if model in ("natural", "worst"):
                base, half = (self.ask if buying else self.bid), 0.0
            elif model == "best":
                base, half = (self.bid if buying else self.ask), 0.0
            else:
                base = 0.5 * (self.bid + self.ask)
            px = max(base + (half * slippage if buying else -half * slippage), 0.0)
        if round_to_penny:
            px = math.floor(px * 100.0 + 0.5) / 100.0
        return px

    def __repr__(self) -> str:
        return f"<{self.symbol} {self.bid:.2f}/{self.ask:.2f} d={self.delta:+.3f}>"


# ---------------------------------------------------------------------------
class Expiry:
    """One expiration's contracts, materialised on first access."""

    __slots__ = ("exp", "dte", "tenor", "date", "_rows", "_calls", "_puts", "_fwd")

    def __init__(self, date: dt.date, exp: dt.date, rows: list):
        self.date = date
        self.exp = exp
        self.dte = (exp - date).days
        self.tenor = max(self.dte / 365.0, _MIN_TENOR_YEARS)
        self._rows = rows
        self._calls = None
        self._puts = None
        self._fwd = None

    # -------------------------------------------------------------- forward
    def _compute_forward(self) -> tuple[float, float]:
        cm: dict[int, float] = {}
        pm: dict[int, float] = {}
        live_c: set[int] = set()
        live_p: set[int] = set()
        for cp, k, bid, ask, mark, _v, _o, _d, _i in self._rows:
            m = mark if (mark and mark > 0) else 0.5 * ((bid or 0.0) + (ask or 0.0))
            if cp == CALL:
                cm[k] = m
                if bid and bid > 0:
                    live_c.add(k)
            else:
                pm[k] = m
                if bid and bid > 0:
                    live_p.add(k)
        both = sorted((live_c & live_p) or (set(cm) & set(pm)))
        if len(both) < 2:
            return _NAN, 1.0
        # Cap the working set to the strikes around the money: the wings carry
        # stale marks and the pairwise-slope step is quadratic in count.
        diffs = [cm[k] - pm[k] for k in both]
        atm_i = min(range(len(both)), key=lambda i: abs(diffs[i]))
        lo, hi = max(0, atm_i - 30), min(len(both), atm_i + 31)
        ks = both[lo:hi]
        return forward_and_discount([k / 1000.0 for k in ks],
                                    [cm[k] for k in ks], [pm[k] for k in ks])

    @property
    def forward(self) -> float:
        if self._fwd is None:
            self._fwd = self._compute_forward()
        return self._fwd[0]

    @property
    def discount(self) -> float:
        if self._fwd is None:
            self._fwd = self._compute_forward()
        return self._fwd[1]

    def override_forward(self, F: float) -> None:
        d = self._fwd[1] if self._fwd else 1.0
        self._fwd = (F, d)

    # ------------------------------------------------------------ contracts
    def _materialise(self) -> None:
        F, D = (self._fwd if self._fwd else self._compute_forward())
        self._fwd = (F, D)
        calls: dict[float, Contract] = {}
        puts: dict[float, Contract] = {}
        for cp, k, bid, ask, mark, vol, oi, dl, iv in self._rows:
            bid = bid or 0.0
            ask = ask or 0.0
            m = mark if (mark and mark > 0) else 0.5 * (bid + ask)
            strike = k / 1000.0
            right = "C" if cp == CALL else "P"
            c = Contract(self.date, self.exp, right, strike, bid, ask, m,
                         vol or 0, oi or 0, self.dte, self.tenor, F, D, dl, iv)
            (calls if cp == CALL else puts)[strike] = c
        self._calls, self._puts = calls, puts

    @property
    def calls(self) -> dict[float, Contract]:
        if self._calls is None:
            self._materialise()
        return self._calls

    @property
    def puts(self) -> dict[float, Contract]:
        if self._puts is None:
            self._materialise()
        return self._puts

    def side(self, right: str) -> dict[float, Contract]:
        return self.calls if right.upper().startswith("C") else self.puts

    def strikes(self, right: str) -> list[float]:
        return sorted(self.side(right))

    def get(self, right: str, strike: float) -> Contract | None:
        return self.side(right).get(strike)

    def __repr__(self) -> str:
        return f"<Expiry {self.exp} dte={self.dte}>"


# ---------------------------------------------------------------------------
class Chain:
    """One trading day's option chain."""

    __slots__ = ("date", "_expiries", "_spot")

    def __init__(self, date: dt.date, expiries: "OrderedDict[dt.date, Expiry]", spot=None):
        self.date = date
        self._expiries = expiries
        self._spot = spot

    @property
    def expiries(self) -> "OrderedDict[dt.date, Expiry]":
        return self._expiries

    def sorted_expiries(self) -> list[Expiry]:
        return list(self._expiries.values())          # already DTE-ordered

    def get(self, exp: dt.date) -> Expiry | None:
        return self._expiries.get(exp)

    def contract(self, exp: dt.date, right: str, strike: float) -> Contract | None:
        e = self._expiries.get(exp)
        return e.get(right, strike) if e else None

    @property
    def spot(self) -> float:
        if self._spot is None:
            self._spot = self._compute_spot()
        return self._spot

    def _compute_spot(self) -> float:
        """Median forward of the front expirations.  Any single expiry can have a
        broken quote set, so a median over several is used rather than the first."""
        votes = []
        for e in self.sorted_expiries():
            if e.dte > 45 and votes:
                break
            f = e.forward
            if math.isfinite(f) and f > 0:
                votes.append(f)
            if len(votes) >= 6:
                break
        if not votes:
            return _NAN
        spot = float(np.median(votes))
        # Clamp any expiration whose parity estimate is implausible relative to
        # spot; a bad wing quote must not leak into strike selection.
        for e in self.sorted_expiries():
            f = e._fwd[0] if e._fwd else None
            if f is not None and math.isfinite(f) and spot > 0:
                if abs(f / spot - 1.0) > 0.03 + 0.0008 * e.dte:
                    e.override_forward(spot)
        return spot


# ---------------------------------------------------------------------------
class MarketData:
    """Read-only access to the option chain and price history."""

    def __init__(self, options_db: str, bars_db: str | None = None,
                 cache_size: int = 8, cache_db: str | None = None):
        self.options_db = options_db
        self.bars_db = bars_db
        self._opt = sqlite3.connect(f"file:{options_db}?mode=ro", uri=True, check_same_thread=False)
        self._bars = None
        if bars_db and os.path.exists(bars_db):
            self._bars = sqlite3.connect(f"file:{bars_db}?mode=ro", uri=True, check_same_thread=False)
        self._cache: "OrderedDict[dt.date, Chain]" = OrderedDict()
        self._cache_size = cache_size
        self._dates: list[dt.date] | None = None
        self._spot: dict[dt.date, float] = {}
        self._store = None
        if cache_db:
            self._open_store(cache_db)

    # --------------------------------------------------------------- caching
    def _open_store(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._store = sqlite3.connect(path, check_same_thread=False)
        self._store.execute("CREATE TABLE IF NOT EXISTS spot (d INTEGER PRIMARY KEY, s REAL)")
        self._store.commit()
        for d, s in self._store.execute("SELECT d, s FROM spot"):
            self._spot[from_ymd(d)] = s

    def flush(self) -> None:
        if self._store is not None:
            self._store.commit()

    # ---------------------------------------------------------------- dates
    def all_dates(self) -> list[dt.date]:
        if self._dates is None:
            self._dates = [from_ymd(r[0]) for r in
                           self._opt.execute("SELECT DISTINCT d FROM opt ORDER BY d")]
        return self._dates

    def trading_dates(self, start: dt.date | None = None, end: dt.date | None = None) -> list[dt.date]:
        ds = self.all_dates()
        lo = bisect_left(ds, start) if start else 0
        hi = bisect_right(ds, end) if end else len(ds)
        return ds[lo:hi]

    def next_date(self, date: dt.date) -> dt.date | None:
        ds = self.all_dates()
        i = bisect_right(ds, date)
        return ds[i] if i < len(ds) else None

    def prev_date(self, date: dt.date) -> dt.date | None:
        ds = self.all_dates()
        i = bisect_left(ds, date)
        return ds[i - 1] if i > 0 else None

    # ---------------------------------------------------------------- chain
    def chain(self, date: dt.date) -> Chain | None:
        c = self._cache.get(date)
        if c is not None:
            self._cache.move_to_end(date)
            return c
        rows = self._opt.execute(
            "SELECT exp, cp, strike, bid, ask, mark, vol, oi, delta, iv FROM opt WHERE d=?",
            (to_ymd(date),)).fetchall()
        if not rows:
            return None
        by_exp: dict[int, list] = {}
        for r in rows:
            by_exp.setdefault(r[0], []).append(r[1:])
        di = to_ymd(date)
        expiries: "OrderedDict[dt.date, Expiry]" = OrderedDict()
        for exp_i in sorted(by_exp):
            if exp_i < di:
                continue
            exp = from_ymd(exp_i)
            expiries[exp] = Expiry(date, exp, by_exp[exp_i])
        if not expiries:
            return None
        c = Chain(date, expiries, self._spot.get(date))
        self._cache[date] = c
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return c

    # ----------------------------------------------------------------- spot
    def spot(self, date: dt.date) -> float:
        s = self._spot.get(date)
        if s is not None:
            return s
        ch = self.chain(date)
        s = ch.spot if ch else _NAN
        self._spot[date] = s
        if self._store is not None and math.isfinite(s):
            self._store.execute("INSERT OR REPLACE INTO spot VALUES (?,?)", (to_ymd(date), s))
        return s

    def spot_series(self, dates: list[dt.date], progress=None) -> list[float]:
        out = []
        for i, d in enumerate(dates):
            out.append(self.spot(d))
            if progress and i % 250 == 0:
                progress(i, len(dates))
        self.flush()
        return out

    # ------------------------------------------------------------- intraday
    def intraday_bars(self, date: dt.date):
        """(time, open, high, low, close), rescaled into raw price terms.

        spy_bars.db is stored back-adjusted (2013 levels sit near half the traded
        price), so each day is rescaled by chain-spot / adjusted close.
        """
        if self._bars is None:
            return []
        rows = self._bars.execute("SELECT t,o,h,l,c FROM bars WHERE d=?", (to_ymd(date),)).fetchall()
        out = []
        for t, o, h, l, c in rows:
            try:
                tt = dt.datetime.strptime(t.strip(), "%I:%M %p").time()
            except (ValueError, AttributeError):
                continue
            out.append((tt, o, h, l, c))
        if not out:
            return []
        out.sort(key=lambda x: x[0])
        adj_close = out[-1][4]
        ref = self.spot(date)
        scale = (ref / adj_close) if (adj_close and adj_close > 0 and math.isfinite(ref)) else 1.0
        return [(t, o * scale, h * scale, l * scale, c * scale) for t, o, h, l, c in out]

    def day_range(self, date: dt.date) -> tuple[float, float]:
        bars = self.intraday_bars(date)
        if not bars:
            return _NAN, _NAN
        return min(b[3] for b in bars), max(b[2] for b in bars)
