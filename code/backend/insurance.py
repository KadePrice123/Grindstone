"""The expectancy engine behind the Insure page — puts priced as insurance.

A cash-secured put is a written insurance contract: the credit is premium
income, assignment is the claim, ``strike − settle`` at expiry is the claim
severity. This module MEASURES what that insurance has actually cost, per
risk class, from the archive (``options_history.db``) and the app's own
closes — so the page can put the measured cost of claims on the same axis as
the credit offered today and let the gap speak.

Everything here follows ``code/docs/INSURE.md`` (panel-settled 2026-08-12 —
do not relitigate its decisions here; the WHYs live there):

- ONE TRIAL is (entry day, contract): sell at that day's EOD quote, hold to
  expiry. Every archived entry day counts — the dishonesty of overlap is
  repaired in the SAMPLE SIZE (cluster means over expirations), never by
  discarding data.
- TWO LEDGERS: a zero-bid row still tells the truth about whether the strike
  was breached (claims ledger); only a sellable row has a P&L (priced ledger).
- THE ZERO-CLAIMS RULE: a class with no observed claims has an UNMEASURABLE
  pure premium, never a zero one — the rule-of-three ceiling is reported
  instead. A measured zero would paint the far wing as free money, the exact
  good-looking lie the archive was loaded to kill.

Pure stdlib, no numpy (the tripwire stands). Read-only over the archive
(mode=ro, opthist's own idiom); the only thing written anywhere is the
expectancy cache in market.db, which is app-owned and re-computable.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import sqlite3
import statistics
from typing import Any

#: Half-open bands, gate-pinned so a boundary value lands in exactly ONE
#: bucket in every future refactor. DTE 0-3 excluded by design (EOD granularity
#: misprices the 0-DTE business). Coarse on purpose: with ~52 weekly
#: expirations a year the n_exp >= 20 solid tier is reachable inside bands
#: this wide and unreachable in half-width ones.
DTE_BANDS: tuple[tuple[int, int], ...] = ((4, 10), (11, 21), (22, 38), (39, 60))
DELTA_BANDS: tuple[tuple[float, float], ...] = ((0.05, 0.15), (0.15, 0.25), (0.25, 0.35))
OTM_BANDS: tuple[tuple[float, float], ...] = (
    (0.01, 0.03), (0.03, 0.06), (0.06, 0.10), (0.10, 0.16))

#: Confidence tiers by distinct settled expirations. A dot IS a claim of
#: measurement; three expirations is not one.
SOLID_N = 20
THIN_N = 8

#: A day-over-day close jump this large inside a trial's life marks it suspect
#: (a split breaks strike/close comparability) — counted, excluded, reported.
SUSPECT_JUMP = 0.40


def wilson(k: float, n: int, z: float = 1.645) -> tuple[float, float]:
    """Wilson score interval, fractional-k tolerant (cluster means make k a
    sum of per-expiration claim fractions, not an integer count)."""
    if n <= 0:
        return (0.0, 1.0)
    p = max(0.0, min(1.0, k / n))
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    half = z * math.sqrt(max(0.0, p * (1 - p) / n + z * z / (4 * n * n)))
    return ((centre - half) / denom, (centre + half) / denom)


def class_of(dte: int, delta: float | None,
             otm_pct: float | None) -> tuple[str, tuple[int, int], tuple[float, float]] | None:
    """The risk class an entry belongs to: (mode, dte_band, band) or None.

    Delta is the UNDERWRITING VARIABLE when the archive recorded one; OTM% is
    the first-class fallback, never silently mixed (`mode` says which). Bands
    are half-open [lo, hi) — except DTE, whose integer bands are inclusive of
    both printed endpoints ([4,10] means 4..10), because "22-38 DTE" reads as
    calendar days, not an interval convention.
    """
    band_dte = next((b for b in DTE_BANDS if b[0] <= dte <= b[1]), None)
    if band_dte is None:
        return None
    if delta is not None and math.isfinite(delta):
        a = abs(delta)
        band = next((b for b in DELTA_BANDS if b[0] <= a < b[1]), None)
        return ("delta", band_dte, band) if band else None
    if otm_pct is not None and math.isfinite(otm_pct):
        band = next((b for b in OTM_BANDS if b[0] <= otm_pct < b[1]), None)
        return ("otm", band_dte, band) if band else None
    return None


def class_key(cls: tuple[str, tuple[int, int], tuple[float, float]]) -> str:
    """ONE serialisation of a class identity, used by the sweep's payload and
    the scan's lookup alike — two writers of this string is how a candidate
    would quietly stop finding its own measurement."""
    mode, dte_band, band = cls
    return f"{mode}|{dte_band[0]}-{dte_band[1]}|{band[0]}-{band[1]}"


def _weekday(iso: str) -> int:
    return dt.date.fromisoformat(iso).weekday()


def _settle_close(exp: str, closes: dict[str, float]) -> tuple[float, str] | None:
    """The close that settles an expiration, or None when it honestly cannot.

    On the date itself when it is a session we have. A NON-SESSION expiration
    (weekend by calendar; or a weekday hole whose neighbouring weekdays both
    have closes — an exchange holiday, not a data gap) settles on the last
    close within 3 calendar days BEFORE it: the prior session's close is the
    last price that contract ever saw. A weekday absent alongside a missing
    neighbour smells like a DATA GAP, and Thursday's close is a different
    contract outcome — refused, never approximated.
    """
    if exp in closes:
        return closes[exp], exp

    def prior(iso: str, days: int) -> str | None:
        d0 = dt.date.fromisoformat(iso)
        for i in range(1, days + 1):
            cand = (d0 - dt.timedelta(days=i)).isoformat()
            if cand in closes:
                return cand
        return None

    wd = _weekday(exp)
    non_session = wd >= 5
    if not non_session:
        # A holiday hole: both neighbouring WEEKDAYS present.
        d0 = dt.date.fromisoformat(exp)
        step = dt.timedelta(days=1)
        prev_wd = d0 - step
        while prev_wd.weekday() >= 5:
            prev_wd -= step
        next_wd = d0 + step
        while next_wd.weekday() >= 5:
            next_wd += step
        non_session = prev_wd.isoformat() in closes and next_wd.isoformat() in closes
    if not non_session:
        return None
    used = prior(exp, 3)
    return (closes[used], used) if used else None


def trials(rows: list[dict[str, Any]], closes: dict[str, float], *,
           today: str) -> list[dict[str, Any]]:
    """Per-(entry day, contract) trial records, each with a status.

    statuses: 'settled' | 'pending' | 'no-close' | 'no-spot' | 'suspect'.
    Only 'settled' feeds statistics; the rest are COUNTED — the tail is
    missing data, not a quiet trade.
    """
    # Suspect scan needs the close series in date order once, not per trial.
    dates = sorted(closes)
    jumps: list[str] = []  # dates where |close/prev - 1| > SUSPECT_JUMP
    for a, b in zip(dates, dates[1:]):
        ca, cb = closes[a], closes[b]
        if ca and cb and abs(cb / ca - 1.0) > SUSPECT_JUMP:
            jumps.append(b)

    dte_memo: dict[tuple[str, str], int] = {}
    out: list[dict[str, Any]] = []
    for r in rows:
        d, exp, k = r["date"], r["expiration"], r["strike"]
        key = (d, exp)
        dte = dte_memo.get(key)
        if dte is None:
            dte = (dt.date.fromisoformat(exp) - dt.date.fromisoformat(d)).days
            dte_memo[key] = dte
        if dte < DTE_BANDS[0][0] or dte > DTE_BANDS[-1][1]:
            continue
        spot = closes.get(d)
        otm = (spot - k) / spot if spot else None
        cls = class_of(dte, r.get("delta"), otm)
        if cls is None:
            continue
        t: dict[str, Any] = {
            "date": d, "expiration": exp, "strike": k, "dte": dte,
            "class": cls, "delta": r.get("delta"),
        }
        bid, ask = r.get("bid"), r.get("ask")
        two_sided = bid is not None and ask is not None and bid > 0 and ask >= bid
        t["credit_pct"] = ((bid + ask) / 2) / k if two_sided else None
        if spot is None:
            t["status"] = "no-spot"
            out.append(t)
            continue
        if exp > today:
            t["status"] = "pending"
            out.append(t)
            continue
        settle = _settle_close(exp, closes)
        if settle is None:
            t["status"] = "no-close"
            out.append(t)
            continue
        s_t, used_date = settle
        if any(d < j <= exp for j in jumps):
            t["status"] = "suspect"
            out.append(t)
            continue
        t["status"] = "settled"
        t["settle"] = s_t
        t["settle_date"] = used_date
        # A claim is settlement STRICTLY in the money; settle == strike wins.
        t["claim"] = 1 if s_t < k else 0
        t["sev_pct"] = max(0.0, k - s_t) / k
        out.append(t)
    return out


def _episodes(exp_claims: list[tuple[str, float]]) -> int:
    """Runs of consecutive claiming expirations, merged: 9 losing expirations
    that are one bad month report as one episode, so overlap cannot
    impersonate independence."""
    n = 0
    prev_claiming = False
    for _, claim in exp_claims:
        claiming = claim > 0
        if claiming and not prev_claiming:
            n += 1
        prev_claiming = claiming
    return n


def win_at(expiries: list[tuple[str, int, float, float]], offer_frac: float) -> float | None:
    """The win rate at TODAY'S credit against history's claims: the share of
    settled expirations whose mean severity came in under the offer."""
    if not expiries or offer_frac is None:
        return None
    return sum(1 for (_t, _n, _c, sev) in expiries if sev < offer_frac) / len(expiries)


def class_stats(all_trials: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Cluster aggregation: every statistic is a mean over EXPIRATIONS, one
    vote each — trials entered on 15 consecutive days into one expiration
    share ONE settlement draw, so entry days are opportunities, not samples.
    """
    by_class: dict[tuple, list[dict[str, Any]]] = {}
    for t in all_trials:
        by_class.setdefault(t["class"], []).append(t)

    out: dict[str, dict[str, Any]] = {}
    for cls, ts in by_class.items():
        mode, dte_band, band = cls
        settled = [t for t in ts if t["status"] == "settled"]
        censored = {
            "pending": sum(1 for t in ts if t["status"] == "pending"),
            "no_close": sum(1 for t in ts if t["status"] == "no-close"),
            "no_spot": sum(1 for t in ts if t["status"] == "no-spot"),
            "suspect": sum(1 for t in ts if t["status"] == "suspect"),
        }
        key = class_key(cls)
        if not settled:
            out[key] = {
                "mode": mode, "dte_band": list(dte_band), "band": list(band),
                "n_exp": 0, "n_days": 0, "censored": censored,
            }
            continue

        # ---- claims ledger: per-expiration cluster means -------------------
        by_exp: dict[str, list[dict[str, Any]]] = {}
        for t in settled:
            by_exp.setdefault(t["expiration"], []).append(t)
        expiries = []
        for exp in sorted(by_exp):
            group = by_exp[exp]
            claim_t = sum(t["claim"] for t in group) / len(group)
            sev_t = sum(t["sev_pct"] for t in group) / len(group)
            expiries.append((exp, len(group), claim_t, sev_t))
        n_exp = len(expiries)
        total_claim = sum(c for (_e, _n, c, _s) in expiries)
        claim_freq = total_claim / n_exp
        lo, hi = wilson(total_claim, n_exp)
        claiming = [(e, c) for (e, _n, c, _s) in expiries]
        episodes = _episodes(claiming)

        # THE ZERO-CLAIMS RULE. No observed claims does not measure a price of
        # zero — it measures nothing, and says so, with the rule-of-three
        # ceiling on what the true claim rate could still be.
        if total_claim == 0:
            expected_loss = None
            zero_reason = (f"no claims in {n_exp} expirations — one year "
                           f"cannot price this tail")
            rule3 = 3.0 / n_exp
        else:
            expected_loss = sum(s for (_e, _n, _c, s) in expiries) / n_exp
            zero_reason = None
            rule3 = None

        deltas = [abs(t["delta"]) for t in settled if t.get("delta") is not None]
        implied = (sum(deltas) / len(deltas)) if deltas else None

        # severity given a claim, over claiming expirations
        claim_sevs = [(e, s) for (e, _n, c, s) in expiries if c > 0]
        severity = None
        if claim_sevs:
            vals = sorted(s for (_e, s) in claim_sevs)
            worst_exp, worst = max(claim_sevs, key=lambda x: x[1])
            p95 = vals[min(len(vals) - 1, math.ceil(0.95 * len(vals)) - 1)]
            severity = {"mean": sum(vals) / len(vals), "p95": p95,
                        "worst": worst, "worst_date": worst_exp}

        # ---- priced ledger: only trials that could actually have been sold -
        priced = [t for t in settled if t["credit_pct"] is not None]
        win_rate = wl_ratio = None
        if priced:
            med_credit = statistics.median(t["credit_pct"] for t in priced)
            win_rate = sum(1 for (_e, _n, _c, s) in expiries if s < med_credit) / n_exp
            nets = [t["credit_pct"] - t["sev_pct"] for t in priced]
            wins = [x for x in nets if x > 0]
            losses = [-x for x in nets if x <= 0]
            if wins and losses:
                wl_ratio = (sum(wins) / len(wins)) / (sum(losses) / len(losses))

        out[key] = {
            "mode": mode, "dte_band": list(dte_band), "band": list(band),
            "n_exp": n_exp,
            "n_days": len(settled),
            "episodes": episodes,
            "claim_freq": claim_freq,
            "ci90": [lo, hi],
            "implied": implied,
            "expected_loss_pct": expected_loss,
            **({"zero_claims_reason": zero_reason, "rule_of_three": rule3}
               if zero_reason else {}),
            "severity": severity,
            "win_rate": win_rate,
            "wl_ratio": wl_ratio,
            "n_priced": len(priced),
            "window": {"first": min(t["date"] for t in settled),
                       "last": max(t["date"] for t in settled)},
            "censored": censored,
            # The per-expiration aggregates ride along so win_at(offer) is
            # computable at ANY offered credit later — a few hundred small
            # tuples, no trial rows retained.
            "expiries": [[e, n, c, s] for (e, n, c, s) in expiries],
        }
    return out


# --------------------------------------------------------------- the sweep

def sweep(con: sqlite3.Connection, underlying: str,
          closes: dict[str, float], *, today: str) -> dict[str, Any]:
    """One pass over an underlying's archived puts → class stats + honesty
    counters. The connection is a parameter so the gate hands it a fixture.
    """
    rows = [dict(r) for r in con.execute(
        "SELECT date, expiration, strike, bid, ask, delta FROM hist_chain"
        " WHERE underlying=? AND right='P'", (underlying.upper(),))]
    if not rows:
        return {"available": False,
                "reason": f"no archived chains for {underlying.upper()} — "
                          "record chains from Data management, or import"}
    ts = trials(rows, closes, today=today)

    # DTE-0 SELF-CHECK: on a contract's expiration-day row, mid must be near
    # intrinsic. Systematic disagreement means the closes and the chain do not
    # describe the same instrument — the scan says so rather than shipping
    # numbers built on it. (DTE-0 rows are outside every band, so this scans
    # the raw rows, not the trials.)
    checks = violations = 0
    for r in rows:
        if r["date"] != r["expiration"]:
            continue
        s = closes.get(r["date"])
        bid, ask = r["bid"], r["ask"]
        if s is None or bid is None or ask is None or bid <= 0 or ask < bid:
            continue
        checks += 1
        mid = (bid + ask) / 2
        intrinsic = max(0.0, r["strike"] - s)
        if abs(mid - intrinsic) > max(1.0, 0.02 * r["strike"]):
            violations += 1
    settle_suspect = checks >= 20 and violations > 0.05 * checks

    stats = class_stats(ts)
    return {
        "available": True,
        "classes": stats,
        "n_rows": len(rows),
        "n_trials": len(ts),
        "selfcheck": {"checks": checks, "violations": violations,
                      "suspect": settle_suspect},
        "window": {"first": min(r["date"] for r in rows),
                   "last": max(r["date"] for r in rows)},
    }


# ------------------------------------------------------------ cache plumbing

def fingerprint(hist_meta: dict[str, str], last_hist_date: str,
                last_close_date: str) -> str:
    """What must change for a recompute to be owed. The archive changes at
    most once a day; polling must never re-sweep on its own."""
    return f"{hist_meta.get('built_at', '')}|{last_hist_date}|{last_close_date}"


def cached_expectancy(market_con: sqlite3.Connection,
                      underlying: str) -> dict[str, Any] | None:
    row = market_con.execute(
        "SELECT fingerprint, computed_at, payload FROM insure_expectancy"
        " WHERE underlying=?", (underlying.upper(),)).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row["payload"])
    except ValueError:
        return None
    return {"fingerprint": row["fingerprint"],
            "computed_at": row["computed_at"], "payload": payload}


def store_expectancy(market_con: sqlite3.Connection, underlying: str,
                     fp: str, payload: dict[str, Any]) -> None:
    with market_con:
        market_con.execute(
            "INSERT INTO insure_expectancy (underlying, fingerprint, computed_at, payload)"
            " VALUES (?,?,?,?)"
            " ON CONFLICT(underlying) DO UPDATE SET fingerprint=excluded.fingerprint,"
            "  computed_at=excluded.computed_at, payload=excluded.payload",
            (underlying.upper(), fp,
             dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             json.dumps(payload)))


# ------------------------------------------------------- candidate matching

#: The scanner's anchors: one candidate per risk class per expiration — every
#: strike inside a class shares one measured expectancy, so more strikes add
#: dots, not information. "Show me every strike" is the Opt page, a click away.
DTE_ANCHORS = (7, 14, 30, 45)
DELTA_ANCHORS = (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35)
OTM_ANCHORS = (0.02, 0.04, 0.07, 0.10, 0.13, 0.16)


def pick_candidates(contracts: list[dict[str, Any]], spot: float,
                    today: str) -> list[dict[str, Any]]:
    """From one expiration's put rows: the priced OTM contract nearest each
    anchor, deduped one-per-class, each labeled with the matching it used."""
    puts = []
    for c in contracts:
        k = c.get("strike")
        if not k or k >= spot:
            continue  # OTM puts only — insurance, not stock replacement
        dte = (dt.date.fromisoformat(c["expiration"])
               - dt.date.fromisoformat(today)).days
        otm = (spot - k) / spot
        puts.append({**c, "dte": dte, "otm_pct": otm})
    if not puts:
        return []
    have_delta = [p for p in puts if p.get("delta") is not None]
    picked: dict[tuple, dict[str, Any]] = {}
    if have_delta:
        for anchor in DELTA_ANCHORS:
            best = min(have_delta, key=lambda p: abs(abs(p["delta"]) - anchor))
            cls = class_of(best["dte"], best["delta"], best["otm_pct"])
            if cls is None:
                continue
            cur = picked.get(cls)
            if cur is None or (abs(abs(best["delta"]) - anchor)
                               < abs(abs(cur["delta"]) - anchor)):
                picked[cls] = {**best, "class_mode": "delta"}
    else:
        for anchor in OTM_ANCHORS:
            best = min(puts, key=lambda p: abs(p["otm_pct"] - anchor))
            cls = class_of(best["dte"], None, best["otm_pct"])
            if cls is None:
                continue
            if cls not in picked:
                picked[cls] = {**best, "class_mode": "otm"}
    return list(picked.values())
