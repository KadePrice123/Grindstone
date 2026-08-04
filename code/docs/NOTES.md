# Pickup notes — backtest data + form (2026-08-03)

Working notes for whoever continues this (written for Kade picking up from
home). Everything below the "shipped" line is verified and pushed; the list
after it is the honest remainder.

## Shipped and verified

- **App-owned backtest store**: `data/backtest_data/<UNDERLYING>.db`, created
  automatically (first status call is enough), both engine tables (`opt` +
  `bars`) in one file. Source resolution per run: Settings path → workspace
  `spy_options.db` (SPY only) → the app's store. `backend/btdata.py` owns
  schema + sync; `data_status()` reports source/coverage honestly.
- **Recorder → store sync**: last in-market-hours (ET) snapshot per trading
  day from `rec_chain`, finest recorded timeframe from `rec_bars`.
  Incremental via `sync_state`. Runs on 'recorded' source auto-sync in the
  runner subprocess first; manual sync = POST `/api/backtests/data/sync`
  (background thread, progress in status). Synced rows deliberately OUTLIVE
  recorder retention — the store is the long-term archive, rec_* the inbox.
- **One-click wiring**: POST `/api/backtests/data/setup-recording` creates
  hourly-chain + daily-bars jobs (idempotent; not gated on universe sync —
  see comment in app.py).
- **Form editor**: `BacktestSpecForm.tsx` compiles to the same spec JSON the
  validator/AI path uses; `tryDecompile` refuses (→ JSON mode) rather than
  drop features. Gate: `SELFTEST OK 38/38`, incl. an end-to-end check that
  builds synthetic arbitrage-clean chains in rec_chain, syncs, and runs the
  REAL runner subprocess on the result.

## Not done yet — pick up here

1. **Multi-underlying UI**: the Data card and its two buttons are hardwired
   to SPY (`syncNow`/`setupRecording` in BacktestPage.tsx). Backend already
   takes `underlying` everywhere (status query param, BtDataIn, per-symbol
   store files); the page needs a small underlying picker that re-fetches
   status and passes it to both POSTs. Run-time resolution already follows
   the spec's `underlying`, so a TSLA spec + TSLA recordings works today —
   only the card's buttons don't know it.
2. **TastyTrade (and other) recording adapters**: the recorder is
   Alpaca-only. Any new provider that lands rows in `rec_chain`/`rec_bars`
   feeds backtests with ZERO further work — the sync doesn't care who wrote
   the rows. Same story for the hosted-data-API idea: write into the same
   two tables (or straight into `opt`/`bars` with the mapping in
   `btdata.py`'s module docstring).
3. **Recorded-data quality guards**: the sync takes the last in-hours
   snapshot as "the day". No holiday filter (a stale half-day quote set can
   become a thin trading day) and no minimum-contract-count sanity check per
   snapshot. Both belong in `btdata.sync_from_recorded`.
4. **Form coverage**: params, sizing scale ladders, expression max_active,
   raw exit-rule lists, calendars (`expiration_of`) are JSON-only — by
   design for now. The vocab endpoint (`/api/backtests/vocab`) is unused by
   the UI; autocomplete in the JSON editor would be the next friendliness
   win.
5. **Sweeps**: engine module is vendored and tested; no API/UI yet.
6. **Packaged build**: the runner needs its own entry point in the frozen
   exe (start() raises RunnerUnavailable → 501 today, see backtests.py).
7. **Timezone edge**: ET conversion falls back to fixed EST when tzdata is
   missing; `tzdata` is in requirements so venv installs are exact. The
   fallback mislabels one hour around DST — harmless for daily snapshots,
   worth removing once packaging bundles tzdata.

## Gotchas that will bite you if forgotten

- Sidecar children MUST get `stdin=DEVNULL` (watchdog pipe wedge, see
  backtests.py comment) and never a piped stderr nobody drains.
- Push failures with `curl 55` on this workspace's machines:
  `git config http.sslBackend openssl` (endpoint AV kills schannel uploads).
- Adding a selftest check = bump `checkpoint.json` expect string in the SAME
  commit (currently 38/38).
