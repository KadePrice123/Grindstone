"""Read option-chain and bar files a user brings, or refuse them with a reason.

The format is specified in docs/DATA_IMPORT.md and was written before this
module existed, so the shape is decided rather than invented here.

Three rules run through everything below:

**Nothing is sniffed.** Every file declares its ``kind``. A chain file and a
bar file are both "a CSV with numbers in it", and a kind guessed wrong imports
silently and is discovered months later inside a backtest, by which time the
run that used it has already been believed.

**Missing is not zero.** An empty bid means *not quoted* and becomes ``None``.
It must never become ``0.0``: downstream, a zero bid is a real and different
statement — a market where somebody would pay nothing — and once stored the
two are indistinguishable forever. The same goes for absent greeks, where it
is not merely cosmetic: ``bt/data.py`` trusts any delta with ``|d| < 9.0``
verbatim and skips the model solve, so a fabricated ``0.0`` delta silently
replaces a computed one.

**All or nothing.** Every file is validated completely before a single row is
written. A half-imported file is the worst outcome available, because it looks
like success and leaves a gap nobody can see.

stdlib only, and deliberately no numpy: this is imported by the sidecar, and
selftest asserts numpy stays out of that process.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path

#: Files above this are refused rather than read into memory. A day of SPY is
#: ~1.8 MB; 512 MB is far past any legitimate single upload and stops a stray
#: multi-GB file from taking the sidecar down with a MemoryError.
MAX_BYTES = 512 * 1024 * 1024

#: An implied volatility is a decimal: 0.1412, not 14.12. A file that means
#: percent is wrong by 100x everywhere downstream and looks entirely
#: reasonable in a spreadsheet, so it is caught here or not at all. Real IV
#: does exceed 1.0 (a 300%-vol meme stock is 3.0), which is why the test is a
#: MEAN over the file rather than any single row.
IV_MEAN_CEILING = 3.0

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_OFFSET_RE = re.compile(r"(Z|[+-]\d{2}:?\d{2})$")


class Refused(Exception):
    """A file that will not be imported, and exactly why.

    Carries the offending line number where there is one, because "invalid
    file" sends a user back to a spreadsheet with no idea which of 14,000 rows
    to look at."""

    def __init__(self, reason: str, line: int | None = None):
        self.reason = reason
        self.line = line
        super().__init__(f"line {line}: {reason}" if line else reason)


@dataclass(frozen=True)
class ChainRow:
    date: str            # observation date, YYYY-MM-DD — NOT the expiration
    symbol: str
    expiration: str      # YYYY-MM-DD
    strike: float
    right: str           # 'C' | 'P'
    bid: float | None
    ask: float | None
    last: float | None
    mark: float | None
    volume: float | None
    open_interest: float | None
    iv: float | None
    delta: float | None


@dataclass(frozen=True)
class BarRow:
    symbol: str
    timeframe: str
    ts: str              # ISO 8601 WITH offset, or YYYY-MM-DD for daily
    open: float
    high: float
    low: float
    close: float
    volume: float | None


@dataclass
class Parsed:
    kind: str                       # 'option_chain' | 'bars'
    source: str                     # provenance, recorded on every row
    chain: list[ChainRow]
    bars: list[BarRow]

    @property
    def count(self) -> int:
        return len(self.chain) + len(self.bars)

    @property
    def symbols(self) -> list[str]:
        return sorted({r.symbol for r in self.chain} | {r.symbol for r in self.bars})


# --------------------------------------------------------------------- atoms
def _num(raw: str | float | int | None, line: int, col: str) -> float | None:
    """A number, or None for absent. Never a substituted zero."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return None if isinstance(raw, bool) else float(raw)
    s = raw.strip()
    if s == "" or s.lower() in ("na", "n/a", "null", "none", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        raise Refused(f"{col!r} is not a number: {s[:40]!r}", line) from None


def _req_num(raw, line: int, col: str) -> float:
    v = _num(raw, line, col)
    if v is None:
        raise Refused(f"{col!r} is required and was empty", line)
    return v


def _date(raw: str | None, line: int, col: str) -> str:
    s = (raw or "").strip()
    if not _DATE_RE.match(s):
        raise Refused(f"{col!r} must be YYYY-MM-DD, got {s[:40]!r}", line)
    try:
        dt.date.fromisoformat(s)
    except ValueError:
        raise Refused(f"{col!r} is not a real date: {s!r}", line) from None
    return s


def _right(raw: str | None, line: int) -> str:
    s = (raw or "").strip().lower()
    if s.startswith("c"):
        return "C"
    if s.startswith("p"):
        return "P"
    raise Refused(f"'type' must be call/put (or C/P), got {(raw or '')[:20]!r}", line)


def _timestamp(raw: str | None, line: int, timeframe: str) -> str:
    """ISO 8601, and the offset is NOT optional for intraday.

    A naive intraday timestamp is ambiguous by exactly the number of hours
    that turns a 09:30 open into a 04:30 one, and nothing later in the system
    can recover the intent. A date-only value is accepted for daily bars,
    where the date IS the identity."""
    s = (raw or "").strip()
    if not s:
        raise Refused("'timestamp' is required and was empty", line)
    if _DATE_RE.match(s):
        if timeframe and timeframe.lower() not in ("1day", "day", "1d", "daily"):
            raise Refused(
                f"date-only timestamp {s!r} is only accepted for daily bars, "
                f"and this file declares timeframe {timeframe!r}", line)
        return s
    body = s.replace(" ", "T", 1)
    if not _OFFSET_RE.search(body):
        raise Refused(
            f"timestamp {s[:32]!r} has no timezone. Add an offset (…Z or "
            f"+00:00): without one the same row means several different "
            f"moments and this importer will not choose between them", line)
    try:
        dt.datetime.fromisoformat(body.replace("Z", "+00:00"))
    except ValueError:
        raise Refused(f"timestamp {s[:32]!r} is not ISO 8601", line) from None
    return body


# ------------------------------------------------------------------ the rules
def _check_iv(rows: list[ChainRow]) -> None:
    ivs = [r.iv for r in rows if r.iv is not None]
    if not ivs:
        return
    mean = sum(ivs) / len(ivs)
    if mean > IV_MEAN_CEILING:
        raise Refused(
            f"implied_volatility averages {mean:.2f} across {len(ivs)} rows, "
            f"which reads as PERCENT. This importer wants a decimal — 0.1412, "
            f"not 14.12. Divide the column by 100 and re-upload; importing it "
            f"as-is would make every model downstream wrong by 100x")


def _check_dupes_chain(rows: list[ChainRow]) -> None:
    """Duplicates WITHIN one file are a mistake; duplicates ACROSS files are
    a correction. The store replaces on the same key, so a re-upload updates —
    but two different quotes for one contract in one file means the file was
    built wrong, and picking one of them silently is a choice nobody made."""
    seen: dict[tuple, int] = {}
    for i, r in enumerate(rows):
        key = (r.symbol, r.date, r.expiration, r.strike, r.right)
        if key in seen:
            raise Refused(
                f"{r.symbol} {r.date} {r.expiration} {r.strike:g}{r.right} appears "
                f"twice in this file (first at row {seen[key]}). One contract on "
                f"one date has one quote; two means the file was built wrong")
        seen[key] = i + 1


# ------------------------------------------------------------------- readers
_CHAIN_REQUIRED = ("date", "symbol", "expiration", "strike", "type", "bid", "ask")
_BARS_REQUIRED = ("symbol", "timestamp", "open", "high", "low", "close")


def _rows_from_csv(text: str, required: tuple[str, ...]) -> list[dict]:
    """Header-driven, never positional.

    The upstream archive reordered its columns around 2024-11. A positional
    parse would have swapped fields across that boundary and produced files
    that are perfectly well-formed and completely wrong — the one class of
    corruption a checksum cannot see."""
    rdr = csv.reader(io.StringIO(text))
    try:
        head = next(rdr)
    except StopIteration:
        raise Refused("the file is empty") from None
    names = [h.strip().lower() for h in head]
    missing = [c for c in required if c not in names]
    if missing:
        raise Refused(
            f"missing required column(s): {', '.join(missing)}. Found: "
            f"{', '.join(names[:12])}{'…' if len(names) > 12 else ''}")
    out = []
    for n, parts in enumerate(rdr, start=2):   # line 1 is the header
        if not any(p.strip() for p in parts):
            continue                            # a blank line is not a row
        row = {names[i]: parts[i] for i in range(min(len(names), len(parts)))}
        row["__line"] = n
        out.append(row)
    return out


def _chain_from_dicts(rows: list[dict], default_symbol: str = "") -> list[ChainRow]:
    out = []
    for r in rows:
        n = r.get("__line") or 0
        sym = str(r.get("symbol") or default_symbol).strip().upper()
        if not sym:
            raise Refused("'symbol' is required and was empty", n)
        out.append(ChainRow(
            date=_date(r.get("date"), n, "date"),
            symbol=sym,
            expiration=_date(r.get("expiration"), n, "expiration"),
            strike=_req_num(r.get("strike"), n, "strike"),
            right=_right(r.get("type"), n),
            bid=_num(r.get("bid"), n, "bid"),
            ask=_num(r.get("ask"), n, "ask"),
            last=_num(r.get("last"), n, "last"),
            # Not in the published column list, but the archive and the
            # OnclickMedia feed both carry it and the engine has a column for
            # it. Using a provided mark beats recomputing one.
            mark=_num(r.get("mark"), n, "mark"),
            volume=_num(r.get("volume"), n, "volume"),
            open_interest=_num(r.get("open_interest"), n, "open_interest"),
            iv=_num(r.get("implied_volatility"), n, "implied_volatility"),
            delta=_num(r.get("delta"), n, "delta"),
        ))
    return out


def _bars_from_dicts(rows: list[dict], default_symbol: str,
                     timeframe: str) -> list[BarRow]:
    out = []
    for r in rows:
        n = r.get("__line") or 0
        sym = str(r.get("symbol") or default_symbol).strip().upper()
        if not sym:
            raise Refused("'symbol' is required and was empty", n)
        tf = str(r.get("timeframe") or timeframe or "1Day")
        out.append(BarRow(
            symbol=sym,
            timeframe=tf,
            ts=_timestamp(r.get("timestamp"), n, tf),
            open=_req_num(r.get("open"), n, "open"),
            high=_req_num(r.get("high"), n, "high"),
            low=_req_num(r.get("low"), n, "low"),
            close=_req_num(r.get("close"), n, "close"),
            volume=_num(r.get("volume"), n, "volume"),
        ))
    return out


# ---------------------------------------------------------------- the door
def parse_text(text: str, kind: str, fmt: str, source: str) -> Parsed:
    """Parse an already-read file body. `kind` is declared, never inferred."""
    if kind not in ("option_chain", "bars"):
        raise Refused(
            f"unknown kind {kind!r} — say 'option_chain' or 'bars'. This is "
            f"not guessed: a chain file and a bar file are both 'a CSV with "
            f"numbers in it', and a wrong guess is found months later inside "
            f"a backtest")
    if fmt == "json":
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as e:
            raise Refused(f"not valid JSON: {e.msg}", e.lineno) from None
        if not isinstance(obj, dict):
            raise Refused("JSON must be an object with 'kind' and 'rows'")
        declared = obj.get("kind")
        if declared and declared != kind:
            raise Refused(
                f"the file declares kind {declared!r} but it is being imported "
                f"as {kind!r}. Refusing rather than picking one")
        raw = obj.get("rows")
        if not isinstance(raw, list):
            raise Refused("JSON has no 'rows' array")
        dicts = [dict(r, __line=i + 1) for i, r in enumerate(raw)
                 if isinstance(r, dict)]
        if len(dicts) != len(raw):
            raise Refused("every entry in 'rows' must be an object")
        env_symbol = str(obj.get("symbol") or "").strip().upper()
        env_tf = str(obj.get("timeframe") or "").strip()
        source = source or str(obj.get("source") or "")
        required = _CHAIN_REQUIRED if kind == "option_chain" else _BARS_REQUIRED
        for d in dicts:
            for c in required:
                if c == "symbol" and env_symbol:
                    continue
                if c not in d:
                    raise Refused(f"missing required field {c!r}", d["__line"])
    else:
        required = _CHAIN_REQUIRED if kind == "option_chain" else _BARS_REQUIRED
        # The envelope's symbol/timeframe only exist in JSON; a CSV must carry
        # them per row, so a missing 'symbol' column is a hard refusal there.
        dicts = _rows_from_csv(text, required)
        env_symbol, env_tf = "", ""

    if not dicts:
        raise Refused("the file has a header but no rows")

    if kind == "option_chain":
        chain = _chain_from_dicts(dicts, env_symbol)
        _check_iv(chain)
        _check_dupes_chain(chain)
        return Parsed("option_chain", source, chain, [])
    bars = _bars_from_dicts(dicts, env_symbol, env_tf)
    return Parsed("bars", source, [], bars)


def read_path(path: str | Path, kind: str, source: str = "") -> Parsed:
    """Read a file off disk. The extension picks CSV vs JSON; `.gz` is
    transparently decompressed so an archive day file imports unchanged."""
    p = Path(path)
    if not p.is_file():
        raise Refused(f"no such file: {p}")
    size = p.stat().st_size
    if size > MAX_BYTES:
        raise Refused(
            f"{p.name} is {size / 1e6:.0f} MB, over the {MAX_BYTES / 1e6:.0f} MB "
            f"limit for a single upload")
    if size == 0:
        raise Refused(f"{p.name} is empty")

    name = p.name.lower()
    if name.endswith(".gz"):
        import gzip
        raw = gzip.decompress(p.read_bytes())
        name = name[:-3]
    else:
        raw = p.read_bytes()
    text = raw.decode("utf-8-sig", errors="replace")
    fmt = "json" if name.endswith(".json") else "csv"
    return parse_text(text, kind, fmt, source or f"upload:{p.name}")
