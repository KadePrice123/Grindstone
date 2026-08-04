"""The backtest loop.

One pass per trading day, in the order tastytrade uses at its 15:45 ET decision
point:

    1. mark every open position to the day's quotes
    2. settle anything that reached expiration
    3. test exit rules, first match wins, and close
    4. test entry rules and open, so a position closed today can be replaced today
    5. record the daily settlement row

The databases hold one end-of-day snapshot per contract, so that snapshot stands
in for the 15:45 quote.  It was verified to be the same session's data and to
reproduce tastytrade's fills at the mid with zero median error.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

from .context import MarketContext
from .data import Chain, MarketData
from .portfolio import (MULT, ClosedTrade, Fill, Holding, Position, option_fee)
from .selection import SelectionError, build_structure
from .spec import Strategy
from .vix import VixSeries

_NAN = float("nan")


@dataclass
class DailyRow:
    date: dt.date
    net_liq: float
    cash: float
    open_pl: float
    closed_pl: float
    total_pl: float
    drawdown: float
    active: int
    bp_used: float
    spot: float
    vix: float


@dataclass
class BacktestResult:
    strategy: Strategy
    trades: list[ClosedTrade] = field(default_factory=list)
    daily: list[DailyRow] = field(default_factory=list)
    skipped: dict = field(default_factory=dict)
    open_at_end: list[Position] = field(default_factory=list)

    @property
    def net_liq(self) -> float:
        return self.daily[-1].net_liq if self.daily else self.strategy.capital


class Backtester:
    def __init__(self, md: MarketData, vix: VixSeries, warmup_days: int = 260,
                 progress=None):
        self.md = md
        self.vix = vix
        self.warmup_days = warmup_days
        self.progress = progress

    # ------------------------------------------------------------------ run
    def run(self, strat: Strategy) -> BacktestResult:
        dates = self.md.trading_dates(strat.start, strat.end)
        if not dates:
            raise ValueError("no trading dates in the requested range")
        all_dates = self.md.all_dates()
        i0 = all_dates.index(dates[0])
        warmup = all_dates[max(0, i0 - self.warmup_days):i0]
        ctx = MarketContext(self.md, dates, self.vix, warmup=warmup, progress=self.progress)

        res = BacktestResult(strategy=strat)
        cash = strat.capital
        realized = 0.0
        peak = strat.capital
        open_positions: list[Position] = []
        next_id = 1
        last_entry: dt.date | None = None
        last_spot: float | None = None
        skipped: dict[str, int] = {}

        for day_index, date in enumerate(dates):
            chain = self.md.chain(date)
            mkt = ctx.at(date)
            if strat.params:
                # Named constants are part of every rule environment.
                mkt = {**strat.params, **mkt}
            spot = mkt.get("spot", _NAN)

            if math.isfinite(spot):
                last_spot = spot
            elif last_spot is not None:
                spot = last_spot                      # carry forward, never settle on nan

            # ------------------------------------------------- 2 exit rules
            # Exits are tested before expiration is settled: on the expiration
            # day itself tastytrade still takes profit at 15:45 if the rule is
            # met, and only lets the position go to settlement otherwise.
            closed_today = False
            still_open: list[Position] = []
            for pos in open_positions:
                self._track(pos, chain, spot)
                reason = self._exit_reason(strat, pos, date, chain, mkt,
                                           open_positions, cash, realized, day_index,
                                           peak, res)
                if reason is None:
                    still_open.append(pos)
                    continue
                tr, cashflow = self._close(strat, pos, date, chain, spot, mkt, reason)
                cash += cashflow
                realized += tr.pnl
                res.trades.append(tr)
                closed_today = True
            open_positions = still_open

            # ------------------------------------------------- 3 expiration
            still_open = []
            for pos in open_positions:
                if pos.first_exp() > date:
                    still_open.append(pos)
                    continue
                if strat.close_at_expiry:
                    tr, cashflow = self._close(strat, pos, date, chain, spot, mkt, "expired")
                else:
                    tr, cashflow = self._settle_expiry(strat, pos, date, chain, spot, mkt)
                cash += cashflow
                if tr is None:                      # multi-expiry: legs remain
                    still_open.append(pos)
                    continue
                realized += tr.pnl
                res.trades.append(tr)
                closed_today = True
            open_positions = still_open

            # ----------------------------------------------------- 4 entries
            opened_today = 0
            while opened_today < strat.entry.max_new_per_day:
                pf = self._portfolio_vars(open_positions, chain, spot, cash, realized,
                                          strat, day_index, peak, len(res.trades))
                env = {**mkt, **pf}
                if not self._entry_allowed(strat, date, last_entry, open_positions, env):
                    break
                if closed_today and not strat.entry.allow_same_day_reentry:
                    break
                if strat.entry.condition is not None and not strat.entry.condition.test(env):
                    break
                pos, cashflow, why = self._open(strat, date, chain, mkt, env, next_id)
                if pos is None:
                    if why:
                        skipped[why] = skipped.get(why, 0) + 1
                    break
                open_positions.append(pos)
                cash += cashflow
                next_id += 1
                last_entry = date
                opened_today += 1

            # ------------------------------------------------- 5 settlement
            open_pl = sum(p.pnl_dollars(chain, spot) for p in open_positions)
            # `cash` already carries entry proceeds and fees, so net liquidity is
            # cash plus the signed market value of everything still open.
            net_liq = cash + sum(p.market_value(chain, spot) * p.contracts * MULT
                                 for p in open_positions)
            peak = max(peak, net_liq)
            res.daily.append(DailyRow(
                date=date, net_liq=net_liq, cash=cash,
                open_pl=open_pl, closed_pl=realized,
                total_pl=net_liq - strat.capital,
                drawdown=100.0 * (net_liq / peak - 1.0) if peak > 0 else 0.0,
                active=len(open_positions),
                bp_used=sum(p.buying_power() for p in open_positions),
                spot=spot, vix=mkt.get("vix", _NAN)))

            if self.progress and day_index % 250 == 0:
                self.progress(day_index, len(dates))

        res.open_at_end = open_positions
        res.skipped = skipped
        self.md.flush()
        self.vix.flush()
        return res

    # ------------------------------------------------------------- entries
    @staticmethod
    def _max_active(strat: Strategy, env: dict) -> int:
        """`max_active` may be an expression, so the book can widen when the
        market state calls for it (high VIX, washed-out RSI)."""
        ma = strat.entry.max_active
        if isinstance(ma, int):
            return ma
        v = ma(env)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            return 0
        return max(0, int(v))

    def _entry_allowed(self, strat: Strategy, date: dt.date,
                       last_entry: dt.date | None, open_positions: list[Position],
                       env: dict) -> bool:
        e = strat.entry
        if len(open_positions) >= self._max_active(strat, env):
            return False
        if last_entry is not None and e.min_days_between:
            if (date - last_entry).days < e.min_days_between:
                return False
        f = e.frequency
        if e.weekdays is not None and date.weekday() not in e.weekdays:
            return False
        if f in ("daily", "every_day", "every day"):
            return True
        if f in ("weekly", "every_week"):
            return last_entry is None or (date - last_entry).days >= 7
        if f in ("monthly", "every_month"):
            return last_entry is None or (date.year, date.month) != (last_entry.year, last_entry.month)
        if f.endswith("d") and f[:-1].isdigit():
            n = int(f[:-1])
            return last_entry is None or (date - last_entry).days >= n
        return True

    def _open(self, strat: Strategy, date: dt.date, chain: Chain | None,
              mkt: dict, env: dict, pid: int):
        if chain is None:
            return None, 0.0, "no chain"
        try:
            structure = build_structure(chain, strat.legs)
        except SelectionError as exc:
            return None, 0.0, str(exc)[:60]

        costs = strat.costs
        px_of: dict[str, float] = {}
        unit_mv = 0.0
        for leg, c in structure:
            side = "sell" if leg.is_short else "buy"
            px = c.fill_price(side, costs.quote_model, costs.slippage, costs.round_to_penny)
            if px <= 0:
                return None, 0.0, "zero-priced leg"
            px_of[leg.name] = px
            unit_mv += leg.signed_ratio * px

        size = self._size(strat, env, unit_mv, structure)
        if size < 1:
            return None, 0.0, "size below one contract"

        holdings, fills = [], []
        cash = 0.0
        for leg, c in structure:
            side = "sell" if leg.is_short else "buy"
            px = px_of[leg.name]
            qty = leg.ratio * size
            fee = option_fee(costs, qty, opening=True, selling=leg.is_short)
            cash += (px * qty * MULT if leg.is_short else -px * qty * MULT) - fee
            holdings.append(Holding(leg=leg.name, exp=c.exp, right=c.right, strike=c.strike,
                                    qty=leg.signed_ratio * 1, open_price=px, open_delta=c.delta))
            fills.append(Fill(date, f"{side} to open", c.exp, c.right, c.strike, px, qty, fee))

        pos = Position(id=pid, opened=date, contracts=size, holdings=holdings,
                       open_mv=unit_mv, spot_at_open=mkt.get("spot", _NAN),
                       vix_at_open=mkt.get("vix", _NAN),
                       fees_paid=sum(f.fees for f in fills), fills=fills)

        if strat.bp_limit is not None:
            net_liq = env.get("net_liq", strat.capital)
            if env.get("bp_used", 0.0) + pos.buying_power() > strat.bp_limit * net_liq:
                return None, 0.0, "buying-power limit"

        if strat.entry.skip_if_exit_met and strat.exits:
            probe = {**env, **self._position_vars(pos, date, chain, mkt)}
            for rule in strat.exits:
                if rule.condition.test(probe):
                    return None, 0.0, f"exit already met: {rule.reason}"
        return pos, cash, ""

    @staticmethod
    def _size(strat: Strategy, env: dict, unit_mv: float, structure) -> int:
        """Base size from `mode`, then the first matching scale rule."""
        s = strat.sizing
        if s.mode == "fixed":
            n = s.contracts
        elif s.mode == "expression":
            v = s.expression(env)
            n = int(v) if isinstance(v, (int, float)) and math.isfinite(v) else 0
        else:
            net_liq = env.get("net_liq", strat.capital)
            probe = Position(id=0, opened=structure[0][1].date, contracts=1,
                             holdings=[Holding(leg=l.name, exp=c.exp, right=c.right,
                                               strike=c.strike, qty=l.signed_ratio,
                                               open_price=c.mark, open_delta=c.delta)
                                       for l, c in structure],
                             open_mv=unit_mv, spot_at_open=_NAN, vix_at_open=_NAN)
            if s.mode == "bp_fraction":
                per = probe.buying_power()
                n = int((net_liq * s.fraction) / per) if per > 0 else 0
            elif s.mode == "risk_fraction":
                per = probe.max_loss()
                n = int((net_liq * s.fraction) / per) if per > 0 and math.isfinite(per) else 0
            elif s.mode == "notional":
                per = abs(unit_mv) * MULT
                n = int((net_liq * s.fraction) / per) if per > 0 else 0
            else:
                n = s.contracts

        if n <= 0:
            return 0            # cannot afford it, or the expression said no

        for rule in s.scale:
            if rule.condition.test(env):
                n = rule.apply(n)
                # Scaling *down* sizes a trade, it does not cancel one -- that is
                # the entry rule's job.  A factor that rounds below one contract
                # therefore floors at one, while an explicit `contracts: 0` is
                # taken at face value.
                if rule.contracts is None and n < 1:
                    n = 1
                break
        if n <= 0:
            return 0
        return max(s.min_contracts, min(int(n), s.max_contracts))

    # --------------------------------------------------------------- exits
    def _exit_reason(self, strat: Strategy, pos: Position, date: dt.date,
                     chain: Chain | None, mkt: dict, open_positions, cash, realized,
                     day_index, peak, res) -> str | None:
        if not strat.exits:
            return None
        env = {**mkt,
               **self._portfolio_vars(open_positions, chain, mkt.get("spot", _NAN), cash,
                                      realized, strat, day_index, peak, len(res.trades)),
               **self._position_vars(pos, date, chain, mkt)}
        for rule in strat.exits:
            if rule.condition.test(env):
                return rule.reason
        return None

    def _close(self, strat: Strategy, pos: Position, date: dt.date, chain: Chain | None,
               spot: float, mkt: dict, reason: str):
        costs = strat.costs
        cash = 0.0
        fills = []
        close_mv = 0.0
        for h in pos.holdings:
            c = h.contract(chain)
            closing_side = "buy" if h.qty < 0 else "sell"
            px = c.fill_price(closing_side, costs.quote_model, costs.slippage,
                              costs.round_to_penny) if c is not None else h.intrinsic(spot)
            qty = abs(h.qty) * pos.contracts
            fee = option_fee(costs, qty, opening=False, selling=(closing_side == "sell"))
            cash += (px * qty * MULT if closing_side == "sell" else -px * qty * MULT) - fee
            close_mv += h.qty * px
            fills.append(Fill(date, f"{closing_side} to close", h.exp, h.right, h.strike,
                              px, qty, fee))
        fees = pos.fees_paid + sum(f.fees for f in fills)
        gross = (close_mv - pos.open_mv) * pos.contracts * MULT
        return self._record(strat, pos, date, reason, close_mv, gross, fees, fills,
                            spot, mkt), cash

    def _settle_expiry(self, strat: Strategy, pos: Position, date: dt.date,
                       chain: Chain | None, spot: float, mkt: dict):
        """Cash-settle the legs that expire today.

        tastytrade assigns in-the-money shorts and liquidates the resulting stock
        at the expiration-day close, charging $5 per exercised contract; the net
        effect equals paying intrinsic value plus the fee, which is what is
        modelled here.  Multi-expiry structures only settle their expiring legs.
        """
        expiring = [h for h in pos.holdings if h.exp <= date]
        remaining = [h for h in pos.holdings if h.exp > date]
        costs = strat.costs
        cash = 0.0
        fills = []
        settle_mv = 0.0
        for h in expiring:
            intr = h.intrinsic(spot)
            qty = abs(h.qty) * pos.contracts
            fee = costs.assignment_fee * qty if intr > 0 else 0.0
            # A short pays intrinsic, a long receives it.
            cash += h.qty * pos.contracts * intr * MULT - fee
            settle_mv += h.qty * intr
            fills.append(Fill(date, "assignment" if intr > 0 else "expiration",
                              h.exp, h.right, h.strike, intr, qty, fee))
        if remaining:
            # Partial settlement: keep the position alive with the residual legs.
            pos.holdings = remaining
            pos.open_mv -= settle_mv
            pos.fees_paid += sum(f.fees for f in fills)
            pos.fills.extend(fills)
            return None, cash

        fees = pos.fees_paid + sum(f.fees for f in fills)
        gross = (settle_mv - pos.open_mv) * pos.contracts * MULT
        reason = "exercised" if any(f.action == "assignment" for f in fills) else "expired"
        return self._record(strat, pos, date, reason, settle_mv, gross, fees, fills,
                            spot, mkt), cash

    def _record(self, strat, pos, date, reason, close_mv, gross, fees, fills,
                spot, mkt) -> ClosedTrade:
        legs = "; ".join(
            f"{'short' if h.qty < 0 else 'long'} {abs(h.qty)*pos.contracts} "
            f"${h.strike:g} {'call' if h.right=='C' else 'put'} exp {h.exp:%Y-%m-%d}"
            for h in pos.holdings)
        return ClosedTrade(
            id=pos.id, opened=pos.opened, closed=date, contracts=pos.contracts,
            open_price=pos.open_mv, close_price=close_mv, premium=pos.premium,
            pnl=gross - fees, gross=gross, fees=fees, reason=reason,
            spot_open=pos.spot_at_open, spot_close=spot,
            vix_open=pos.vix_at_open, vix_close=mkt.get("vix", _NAN),
            dte_open=(pos.first_exp() - pos.opened).days, dit=(date - pos.opened).days,
            buying_power=pos.buying_power(), legs=legs, fills=pos.fills + fills)

    # ----------------------------------------------------------- rule state
    def _track(self, pos: Position, chain: Chain | None, spot: float) -> None:
        p = pos.pnl_pct(chain, spot)
        if math.isfinite(p):
            pos.mtm_high = max(pos.mtm_high, p)
            pos.mtm_low = min(pos.mtm_low, p)
        if not pos.touched_short and math.isfinite(spot):
            for h in pos.shorts():
                if (h.right == "P" and spot <= h.strike) or (h.right == "C" and spot >= h.strike):
                    pos.touched_short = True
                    break

    @staticmethod
    def _position_vars(pos: Position, date: dt.date, chain: Chain | None, mkt: dict) -> dict:
        spot = mkt.get("spot", _NAN)
        mv = pos.market_value(chain, spot)
        g = pos.greeks(chain)
        shorts, longs = pos.shorts(), pos.longs()
        maxp, maxl = pos.max_profit(), pos.max_loss()
        pnl_d = (mv - pos.open_mv) * pos.contracts * MULT
        pnl_pct = (mv - pos.open_mv) / pos.premium if pos.premium > 0 else 0.0
        s_delta = 0.0
        for h in shorts:
            c = h.contract(chain)
            if c is not None:
                s_delta += h.qty * c.delta
        l_delta = 0.0
        for h in longs:
            c = h.contract(chain)
            if c is not None:
                l_delta += h.qty * c.delta
        short_k = max((h.strike for h in shorts), default=_NAN)
        long_k = max((h.strike for h in longs), default=_NAN)
        itm = [h.is_itm(spot) for h in pos.holdings]
        dtes = {h.leg: (h.exp - date).days for h in pos.holdings}
        return {
            "dte": pos.dte(date), "dte_min": pos.dte(date),
            "dte_max": max(dtes.values()) if dtes else 0,
            "dte_of": lambda name: dtes.get(str(name), float("nan")),
            "dit": (date - pos.opened).days,
            "pnl": pnl_pct, "pnl_pct": pnl_pct, "pnl_dollars": pnl_d,
            "premium": pos.premium, "value": mv,
            "credit": pos.premium if pos.is_credit else 0.0,
            "debit": pos.premium if not pos.is_credit else 0.0,
            "is_credit": pos.is_credit,
            "max_profit": maxp, "max_loss": maxl,
            "pct_max_profit": (pnl_d / maxp) if (maxp and math.isfinite(maxp) and maxp > 0) else _NAN,
            "pct_max_loss": (-pnl_d / maxl) if (maxl and math.isfinite(maxl) and maxl > 0) else _NAN,
            "delta": g["delta"], "gamma": g["gamma"], "theta": g["theta"], "vega": g["vega"],
            "short_delta": s_delta, "long_delta": l_delta,
            "underlying_move": spot - pos.spot_at_open,
            "underlying_move_pct": (100.0 * (spot / pos.spot_at_open - 1.0)
                                    if pos.spot_at_open else _NAN),
            "spot_at_open": pos.spot_at_open, "vix_at_open": pos.vix_at_open,
            "short_strike": short_k, "long_strike": long_k, "width": pos.width(),
            "itm": any(itm), "any_itm": any(itm), "all_itm": all(itm) if itm else False,
            "moneyness": (spot / short_k - 1.0) if (math.isfinite(short_k) and short_k) else _NAN,
            "touched_short": pos.touched_short, "contracts": pos.contracts,
            "open_price": pos.open_mv, "mtm_high": pos.mtm_high, "mtm_low": pos.mtm_low,
        }

    @staticmethod
    def _portfolio_vars(open_positions, chain, spot, cash, realized, strat,
                        day_index, peak, total_trades) -> dict:
        mv = sum(p.market_value(chain, spot) * p.contracts * MULT for p in open_positions)
        net_liq = cash + mv
        bp = sum(p.buying_power() for p in open_positions)
        g = {"delta": 0.0, "theta": 0.0, "vega": 0.0}
        for p in open_positions:
            pg = p.greeks(chain)
            for k in g:
                g[k] += pg[k]
        return {
            "active_trades": len(open_positions), "net_liq": net_liq, "cash": cash,
            "bp_used": bp, "bp_free": max(net_liq - bp, 0.0),
            "bp_pct": (100.0 * bp / net_liq) if net_liq else _NAN,
            "equity_peak": peak, "equity_dd": (100.0 * (net_liq / peak - 1.0)) if peak else 0.0,
            "day_index": day_index, "total_trades": total_trades,
            "open_delta": g["delta"], "open_theta": g["theta"], "open_vega": g["vega"],
        }
