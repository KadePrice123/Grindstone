"""Unit tests for the backtest engine.

The numbers asserted here are the ones read directly off the tastytrade
reference exports, so a regression in the fee schedule, the buying-power
formula or the profit definition fails loudly.

    cd code && python -m pytest backend/bt/tests -q
    (also run by the verification gate: selftest.py `bt engine unit tests`)
"""
from __future__ import annotations

import datetime as dt
import math
import os
import sys

import pytest

# Resolve imports from code/ so `backend.bt` is the package, matching how the
# sidecar and the gate import the engine.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from backend.bt.data import Contract                                  # noqa: E402
from backend.bt.expr import Rule, RuleError, compile_rule             # noqa: E402
from backend.bt.portfolio import Holding, Position, option_fee        # noqa: E402
from backend.bt.pricing import (black76, black76_greeks,              # noqa: E402
                                forward_and_discount, implied_vol)
from backend.bt.selection import pick_expiry, pick_strike             # noqa: E402
from backend.bt.spec import CostSpec, LegSpec, SpecError, Strategy    # noqa: E402

D = dt.date


# --------------------------------------------------------------------- expr
class TestRules:
    def test_basic(self):
        assert Rule("dte <= 21").test({"dte": 20})
        assert not Rule("dte <= 21").test({"dte": 22})
        assert Rule("a and (b or c)").test({"a": True, "b": False, "c": True})

    def test_functions(self):
        assert Rule("abs(short_delta) >= 0.3").test({"short_delta": -0.4})
        assert Rule("between(vix, 15, 25)").test({"vix": 20})
        assert Rule("max(a, b) == 5").test({"a": 5, "b": 3})

    def test_unknown_name_rejected(self):
        with pytest.raises(RuleError):
            Rule("dtee <= 21", allowed_names={"dte"})

    def test_sandbox(self):
        for bad in ("__import__('os')", "().__class__", "[x for x in (1,2)]",
                    "open('f')", "lambda: 1"):
            with pytest.raises(RuleError):
                Rule(bad)

    def test_nan_is_falsey(self):
        # A rule touching an unavailable quantity must not fire.
        assert not Rule("vix >= 25").test({"vix": float("nan")})
        assert not Rule("pnl_pct >= 0.5").test({"pnl_pct": float("nan")})

    def test_compile_rule_passthrough(self):
        r = compile_rule("dte <= 5")
        assert compile_rule(r) is r
        assert compile_rule(None) is None


# ------------------------------------------------------------------ pricing
class TestPricing:
    def test_put_call_parity(self):
        F, K, T, v = 400.0, 405.0, 0.25, 0.20
        c = black76(F, K, T, v, True)
        p = black76(F, K, T, v, False)
        assert c - p == pytest.approx(F - K, abs=1e-9)

    def test_implied_vol_roundtrip(self):
        F, K, T, v = 400.0, 380.0, 0.12, 0.23
        px = black76(F, K, T, v, False)
        assert implied_vol(px, F, K, T, False) == pytest.approx(v, abs=1e-5)

    def test_iv_rejects_arbitrage(self):
        assert math.isnan(implied_vol(0.0, 400, 380, 0.1, False))
        assert math.isnan(implied_vol(400.0, 400, 380, 0.1, False))

    def test_delta_signs_and_bounds(self):
        gc = black76_greeks(400, 400, 0.25, 0.2, True)
        gp = black76_greeks(400, 400, 0.25, 0.2, False)
        assert 0 < gc["delta"] < 1 and -1 < gp["delta"] < 0
        assert gc["delta"] - gp["delta"] == pytest.approx(1.0, abs=1e-9)
        assert gc["gamma"] > 0 and gc["vega"] > 0 and gc["theta"] < 0

    def test_forward_extraction_ignores_bad_wings(self):
        F0, D0 = 500.0, 0.995
        ks = [float(k) for k in range(440, 561, 5)]
        calls, puts = [], []
        for k in ks:
            c = black76(F0, k, 0.1, 0.2, True, D0)
            p = black76(F0, k, 0.1, 0.2, False, D0)
            calls.append(c)
            puts.append(p)
        # Corrupt two deep wings the way a stale quote would.
        calls[0] += 4.0
        puts[-1] -= 3.5
        F, Dd = forward_and_discount(ks, calls, puts)
        assert F == pytest.approx(F0, abs=0.05)
        assert Dd == pytest.approx(D0, abs=0.01)


# --------------------------------------------------------------------- fees
class TestFees:
    """Every figure below is read straight from the reference Transactions.csv."""

    C = CostSpec()

    @pytest.mark.parametrize("opening,selling,expected", [
        (True, True, 1.142),      # sell to open
        (True, False, 1.127),     # buy to open
        (False, True, 0.142),     # sell to close
        (False, False, 0.127),    # buy to close
    ])
    def test_per_contract(self, opening, selling, expected):
        assert option_fee(self.C, 1, opening, selling) == pytest.approx(expected)

    def test_scales_with_quantity(self):
        assert option_fee(self.C, 7, True, True) == pytest.approx(7 * 1.142)

    def test_round_trip_matches_reference(self):
        # put spread trade #1: sell+buy to open, buy+sell to close -> 2.54
        total = (option_fee(self.C, 1, True, True) + option_fee(self.C, 1, True, False)
                 + option_fee(self.C, 1, False, False) + option_fee(self.C, 1, False, True))
        assert round(total, 2) == 2.54

    def test_naked_put_round_trip(self):
        total = option_fee(self.C, 1, True, True) + option_fee(self.C, 1, False, False)
        assert round(total, 2) == 1.27

    def test_assignment(self):
        # reference: sell to open (1.142) + $5 exercise = 6.14
        assert round(option_fee(self.C, 1, True, True) + self.C.assignment_fee, 2) == 6.14


# ---------------------------------------------------------------- positions
def _pos(legs, contracts=1, opened=D(2020, 1, 2)):
    holdings = [Holding(leg=f"l{i}", exp=exp, right=r, strike=k, qty=q,
                        open_price=px, open_delta=0.0)
                for i, (r, k, q, px, exp) in enumerate(legs)]
    mv = sum(q * px for _, _, q, px, _ in legs)
    return Position(id=1, opened=opened, contracts=contracts, holdings=holdings,
                    open_mv=mv, spot_at_open=300.0, vix_at_open=15.0)


class TestPosition:
    E = D(2020, 2, 21)

    def test_credit_spread_profit_definition(self):
        """Reference trade: sell 142p at 1.43, buy 137p at 0.68 -> 0.75 credit;
        closed for 0.33 is 56% of premium, which is what must clear a 50% rule."""
        p = _pos([("P", 142, -1, 1.43, self.E), ("P", 137, +1, 0.68, self.E)])
        assert p.open_mv == pytest.approx(-0.75)
        assert p.premium == pytest.approx(0.75)
        assert p.is_credit
        assert p.pnl_pct(None, 200.0) is not None
        mv_now = -0.33
        assert (mv_now - p.open_mv) / p.premium == pytest.approx(0.56, abs=1e-9)

    def test_debit_spread_profit_definition(self):
        """Reference call spread #1: 0.47 debit, closed at 0.77 -> +63.8%."""
        p = _pos([("C", 148, +1, 1.70, self.E), ("C", 152, -1, 1.23, self.E)])
        assert p.open_mv == pytest.approx(0.47)
        assert not p.is_credit
        assert (0.77 - p.open_mv) / p.premium == pytest.approx(0.6383, abs=1e-3)

    def test_stop_loss_at_300_pct(self):
        """A 0.30 credit stopped at -300% means buying it back for 1.20."""
        p = _pos([("P", 143, -1, 0.30, self.E)])
        assert (-1.20 - p.open_mv) / p.premium == pytest.approx(-3.0, abs=1e-9)

    def test_buying_power_credit_vertical_is_width(self):
        p = _pos([("P", 142, -1, 1.43, self.E), ("P", 137, +1, 0.68, self.E)])
        assert p.buying_power() == pytest.approx(500.0)

    def test_buying_power_debit_vertical_is_debit(self):
        p = _pos([("C", 148, +1, 1.70, self.E), ("C", 152, -1, 1.23, self.E)])
        assert p.buying_power() == pytest.approx(47.0)

    def test_buying_power_naked_put_is_20pct_of_strike(self):
        p = _pos([("P", 143, -1, 0.30, self.E)])
        assert p.buying_power() == pytest.approx(2860.0)
        p2 = _pos([("P", 736, -1, 1.83, self.E)])
        assert p2.buying_power() == pytest.approx(14720.0)

    def test_buying_power_scales_with_contracts(self):
        p = _pos([("P", 142, -1, 1.43, self.E), ("P", 137, +1, 0.68, self.E)], contracts=3)
        assert p.buying_power() == pytest.approx(1500.0)

    def test_max_profit_and_loss(self):
        p = _pos([("P", 142, -1, 1.43, self.E), ("P", 137, +1, 0.68, self.E)])
        assert p.max_profit() == pytest.approx(75.0)
        assert p.max_loss() == pytest.approx(425.0)

    def test_naked_short_has_undefined_risk(self):
        assert math.isinf(_pos([("P", 143, -1, 0.30, self.E)]).max_loss())

    def test_pnl_scales_with_contracts(self):
        p = _pos([("P", 142, -1, 1.43, self.E), ("P", 137, +1, 0.68, self.E)], contracts=5)
        assert p.pnl_dollars(None, 400.0) == pytest.approx((0.0 - (-0.75)) * 5 * 100)

    def test_intrinsic_and_itm(self):
        h = Holding("l", self.E, "P", 300.0, -1, 1.0, 0.0)
        assert h.intrinsic(290.0) == pytest.approx(10.0)
        assert h.intrinsic(310.0) == 0.0
        assert h.is_itm(290.0) and not h.is_itm(310.0)


# ------------------------------------------------------------------ filling
def _c(bid, ask, strike=300.0, right="P", dte=45):
    date = D(2020, 1, 2)
    return Contract(date, date + dt.timedelta(days=dte), right, strike, bid, ask,
                    0.5 * (bid + ask), 10, 100, dte, dte / 365.0, 300.0, 1.0, None, None)


class TestFills:
    def test_mark_is_the_mid(self):
        c = _c(1.41, 1.43)
        assert c.fill_price("sell") == pytest.approx(1.42)
        assert c.fill_price("buy") == pytest.approx(1.42)

    def test_penny_rounding_is_half_up(self):
        c = _c(0.66, 0.69)          # mid 0.675
        assert c.fill_price("buy") == pytest.approx(0.68)
        assert c.fill_price("buy", round_to_penny=False) == pytest.approx(0.675)

    def test_natural_crosses_the_spread(self):
        c = _c(1.41, 1.43)
        assert c.fill_price("buy", "natural") == pytest.approx(1.43)
        assert c.fill_price("sell", "natural") == pytest.approx(1.41)

    def test_slippage_moves_against_the_trader(self):
        c = _c(1.00, 1.40, )
        assert c.fill_price("buy", "mark", 1.0, False) == pytest.approx(1.40)
        assert c.fill_price("sell", "mark", 1.0, False) == pytest.approx(1.00)
        assert c.fill_price("buy", "mark", 0.5, False) == pytest.approx(1.30)

    def test_missing_quote_falls_back_to_mark(self):
        c = _c(0.0, 0.0)
        c.mark = 0.05
        assert c.fill_price("buy") == pytest.approx(0.05)

    def test_never_negative(self):
        c = _c(0.01, 0.02)
        assert c.fill_price("sell", "mark", 5.0) >= 0.0


# --------------------------------------------------------------------- spec
class TestSpec:
    BASE = {"name": "t", "legs": [{"action": "sell", "right": "put", "dte": 45, "delta": 0.30}]}

    def test_minimal(self):
        s = Strategy.from_dict(self.BASE)
        assert s.legs[0].is_short and s.legs[0].right == "P"
        assert s.legs[0].signed_ratio == -1

    def test_exit_shorthand_expands(self):
        s = Strategy.from_dict({**self.BASE, "exits": {"dte": 21, "take_profit": 0.5,
                                                       "stop_loss": 3.0, "vix_above": 25}})
        srcs = {r.condition.source for r in s.exits}
        assert "dte <= 21" in srcs
        assert "pnl_pct >= 0.5" in srcs
        assert "pnl_pct <= -(3.0)" in srcs
        assert "vix >= 25" in srcs
        assert any(r.reason == "reached minimum exit VIX" for r in s.exits)

    def test_free_form_exit_rules(self):
        s = Strategy.from_dict({**self.BASE, "exits": {"rules": [
            {"when": "dit >= 10 and pnl_pct > 0.25", "reason": "time+profit"},
            {"when": "abs(short_delta) > 0.45", "reason": "delta breach", "priority": 5},
        ]}})
        assert s.exits[0].reason == "delta breach"      # priority ordering
        assert len(s.exits) == 2

    def test_second_leg_inherits_dte(self):
        s = Strategy.from_dict({"name": "t", "legs": [
            {"action": "sell", "right": "put", "dte": 45, "delta": 0.30},
            {"action": "buy", "right": "put", "width": 5},
        ]})
        assert s.legs[1].dte == 45
        assert s.legs[1].anchor == "$prev"

    def test_rejects_unknown_keys(self):
        with pytest.raises(SpecError):
            Strategy.from_dict({**self.BASE, "wobble": 3})
        with pytest.raises(SpecError):
            Strategy.from_dict({"name": "t", "legs": [
                {"action": "sell", "right": "put", "dte": 45, "delta": 0.3, "nope": 1}]})

    def test_rejects_bad_rule_variable(self):
        with pytest.raises(Exception):
            Strategy.from_dict({**self.BASE, "exits": {"rules": [{"when": "dtee <= 21"}]}})

    def test_duplicate_leg_names_rejected(self):
        with pytest.raises(SpecError):
            Strategy.from_dict({"name": "t", "legs": [
                {"name": "a", "action": "sell", "right": "put", "dte": 45, "delta": 0.3},
                {"name": "a", "action": "buy", "right": "put", "dte": 45, "delta": 0.15}]})

    def test_leg_needs_a_selector(self):
        with pytest.raises(SpecError):
            LegSpec.from_dict({"action": "sell", "right": "put", "dte": 45}, 0)


# ---------------------------------------------------------------- selection
class _FakeExpiry:
    def __init__(self, exp, dte, contracts):
        self.exp, self.dte = exp, dte
        self._c = contracts

    def side(self, right):
        return {c.strike: c for c in self._c if c.right == right.upper()[:1]}


class _FakeChain:
    def __init__(self, expiries, spot=300.0):
        self._e = expiries
        self.spot = spot

    def sorted_expiries(self):
        return sorted(self._e, key=lambda e: e.dte)

    def get(self, exp):
        return next((e for e in self._e if e.exp == exp), None)


def _chain_with(dtes, spot=300.0):
    base = D(2020, 1, 2)
    exps = []
    for dte in dtes:
        cs = []
        for k in range(int(spot) - 30, int(spot) + 31):
            c = _c(1.0, 1.1, float(k), "P", dte)
            c._delta = -max(0.001, min(0.999, 0.5 - (k - spot) * -0.02))
            cs.append(c)
        exps.append(_FakeExpiry(base + dt.timedelta(days=dte), dte, cs))
    return _FakeChain(exps, spot)


class TestSelection:
    """Expiration ties break toward the longer expiry -- measured on 1 400
    tastytrade openings, e.g. target 45 with {38, 52} listed picks 52."""

    def test_tie_breaks_long(self):
        ch = _chain_with([38, 52])
        leg = LegSpec(action="sell", right="P", dte=45, selector="delta", target=0.3)
        assert pick_expiry(ch, leg, {}).dte == 52

    def test_tie_breaks_short_when_asked(self):
        ch = _chain_with([38, 52])
        leg = LegSpec(action="sell", right="P", dte=45, selector="delta", target=0.3,
                      dte_tie="shorter")
        assert pick_expiry(ch, leg, {}).dte == 38

    def test_nearest_wins_over_tie_rule(self):
        ch = _chain_with([43, 52])
        leg = LegSpec(action="sell", right="P", dte=45, selector="delta", target=0.3)
        assert pick_expiry(ch, leg, {}).dte == 43

    def test_dte_window(self):
        ch = _chain_with([10, 30, 45, 60])
        leg = LegSpec(action="sell", right="P", dte=45, dte_min=25, dte_max=35,
                      selector="delta", target=0.3)
        assert pick_expiry(ch, leg, {}).dte == 30

    def test_strike_by_delta(self):
        ch = _chain_with([45])
        e = ch.sorted_expiries()[0]
        leg = LegSpec(action="sell", right="P", selector="delta", target=0.30, dte=45)
        got = pick_strike(ch, e, leg, {}, [])
        assert abs(abs(got.delta) - 0.30) <= 0.02

    def test_width_moves_away_from_the_money(self):
        ch = _chain_with([45])
        e = ch.sorted_expiries()[0]
        anchor = pick_strike(ch, e, LegSpec(action="sell", right="P", selector="strike",
                                            target=295.0, dte=45), {}, [])
        wing = pick_strike(ch, e, LegSpec(action="buy", right="P", selector="width",
                                          target=5, anchor="a", dte=45),
                           {"a": anchor}, [])
        assert wing.strike == pytest.approx(anchor.strike - 5)

    def test_moneyness_direction_depends_on_right(self):
        ch = _chain_with([45])
        e = ch.sorted_expiries()[0]
        p = pick_strike(ch, e, LegSpec(action="sell", right="P", selector="moneyness",
                                       target=0.05, dte=45), {}, [])
        assert p.strike < ch.spot


# ------------------------------------------------------------------- report
class _FakeDaily:
    def __init__(self, date, net_liq, drawdown=0.0):
        self.date, self.net_liq, self.drawdown = date, net_liq, drawdown
        self.total_pl = net_liq - 1000.0
        self.cash = net_liq
        self.open_pl = 0.0
        self.closed_pl = 0.0
        self.active = 0
        self.bp_used = 0.0
        self.spot = 300.0
        self.vix = 15.0


class _FakeRes:
    def __init__(self, strat, daily, trades):
        self.strategy, self.daily, self.trades = strat, daily, trades
        self.skipped = {}
        self.open_at_end = []


def _fake_run(n_days=420, n_trades=12):
    from backend.bt.portfolio import ClosedTrade
    strat = Strategy.from_dict({"name": "rep", "capital": 1000, "legs": [
        {"action": "sell", "right": "put", "dte": 45, "delta": 0.3}],
        "exits": {"dte": 21, "take_profit": 0.5}})
    d0 = D(2020, 1, 2)
    daily = [_FakeDaily(d0 + dt.timedelta(days=i), 1000 + i * 1.5,
                        -abs(math.sin(i / 30)) * 4) for i in range(n_days)]
    trades = [ClosedTrade(
        id=i + 1, opened=d0 + dt.timedelta(days=i * 7), closed=d0 + dt.timedelta(days=i * 7 + 5),
        contracts=1, open_price=-0.75, close_price=-0.33, premium=0.75,
        pnl=40.0 if i % 3 else -60.0, gross=42.0, fees=2.54,
        reason="take profit" if i % 3 else "stop loss",
        spot_open=300.0, spot_close=302.0, vix_open=15.0, vix_close=16.0,
        dte_open=45, dit=5, buying_power=500.0, legs="short 1 $300 put") for i in range(n_trades)]
    return _FakeRes(strat, daily, trades)


class TestReport:
    def test_builds_a_standalone_page(self, tmp_path):
        from backend.bt.report import build_report
        p = build_report(_fake_run(), str(tmp_path / "r.html"))
        doc = open(p, encoding="utf-8").read()
        assert doc.startswith("<!doctype html")
        assert "</html>" in doc
        # Self-contained: nothing may be fetched at view time.  The SVG/XHTML
        # namespace URIs are identifiers, not requests, so drop them first.
        probe = doc.replace("http://www.w3.org/2000/svg", "")
        for bad in ("http://", "https://", "src=", "@import", "<link"):
            assert bad not in probe, f"report references external resource: {bad}"

    def test_every_trade_is_in_the_log(self, tmp_path):
        from backend.bt.report import build_report
        run = _fake_run(n_trades=9)
        doc = open(build_report(run, str(tmp_path / "r.html")), encoding="utf-8").read()
        assert doc.count('<tr data-reason=') == 9

    def test_charts_are_rendered(self, tmp_path):
        from backend.bt.report import build_report
        doc = open(build_report(_fake_run(), str(tmp_path / "r.html")), encoding="utf-8").read()
        assert doc.count("<svg") >= 5           # equity, drawdown, heatmap, hist, reasons
        assert "viewBox" in doc

    def test_escapes_untrusted_strategy_text(self, tmp_path):
        from backend.bt.report import build_report
        run = _fake_run()
        run.strategy.name = '<img src=x onerror="alert(1)">'
        doc = open(build_report(run, str(tmp_path / "r.html")), encoding="utf-8").read()
        assert 'onerror="alert(1)"' not in doc
        assert "&lt;img" in doc

    def test_monthly_returns_chain_correctly(self):
        from backend.bt.report import monthly_returns
        daily = [_FakeDaily(D(2021, 1, 31), 100.0), _FakeDaily(D(2021, 2, 28), 110.0),
                 _FakeDaily(D(2021, 3, 31), 99.0), _FakeDaily(D(2021, 12, 31), 99.0)]
        m, y = monthly_returns(daily)
        assert m[(2021, 2)] == pytest.approx(10.0)
        assert m[(2021, 3)] == pytest.approx(-10.0)
        assert 2021 in y

    def test_index_and_manifest_accumulate(self, tmp_path):
        from backend.bt.report import build_index, build_report, update_manifest
        from backend.bt.stats import summarize
        out = str(tmp_path)
        a = _fake_run()
        build_report(a, os.path.join(out, "a.html"), title="A")
        rows = update_manifest(out, [("A", "a.html", summarize(a))])
        assert len(rows) == 1
        build_report(a, os.path.join(out, "b.html"), title="B")
        rows = update_manifest(out, [("B", "b.html", summarize(a))])
        assert len(rows) == 2                       # the first entry survives
        idx = open(build_index(rows, os.path.join(out, "index.html")), encoding="utf-8").read()
        assert 'href="a.html"' in idx and 'href="b.html"' in idx

    def test_manifest_drops_deleted_reports(self, tmp_path):
        from backend.bt.report import build_report, update_manifest
        from backend.bt.stats import summarize
        out = str(tmp_path)
        a = _fake_run()
        build_report(a, os.path.join(out, "a.html"), title="A")
        update_manifest(out, [("A", "a.html", summarize(a))])
        os.remove(os.path.join(out, "a.html"))
        assert update_manifest(out, []) == []

    def test_nice_ticks_are_round(self):
        from backend.bt.report import _nice_ticks
        t = _nice_ticks(0, 9700)
        assert all(abs(v / t[0] - round(v / t[0])) < 1e-9 for v in t if t[0])
        assert len(_nice_ticks(5.0, 5.0)) >= 1       # degenerate range must not hang


# -------------------------------------------------------------------- sweep
BASE_SPEC = {
    "name": "sw", "capital": 5000,
    "legs": [{"name": "short_put", "action": "sell", "right": "put", "dte": 45, "delta": 0.30},
             {"name": "long_put", "action": "buy", "right": "put", "dte": 45, "delta": 0.15}],
    "entry": {"frequency": "daily", "max_active": 1},
    "exits": {"dte": 21, "take_profit": 0.5},
}


class TestSweepPaths:
    def test_set_by_index(self):
        from backend.bt.sweep import set_path
        s = dict(BASE_SPEC, legs=[dict(x) for x in BASE_SPEC["legs"]])
        set_path(s, "legs[0].delta", 0.16)
        assert s["legs"][0]["delta"] == 0.16

    def test_set_by_leg_name(self):
        from backend.bt.sweep import set_path
        s = dict(BASE_SPEC, legs=[dict(x) for x in BASE_SPEC["legs"]])
        set_path(s, "legs.long_put.delta", 0.08)
        assert s["legs"][1]["delta"] == 0.08

    def test_set_nested(self):
        from backend.bt.sweep import set_path
        s = copy_spec()
        set_path(s, "exits.take_profit", 0.75)
        set_path(s, "entry.max_active", 3)
        assert s["exits"]["take_profit"] == 0.75 and s["entry"]["max_active"] == 3

    def test_unknown_key_names_the_alternatives(self):
        from backend.bt.sweep import SweepError, get_path
        with pytest.raises(SweepError) as e:
            get_path(copy_spec(), "legs[0].gamma")
        assert "delta" in str(e.value)          # tells the user what is available

    def test_unknown_leg_name(self):
        from backend.bt.sweep import SweepError, get_path
        with pytest.raises(SweepError) as e:
            get_path(copy_spec(), "legs.nope.delta")
        assert "short_put" in str(e.value)

    def test_index_out_of_range(self):
        from backend.bt.sweep import SweepError, get_path
        with pytest.raises(SweepError):
            get_path(copy_spec(), "legs[7].delta")

    def test_setting_an_absent_key_is_refused(self):
        """Silently inventing `exits.stop_loss` would sweep a rule the base
        strategy never had."""
        from backend.bt.sweep import SweepError, set_path
        with pytest.raises(SweepError):
            set_path(copy_spec(), "exits.stop_loss", 3.0)


def copy_spec():
    import copy as _c
    return _c.deepcopy(BASE_SPEC)


class TestSweepParams:
    def test_value_list(self):
        from backend.bt.sweep import parse_param
        assert parse_param("legs[0].delta=0.16,0.20,0.25") == \
            ("legs[0].delta", [0.16, 0.20, 0.25])

    def test_range(self):
        from backend.bt.sweep import parse_param
        assert parse_param("exits.dte=7:28:7") == ("exits.dte", [7, 14, 21, 28])

    def test_ints_stay_ints(self):
        from backend.bt.sweep import parse_param
        _, v = parse_param("entry.max_active=1,2,4")
        assert v == [1, 2, 4] and all(isinstance(x, int) for x in v)

    def test_rejects_missing_equals(self):
        from backend.bt.sweep import SweepError, parse_param
        with pytest.raises(SweepError):
            parse_param("legs[0].delta")

    def test_rejects_bad_range(self):
        from backend.bt.sweep import SweepError, parse_param
        with pytest.raises(SweepError):
            parse_param("exits.dte=7:28:0")


class TestSweepGrid:
    def test_cartesian_product(self):
        from backend.bt.sweep import Axis, expand
        combos = expand([Axis("legs[0].delta", [0.2, 0.3]),
                         Axis("exits.dte", [14, 21, 28])])
        assert len(combos) == 6
        assert {"legs[0].delta": 0.3, "exits.dte": 28} in combos

    def test_limit_is_enforced(self):
        from backend.bt.sweep import Axis, SweepError, expand
        with pytest.raises(SweepError):
            expand([Axis("a", list(range(30))), Axis("b", list(range(30)))], limit=400)

    def test_validate_rejects_bad_path_before_running(self):
        from backend.bt.sweep import Axis, SweepError, validate
        with pytest.raises(SweepError):
            validate(copy_spec(), [Axis("legs[0].nope", [1, 2])])

    def test_validate_rejects_type_change(self):
        from backend.bt.sweep import Axis, SweepError, validate
        with pytest.raises(SweepError):
            validate(copy_spec(), [Axis("entry.frequency", [1, 2])])

    def test_validate_accepts_int_for_float(self):
        from backend.bt.sweep import Axis, validate
        validate(copy_spec(), [Axis("exits.dte", [14, 21])])

    def test_apply_produces_a_compiled_strategy(self):
        from backend.bt.sweep import apply
        s = apply(copy_spec(), {"legs[0].delta": 0.16, "exits.take_profit": 0.75})
        assert s.legs[0].target == 0.16
        assert any(r.condition.source == "pnl_pct >= 0.75" for r in s.exits)

    def test_apply_does_not_mutate_the_base(self):
        from backend.bt.sweep import apply
        base = copy_spec()
        apply(base, {"legs[0].delta": 0.16})
        assert base["legs"][0]["delta"] == 0.30

    def test_best_and_grid(self):
        from backend.bt.sweep import Axis, Cell, SweepResult
        axes = [Axis("legs[0].delta", [0.2, 0.3])]
        cells = [Cell(0, {"legs[0].delta": 0.2}, {"cagr_pct": 4.0}),
                 Cell(1, {"legs[0].delta": 0.3}, {"cagr_pct": 9.0}),
                 Cell(2, {"legs[0].delta": 0.4}, {}, error="boom")]
        r = SweepResult("s", axes, cells, "cagr_pct")
        assert len(r.ok) == 2
        assert r.best().overrides["legs[0].delta"] == 0.3
        assert r.grid()[(0.2,)] == 4.0

    def test_failed_cell_does_not_sink_the_sweep(self):
        from backend.bt.sweep import Cell, SweepResult
        r = SweepResult("s", [], [Cell(0, {}, {}, error="x")], "cagr_pct")
        assert r.best() is None and r.ok == []


class TestSweepReport:
    def _result(self, n_axes=2):
        from backend.bt.sweep import Axis, Cell, SweepResult
        d0 = D(2020, 1, 1)
        if n_axes == 2:
            axes = [Axis("legs[0].delta", [0.2, 0.3]), Axis("exits.dte", [14, 21])]
            combos = [(0.2, 14), (0.2, 21), (0.3, 14), (0.3, 21)]
            cells = [Cell(i, {"legs[0].delta": a, "exits.dte": b},
                          {"cagr_pct": -2.0 + 3.0 * i, "sharpe": 0.5 + i * 0.1,
                           "max_drawdown_pct": -10.0, "trades": 30, "total_return_pct": 5.0 * i},
                          daily=[(d0 + dt.timedelta(days=k), 1000 + k + i) for k in range(60)])
                     for i, (a, b) in enumerate(combos)]
        else:
            axes = [Axis("exits.dte", [14, 21, 28])]
            cells = [Cell(i, {"exits.dte": v},
                          {"cagr_pct": 1.0 + i, "sharpe": 0.4, "max_drawdown_pct": -8.0,
                           "trades": 20, "total_return_pct": 3.0},
                          daily=[(d0 + dt.timedelta(days=k), 1000 + k) for k in range(60)])
                     for i, v in enumerate([14, 21, 28])]
        return SweepResult("sw", axes, cells, "cagr_pct")

    def test_two_axis_report(self, tmp_path):
        from backend.bt.report import build_sweep_report
        doc = open(build_sweep_report(self._result(2), str(tmp_path / "s.html")),
                   encoding="utf-8").read()
        assert doc.count("<svg") >= 2           # heatmap + equity comparison
        assert "bestring" in doc                # best cell is outlined
        assert doc.count("<tr>") >= 4

    def test_one_axis_report(self, tmp_path):
        from backend.bt.report import build_sweep_report
        doc = open(build_sweep_report(self._result(1), str(tmp_path / "s.html")),
                   encoding="utf-8").read()
        assert "<svg" in doc

    def test_report_states_the_in_sample_caveat(self, tmp_path):
        from backend.bt.report import build_sweep_report
        doc = open(build_sweep_report(self._result(2), str(tmp_path / "s.html")),
                   encoding="utf-8").read()
        assert "in sample" in doc.lower()

    def test_diverging_only_when_the_metric_crosses_zero(self):
        from backend.bt.report import _metric_scale, _metric_var
        assert _metric_scale([-2.0, 5.0])[2] is True
        assert _metric_scale([1.0, 5.0])[2] is False
        assert _metric_var(-1.0, -2.0, 5.0, True).startswith("--div-neg")
        assert _metric_var(4.0, 1.0, 5.0, False).startswith("--seq-")


# --------------------------------------------------------------- indicators
class _FakeMD:
    def __init__(self, prices):
        self.prices = prices

    def spot_series(self, dates, progress=None):
        return [self.prices[d] for d in dates]

    def day_range(self, d):
        p = self.prices[d]
        return p * 0.99, p * 1.01

    def flush(self):
        pass


class _FakeVix:
    def __init__(self, level=15.0):
        self.level = level

    def get(self, d):
        return self.level

    def flush(self):
        pass


def _ctx(values, vix=15.0):
    from backend.bt.context import MarketContext
    dates = [D(2020, 1, 1) + dt.timedelta(days=i) for i in range(len(values))]
    md = _FakeMD(dict(zip(dates, values)))
    return MarketContext(md, dates, _FakeVix(vix)), dates


class TestIndicators:
    def test_rsi_of_a_pure_uptrend_is_100(self):
        c, d = _ctx([100 + i for i in range(60)])
        assert c.at(d[-1])["rsi"](14) == pytest.approx(100.0)

    def test_rsi_of_a_pure_downtrend_is_zero(self):
        c, d = _ctx([200 - i for i in range(60)])
        assert c.at(d[-1])["rsi"](14) == pytest.approx(0.0, abs=1e-9)

    def test_rsi_stays_in_range(self):
        vals = [100 + 10 * math.sin(i / 3) + (i % 7) for i in range(200)]
        c, d = _ctx(vals)
        for day in d[30:]:
            r = c.at(day)["rsi"](14)
            if math.isfinite(r):
                assert 0.0 <= r <= 100.0

    def test_callable_rsi_matches_the_scalar(self):
        vals = [100 + 8 * math.sin(i / 5) for i in range(120)]
        c, d = _ctx(vals)
        env = c.at(d[-1])
        assert env["rsi"](14) == pytest.approx(env["rsi_14"])

    def test_any_lookback_is_available(self):
        vals = [100 + 8 * math.sin(i / 5) for i in range(120)]
        c, d = _ctx(vals)
        env = c.at(d[-1])
        assert math.isfinite(env["rsi"](2))
        assert env["rsi"](2) != env["rsi"](14)

    def test_sma_and_ret(self):
        c, d = _ctx([float(100 + i) for i in range(60)])
        env = c.at(d[-1])
        assert env["sma"](5) == pytest.approx((155 + 156 + 157 + 158 + 159) / 5)
        assert env["ret"](10) == pytest.approx(100.0 * (159 / 149 - 1.0))

    def test_high_low(self):
        vals = [100.0] * 30 + [120.0] + [100.0] * 10
        c, d = _ctx(vals)
        env = c.at(d[-1])
        assert env["high"](20) == pytest.approx(120.0)
        assert env["low"](20) == pytest.approx(100.0)

    def test_series_is_cached_per_period(self):
        c, _ = _ctx([100.0 + i for i in range(40)])
        a = c.series("rsi", 14)
        assert c.series("rsi", 14) is a
        assert c.series("rsi", 7) is not a

    def test_bad_period_is_rejected(self):
        c, _ = _ctx([100.0 + i for i in range(40)])
        with pytest.raises(ValueError):
            c.series("rsi", 0)


class TestIndicatorRules:
    def test_callable_indicator_compiles(self):
        r = Rule("rsi(2) < 10 and spot > sma(200)", allowed_names={"spot"})
        assert r.names == {"spot"}          # rsi/sma are functions, not variables

    def test_unknown_function_is_rejected_with_a_list(self):
        with pytest.raises(RuleError) as e:
            Rule("macd(12) < 0")
        assert "rsi" in str(e.value)        # tells the user what does exist

    def test_evaluates_against_a_context(self):
        r = Rule("rsi(2) < 30")
        assert r.test({"rsi": lambda n: 12.0})
        assert not r.test({"rsi": lambda n: 55.0})


# ------------------------------------------------------------ dynamic sizing
def _size_of(spec_sizing, env, base_legs=None):
    from backend.bt.engine import Backtester
    strat = Strategy.from_dict({
        "name": "s", "capital": 100000,
        "legs": base_legs or [{"action": "sell", "right": "put", "dte": 7, "delta": 0.2}],
        "sizing": spec_sizing})
    # The capital-based modes price a one-lot probe, so give them a real leg.
    structure = [(strat.legs[0], _c(1.00, 1.02, 300.0, "P"))]
    return Backtester._size(strat, env, -1.0, structure)


class TestScaledSizing:
    SCALE = {"mode": "fixed", "contracts": 2, "max_contracts": 8, "scale": [
        {"when": "rsi(2) <= 5 and vix >= 30", "factor": 4},
        {"when": "rsi(2) <= 10", "factor": 3},
        {"when": "rsi_14 <= 30", "factor": 2},
        {"when": "vix >= 28", "factor": 2},
        {"when": "rsi_14 >= 65", "contracts": 1},
    ]}

    def _env(self, rsi2=50.0, rsi14=50.0, vix=15.0):
        return {"rsi": lambda n: rsi2 if n == 2 else rsi14, "rsi_14": rsi14, "vix": vix,
                "net_liq": 100000.0}

    def test_base_size_when_nothing_matches(self):
        assert _size_of(self.SCALE, self._env()) == 2

    def test_first_match_wins(self):
        # rsi(2)=4 and vix=35 satisfies rules 1, 2 and 4; the first must win.
        assert _size_of(self.SCALE, self._env(rsi2=4.0, vix=35.0)) == 8

    def test_second_rule_when_the_first_misses(self):
        assert _size_of(self.SCALE, self._env(rsi2=8.0, vix=15.0)) == 6

    def test_absolute_contracts_rule(self):
        assert _size_of(self.SCALE, self._env(rsi14=70.0)) == 1

    def test_vix_rule_alone(self):
        assert _size_of(self.SCALE, self._env(vix=30.0)) == 4

    def test_max_contracts_clamps(self):
        s = dict(self.SCALE, contracts=4, max_contracts=6)
        assert _size_of(s, self._env(rsi2=4.0, vix=35.0)) == 6

    def test_min_contracts_floor(self):
        s = {"mode": "fixed", "contracts": 1, "min_contracts": 2,
             "scale": [{"when": "vix > 10", "factor": 0.1}]}
        assert _size_of(s, {"vix": 20.0, "net_liq": 1.0}) == 2

    def test_scaling_down_never_cancels_a_trade(self):
        """A factor rounding below one contract sizes down to one; deciding
        *whether* to trade is the entry rule's job."""
        s = {"mode": "fixed", "contracts": 2, "scale": [{"when": "vix > 10", "factor": 0.1}]}
        assert _size_of(s, {"vix": 20.0, "net_liq": 1.0}) == 1

    def test_explicit_zero_contracts_does_skip(self):
        s = {"mode": "fixed", "contracts": 2, "min_contracts": 0,
             "scale": [{"when": "vix > 40", "contracts": 0}]}
        assert _size_of(s, {"vix": 45.0, "net_liq": 1.0}) == 0
        assert _size_of(s, {"vix": 20.0, "net_liq": 1.0}) == 2

    def test_unaffordable_base_still_skips(self):
        s = {"mode": "bp_fraction", "fraction": 0.001,
             "scale": [{"when": "vix > 10", "factor": 5}]}
        assert _size_of(s, {"vix": 20.0, "net_liq": 100.0}) == 0

    def test_expression_mode(self):
        s = {"mode": "expression", "expression": "3 if vix >= 25 else 1", "max_contracts": 5}
        assert _size_of(s, {"vix": 30.0, "net_liq": 1.0}) == 3
        assert _size_of(s, {"vix": 12.0, "net_liq": 1.0}) == 1

    def test_expression_mode_inferred_from_the_key(self):
        s = {"expression": "2", "max_contracts": 5}
        assert _size_of(s, {"net_liq": 1.0}) == 2

    def test_scale_rule_needs_a_factor_or_contracts(self):
        with pytest.raises(SpecError):
            Strategy.from_dict({"name": "s", "legs": [
                {"action": "sell", "right": "put", "dte": 7, "delta": 0.2}],
                "sizing": {"scale": [{"when": "vix > 20"}]}})

    def test_scale_rule_rejects_both(self):
        with pytest.raises(SpecError):
            Strategy.from_dict({"name": "s", "legs": [
                {"action": "sell", "right": "put", "dte": 7, "delta": 0.2}],
                "sizing": {"scale": [{"when": "vix > 20", "factor": 2, "contracts": 3}]}})

    def test_scale_rule_validates_variable_names(self):
        with pytest.raises(Exception):
            Strategy.from_dict({"name": "s", "legs": [
                {"action": "sell", "right": "put", "dte": 7, "delta": 0.2}],
                "sizing": {"scale": [{"when": "nonsense > 20", "factor": 2}]}})


class TestDynamicMaxActive:
    def _strat(self, ma):
        return Strategy.from_dict({"name": "s", "legs": [
            {"action": "sell", "right": "put", "dte": 7, "delta": 0.2}],
            "entry": {"max_active": ma}})

    def test_static_value(self):
        from backend.bt.engine import Backtester
        assert Backtester._max_active(self._strat(3), {}) == 3

    def test_expression_widens_with_vix(self):
        from backend.bt.engine import Backtester
        s = self._strat("3 if vix >= 30 else (2 if vix >= 22 else 1)")
        assert Backtester._max_active(s, {"vix": 35.0}) == 3
        assert Backtester._max_active(s, {"vix": 25.0}) == 2
        assert Backtester._max_active(s, {"vix": 12.0}) == 1

    def test_unavailable_state_blocks_new_entries(self):
        from backend.bt.engine import Backtester
        s = self._strat("3 if vix >= 30 else 1")
        assert Backtester._max_active(s, {"vix": float("nan")}) == 1

    def test_describe_shows_the_expression(self):
        assert "vix >= 30" in self._strat("3 if vix >= 30 else 1").describe()


class TestParams:
    """Named constants exist so a threshold can be swept by path instead of
    being buried in an expression string."""

    SPEC = {"name": "p", "params": {"rsi_gate": 70, "oversold": 30},
            "legs": [{"action": "sell", "right": "put", "dte": 7, "delta": 0.2}],
            "entry": {"when": "rsi_14 < rsi_gate"},
            "exits": {"rules": [{"when": "rsi_14 <= oversold", "reason": "washed out"}]}}

    def test_params_are_usable_in_entry_and_exit_rules(self):
        s = Strategy.from_dict(self.SPEC)
        assert s.params["rsi_gate"] == 70
        assert s.entry.condition.test({"rsi_14": 50, "rsi_gate": 70})
        assert not s.entry.condition.test({"rsi_14": 80, "rsi_gate": 70})

    def test_params_are_usable_in_scale_rules(self):
        spec = dict(self.SPEC, sizing={"contracts": 1,
                                       "scale": [{"when": "rsi_14 <= oversold", "factor": 3}]})
        s = Strategy.from_dict(spec)
        assert s.sizing.scale[0].condition.test({"rsi_14": 20, "oversold": 30})

    def test_undeclared_name_still_fails(self):
        with pytest.raises(Exception):
            Strategy.from_dict(dict(self.SPEC, entry={"when": "rsi_14 < not_declared"}))

    def test_params_may_not_shadow_builtins(self):
        with pytest.raises(SpecError):
            Strategy.from_dict(dict(self.SPEC, params={"vix": 20}))
        with pytest.raises(SpecError):
            Strategy.from_dict(dict(self.SPEC, params={"rsi": 20}))

    def test_sweep_can_override_a_param(self):
        from backend.bt.sweep import apply, get_path
        assert get_path(self.SPEC, "params.rsi_gate") == 70
        s = apply(self.SPEC, {"params.rsi_gate": 55})
        assert s.params["rsi_gate"] == 55
        assert not s.entry.condition.test({"rsi_14": 60, "rsi_gate": 55})


if __name__ == "__main__":
    raise SystemExit(pytest.main([os.path.abspath(__file__), "-q"]))

