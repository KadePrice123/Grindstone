"""What can this API key actually do? Answered with read-only calls only.

WHAT THIS CANNOT DO, stated first because the honest limit is the whole point:
**it cannot prove a key is unable to trade.** Alpaca does not scope plain key
pairs — there is no read-only variant of one — so no sequence of GETs proves
that negative. Anything claiming otherwise would be a guarantee resting on a
word the user typed into a dropdown.

What it CAN prove is the positive, which is the half that protects the user:
a key that authenticates against the LIVE trading host can place real orders.

Three verdicts, and the UI must repeat them in these terms:

  LIVE_CAPABLE  the live host authenticated — THIS KEY CAN PLACE REAL ORDERS
  PAPER_ONLY    only the paper host authenticated — it cannot move real money.
                NOT "read-only": it can still place paper orders and reset the
                paper account. Never print the words read-only for it.
  UNDETERMINED  rate limited, offline, or ambiguous. Never recorded as safe.

Fail closed everywhere: anything not positively established as PAPER_ONLY is
not PAPER_ONLY.

Split like `alpaca.py`: `verdict_of` is pure and decides everything, `probe`
merely gathers outcomes. That split is what lets the gate table-test every
combination with no network and no key.
"""
from __future__ import annotations

from typing import Any

import httpx

from .brokers.alpaca import LIVE_URL, PAPER_URL

LIVE_CAPABLE = "LIVE_CAPABLE"
PAPER_ONLY = "PAPER_ONLY"
UNDETERMINED = "UNDETERMINED"

#: One host's answer, reduced to what the verdict depends on.
OK = "ok"                  # 200 — these credentials are valid here
REJECTED = "rejected"      # 401/403 — valid request, wrong environment
UNCLEAR = "unclear"        # 429, 5xx, network error, non-JSON


def verdict_of(live: str, paper: str) -> str:
    """PURE. The entire decision, so it can be tested exhaustively offline."""
    if live == OK:
        # Authenticating against the live trading host is proof of capability,
        # and it stands whatever the paper host said. A key that answers on
        # both is still a key that can trade.
        return LIVE_CAPABLE
    if live == UNCLEAR:
        # The one answer that would have been decisive never arrived. Refusing
        # to conclude from the paper host alone is the fail-closed choice: a
        # live key behind a rate limit would otherwise read as PAPER_ONLY.
        return UNDETERMINED
    # live is REJECTED — these credentials are definitively not live-account
    # credentials.
    if paper == OK:
        return PAPER_ONLY
    return UNDETERMINED


def describe(verdict: str) -> str:
    """The sentence shown to the user. No euphemisms."""
    return {
        LIVE_CAPABLE: "This key can place real orders on your live account.",
        PAPER_ONLY: ("This key reaches your paper account only and cannot move "
                     "real money. It can still place paper orders."),
        UNDETERMINED: ("Could not establish what this key can do — it may have "
                       "been rate limited or the network was unavailable. "
                       "Treated as unsafe until it answers."),
    }.get(verdict, verdict)


def _ask(base: str, key_id: str, secret_key: str, timeout: float) -> str:
    """One read-only GET against one trading host. Never writes anything."""
    try:
        r = httpx.get(
            base + "/v2/account",
            headers={"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key},
            timeout=timeout)
    except httpx.HTTPError:
        return UNCLEAR
    if r.status_code == 200:
        return OK
    if r.status_code in (401, 403):
        return REJECTED
    return UNCLEAR


def probe(key_id: str, secret_key: str, timeout: float = 10.0) -> dict[str, Any]:
    """Ask both trading hosts and return the verdict plus what was observed.

    Two GETs, no writes — this module must stay inside the read-only guarantee
    the gate pins, so it must never gain a mutating HTTP call. (Spelling those
    method names out here would be matched by the very scan that enforces it.)
    """
    live = _ask(LIVE_URL, key_id, secret_key, timeout)
    paper = _ask(PAPER_URL, key_id, secret_key, timeout)
    v = verdict_of(live, paper)
    return {
        "verdict": v,
        "detail": describe(v),
        "live_host": live,
        "paper_host": paper,
        # A convenience for the UI, not a separate claim: only PAPER_ONLY is
        # eligible for unattended storage without an explicit override.
        "safe_to_store_unattended": v == PAPER_ONLY,
    }
