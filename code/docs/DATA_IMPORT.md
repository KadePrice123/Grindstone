# Importing your own data — file formats

Status: **built.** `backend/chainimport.py` parses these formats and enforces
every rule below; `backend/btdata.import_chain` writes the rows. Written before
the importer existed so it was built against a decided shape.

The data page accepts `.csv` and `.json` uploads and stores them in the app's
own database, alongside data the recorder collected. Two kinds are accepted:
**option chains** and **bars**. Nothing is sniffed — every upload declares which
kind it is, because a file whose kind was guessed wrong imports silently and is
discovered months later inside a backtest.

---

## 1. Option chains

One row per contract per observation. This is the format the vault's archive
already uses and the one `tools/fetchchains.py` writes, so an archive file is a
valid upload with no conversion.

### CSV

```csv
date,symbol,expiration,strike,type,bid,ask,last,volume,open_interest,implied_volatility,delta,gamma,theta,vega,rho
2026-08-05,SPY,2026-09-18,640.0,put,3.41,3.45,3.42,1204,8817,0.1412,-0.2731,0.0121,-0.0842,0.5613,-0.0914
2026-08-05,SPY,2026-09-18,645.0,put,4.78,4.83,4.80,932,5140,0.1455,-0.3402,0.0129,-0.0921,0.5988,-0.1147
2026-08-05,SPY,2026-09-18,640.0,call,9.12,9.19,9.15,2211,12043,0.1388,0.7269,0.0121,-0.0871,0.5613,0.1832
```

| Column | Required | Notes |
|---|---|---|
| `date` | **yes** | `YYYY-MM-DD`, the observation date — *not* the expiration |
| `symbol` | **yes** | underlying, e.g. `SPY` |
| `expiration` | **yes** | `YYYY-MM-DD` |
| `strike` | **yes** | decimal |
| `type` | **yes** | `call` or `put` (also accepts `C`/`P`) |
| `bid`, `ask` | **yes** | may be empty; see *missing values* below |
| `last`, `volume`, `open_interest` | no | |
| `mark` | no | Used when present. Not required — computed from bid/ask otherwise. |
| `implied_volatility` | no | decimal, `0.1412` — **not** `14.12` |
| `delta`, `gamma`, `theta`, `vega`, `rho` | no | omit the columns entirely if you don't have them |

Extra columns are ignored. Column order does not matter; the header names do.

### JSON

```json
{
  "kind": "option_chain",
  "source": "schwab-export-2026-08",
  "rows": [
    {
      "date": "2026-08-05", "symbol": "SPY", "expiration": "2026-09-18",
      "strike": 640.0, "type": "put",
      "bid": 3.41, "ask": 3.45, "last": 3.42,
      "volume": 1204, "open_interest": 8817,
      "implied_volatility": 0.1412,
      "delta": -0.2731, "gamma": 0.0121, "theta": -0.0842,
      "vega": 0.5613, "rho": -0.0914
    }
  ]
}
```

---

## 2. Bars

One row per symbol per period.

### CSV

```csv
symbol,timestamp,open,high,low,close,volume
SPY,2026-08-05T13:30:00Z,634.12,637.88,633.90,637.02,71204118
SPY,2026-08-04T13:30:00Z,631.55,635.20,630.87,634.09,66914203
```

| Column | Required | Notes |
|---|---|---|
| `symbol` | **yes** | |
| `timestamp` | **yes** | ISO 8601. **Include the timezone** — see below |
| `open`,`high`,`low`,`close` | **yes** | |
| `volume` | no | |

A date-only `timestamp` (`2026-08-05`) is accepted for daily bars and read as
that session's open in US market time.

### JSON

```json
{
  "kind": "bars",
  "symbol": "SPY",
  "timeframe": "1Day",
  "source": "my-broker-export",
  "rows": [
    { "timestamp": "2026-08-05T13:30:00Z", "open": 634.12, "high": 637.88,
      "low": 633.90, "close": 637.02, "volume": 71204118 }
  ]
}
```

`symbol` and `timeframe` on the envelope apply to every row, so they may be
omitted from the rows themselves. A row-level value wins where both appear.

---

## The rules the importer enforces

These exist because the alternative — importing something plausible — produces
a chart that is confidently wrong, which is worse than a refused upload.

**Timezones are not guessed.** A `timestamp` with no offset is ambiguous by
exactly the number of hours that turns a 09:30 open into a 04:30 one. Naive
timestamps are accepted only for daily bars, where the date is the identity.
Intraday rows without an offset are refused, naming the first offending row.

**Missing is not zero.** An empty `bid` means *not quoted* and is stored as
null. It must never become `0.0` — a zero bid is a real and different statement,
and a mid built on a fabricated zero is a lie the whole options surface would
inherit. Same for absent greeks: no delta means no delta, never `0`.

**Implied volatility is a decimal.** `0.1412`, not `14.12`. A file whose IV
column averages above 3.0 is refused with that reason rather than silently
storing percentages that make every model downstream wrong by 100×.

**Provenance is recorded and shown.** Every imported row carries its `source`
string and import timestamp, and anything drawn from it is labelled — the same
discipline as `alpaca (indicative)` on live chains. Imported data is never
presented as if it came from a live feed.

**Duplicates are replaced, not doubled.** The key is
`(symbol, date, expiration, strike, type)` for chains and
`(symbol, timeframe, timestamp)` for bars. Re-uploading a corrected file
overwrites those rows and leaves the rest alone.

**A file is all-or-nothing.** Validation runs over the whole file before a
single row is written, and a failure reports the row number and the reason. A
half-imported file is the worst outcome available: it looks like success and
leaves a gap nobody can see.
