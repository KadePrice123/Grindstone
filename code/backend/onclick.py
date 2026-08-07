"""The OnclickMedia chain puller: slow, off by default, and honest about limits.

Free and keyless — no credentials have ever existed for this. What it does have
is a **rolling window**: measured 2026-08-06, the earliest date it will serve is
exactly 182 days back, and anything older returns
``403 {"error":"Requested date is outside your plan's data range"}``.

That boundary MOVES, one day per day. A note recorded on 2026-07-30 read the
same 403 as a permanent paywall dated 2026-01-29 — which is 182 days before
*that* day. Nothing changed upstream; a sliding window was mistaken for a
policy. So the window is computed here, never remembered, and the UI clamps to
it rather than letting a user queue years that can only 403.

Pace: one ticker-day per 5-10 seconds, and the default is OFF. The archive this
extends is irreplaceable — nothing older than the window can be re-fetched at
any price — which is a reason to be a good guest.

The parsing half is deliberately separable from the network half so the gate
can feed it canned bodies and assert the guard without touching the wire.
"""
from __future__ import annotations

import datetime as dt
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.onclickmedia.com/options/"

#: The archive's own header, character for character. Ported from
#: tools/fetchchains.py, which lives OUTSIDE this repo — it is not installed,
#: not gated, and not shipped, so importing it would be a dependency users do
#: not have. Copied rather than referenced, and the gate pins the copy.
HEADER = (
    "date,symbol,expiration,strike,type,last,mark,bid,bid_size,ask,ask_size,"
    "change,percent_change,volume,open_interest,implied_volatility,"
    "delta,gamma,theta,vega,rho"
)

#: Returned when the columns are not the archive's columns.
MISMATCH = "\x00MISMATCH"

#: Free-plan reach, in days. Measured by binary search, not documented
#: anywhere upstream. Kept slightly inside the real edge so a request is
#: refused locally rather than spending a round trip to be told 403.
WINDOW_DAYS = 180

MIN_DELAY = 5.0
MAX_DELAY = 10.0


def window(today: dt.date | None = None) -> tuple[dt.date, dt.date]:
    """The (earliest, latest) dates worth asking for.

    The latest is YESTERDAY, not today: an open session returns a SHORTER
    header with no greeks, because they are only populated at the close. That
    is the second, independent guard against absorbing a greek-less day — the
    header check being the first."""
    t = today or dt.date.today()
    return t - dt.timedelta(days=WINDOW_DAYS), t - dt.timedelta(days=1)


def clamp(start: dt.date, end: dt.date,
          today: dt.date | None = None) -> tuple[dt.date, dt.date, str]:
    """A requested range trimmed to what can actually be served, plus a note
    saying what was trimmed. Silence here would look like the puller simply
    finding no data for two thirds of the request."""
    lo, hi = window(today)
    s, e = max(start, lo), min(end, hi)
    notes = []
    if start < lo:
        notes.append(
            f"{start} is outside the free plan's rolling {WINDOW_DAYS}-day "
            f"window; starting at {lo} instead")
    if end > hi:
        notes.append(f"ending at {hi} — today's session has no greeks yet")
    return s, e, " · ".join(notes)


def trading_days(start: dt.date, end: dt.date) -> list[dt.date]:
    """Weekdays in the range. Holidays are not known here; the upstream simply
    returns nothing for them, which the caller records as 'no data' rather than
    as a failure."""
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def check_body(body: str) -> str | None:
    """The guard, with no network in it.

    None  -> the upstream has no such day (empty body).
    MISMATCH -> the columns are not the archive's. The live-session case is the
                one that actually occurs: today's rows stop at
                implied_volatility with no greeks. Absorbing that would put
                greek-less rows into a history nothing can rebuild.
    otherwise -> the body, unchanged.
    """
    body = (body or "").strip()
    if not body:
        return None
    head = body.splitlines()[0].strip()
    if head != HEADER:
        return MISMATCH
    return body


def fetch_day(sym: str, day: str, timeout: float = 60.0, retries: int = 2) -> str | None:
    """One day's chain as CSV text.

    A transient failure RAISES rather than returning None. Recording a network
    fault as "no data" would leave a permanent hole indistinguishable from a
    market holiday, and the next run would skip it forever.
    """
    url = API + "?" + urllib.parse.urlencode({"ticker": sym, "date": day})
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "grindstone-chain-puller/1"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode("utf-8", errors="replace")
            return check_body(body)
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                return None                      # not carried / not a session
            if e.code == 403:
                # PERMANENT for this date, not transient. Retrying it wastes
                # the user's pacing budget on an answer that cannot change.
                raise PermissionError(
                    f"{day} is outside the free plan's data range") from None
            last = e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{sym} {day}: {type(last).__name__}: {last}")
