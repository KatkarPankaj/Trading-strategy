# Phase 2A — Global Instrument Universe and Market Scanner

## Purpose and boundary

The scanner is a standalone candidate-discovery service:

```text
Instrument registry → universe selection → market/session gate
→ cheap data-quality and liquidity filters → deterministic ranking → Top-N
```

It has no dependency on AI, `RiskEngine`, `OrderManager`, brokers, or paper execution. A `PAPER` scan means paper-trading candidate generation only; it does not submit or stage an order. `LIVE` mode is not accepted. Candidate ranking is not a strategy signal, risk decision, profitability claim, or execution instruction.

## IMPLEMENTED

### Instrument registry and universe management

- `Instrument` remains the authoritative domain model. Optional metadata includes name, country, sector, industry, ISIN, FIGI, CUSIP, MIC, exchange/provider symbols, market capitalization, and active/tradable flags. Existing instruments remain compatible when these fields are absent.
- `InstrumentRepository` persists this metadata in the existing instrument payload; scanner run data uses the existing database and repository abstraction, with schema migration V6.
- A `UniverseDefinition` is validated data, never executable code. It supports markets, asset classes, explicit inclusion/exclusion IDs, active status, price/volume/turnover/volatility/market-cap thresholds, and sector filters.
- `InstrumentUniverseProvider` is the source boundary. `StaticUniverseProvider` supports injected definitions; `RegistryUniverseProvider` is the initial provider and exposes `<MARKET>_ALL` universes over instruments already registered in the database.
- API deployments may replace those default universes with a validated JSON array in `SCANNER_UNIVERSES`. Unknown fields, invalid asset classes, invalid thresholds, and executable expressions are rejected. It is not a vendor feed or a built-in global symbol master.

Example `SCANNER_UNIVERSES` JSON:

```json
[
  {
    "universe_id": "US_LIQUID_STOCKS",
    "name": "US liquid equities",
    "markets": ["US"],
    "asset_classes": ["EQUITY"],
    "minimum_price": 5,
    "minimum_average_volume": 500000,
    "minimum_turnover_by_currency": {"USD": 10000000},
    "maximum_volatility": 0.08
  },
  {
    "universe_id": "EUROPE_ETFS",
    "name": "European ETFs",
    "markets": ["DE"],
    "asset_classes": ["ETF"]
  }
]
```

### Markets, sessions, and modes

- Session decisions use `MarketRegistry`, `MarketDefinition`, and `TradingCalendar`; scanner code does not duplicate exchange hours or holiday rules.
- Scanner statuses are `OPEN`, `PRE_OPEN`, `POST_CLOSE`, `CLOSED`, `HOLIDAY`, or `UNKNOWN`. Unknown market or uncovered calendar data fails closed.
- `PAPER` candidate scans require a known configured session (`OPEN`, `PRE_OPEN`, or `POST_CLOSE`) and a fresh quote. `RESEARCH` uses only bars complete as of its timestamp and can operate outside an open session. An `as_of` later than the injected clock is rejected.
- Default market definitions include US, India, and Germany/Xetra. Calendar data coverage is separate from market representation: US and Xetra have calendar files for 2026; India and uncovered years report unknown session coverage and cannot pass a live-candidate gate until calendar data is supplied.

### Data quality, filters, ranking, and scale

- Uses the existing provider contract, `ResilientProvider`, and `validate_quote` / `validate_bars`; no second quality framework was introduced.
- Provider quote/bar errors, incomplete history, invalid values, stale/future quotes, and off-tick prices are recorded with quality and rejection information rather than converted to opportunities.
- Filters include active/tradable state, asset class, known market/session, history sufficiency, min/max price, average volume, native-currency turnover, volatility, optional sector, and optional market capitalization when available.
- Turnover and market-cap thresholds are keyed by currency so a configured USD limit is not silently reused as an EUR or INR limit. Price limits are applied in each instrument's native currency.
- Turnover is reference price multiplied by average volume in the instrument's native currency. No FX normalization is performed. Liquidity ranking is a within-currency percentile, not a comparison of USD, INR, and EUR amounts.
- Preliminary scoring is deterministic and weight-configurable (`SCANNER_SETTINGS`); it combines currency-relative liquidity, bounded recent momentum, volume expansion, volatility suitability, and data quality. Each filter result, score, reason, provider, quality, timestamp, age, and history length remains explainable.
- `max_concurrency`, `batch_size`, `top_n`, interval, lookback, and history requirements are validated in `SCANNER_SETTINGS`. Processing is bounded and batched in one process; one instrument/provider failure does not stop the remaining batch.
- Runs distinguish `COMPLETE`, `PARTIAL`, and `FAILED`; failed instruments and reasons are included in the run. A persistence error is surfaced as an API/CLI failure, not a successful scan.
- The synchronous scan response contains the accepted Top-N; all evaluated and failed candidate records are persisted and are retrievable with the paginated candidates endpoint. No raw vendor payload is stored in scanner domain objects.

Example `SCANNER_SETTINGS` JSON:

```json
{
  "max_concurrency": 8,
  "batch_size": 100,
  "top_n": 50,
  "minimum_history_bars": 20,
  "lookback_days": 90,
  "interval": "1d",
  "maximum_quote_age_seconds": 60
}
```

### API and CLI

Authenticated API endpoints:

- `GET /universes` and `GET /universes/{universe_id}`
- `POST /scanner/scan` with `universe_id`, `mode` (`RESEARCH` or `PAPER`), optional `top_n`, and optional timezone-aware `as_of`
- `GET /scanner/runs/{scan_id}`
- `GET /scanner/candidates?scan_id=...&accepted_only=...&limit=...&offset=...`

Manual CLI example:

```powershell
python -m stockmarket scan --universe US_ALL --mode RESEARCH --top 20
```

The command uses the existing configured database, instrument registry, market-data provider, and optional `SCANNER_UNIVERSES` / `SCANNER_SETTINGS`. The selected universe must already have registered instruments. Yahoo and mock data are research/scanning only, not production or execution feeds.

## Phase 2B hand-off

Phase 2B consumes selected Top-N candidates from persisted `RESEARCH` scanner runs. Migration V7 records the scanner run's `as_of` and selected-candidate membership; older runs without this point-in-time metadata are deliberately ineligible. Research uses a configured candidate limit in ranked order; `PAPER` scanner runs are not valid inputs. Research snapshots are a separate service and do not extend the scanner into AI analysis, strategy signals, risk approval, or order submission. See [MARKET_INTELLIGENCE.md](./MARKET_INTELLIGENCE.md) for its API/CLI and evidence contract.

## FUTURE

- A maintained, licensed global instrument master and provider-backed registry ingestion.
- Additional verified exchange calendars, including India and future-year coverage.
- Provider-specific halt detection, corporate-action normalization, verified average-volume definitions, and execution-grade source provenance.
- Cross-currency normalization only through an explicit timestamped FX service.
- Portfolio-aware candidate diversification, advanced ranking calibration, and scheduler integration.
- Load testing and provider-specific quota policy verification at thousands-instrument scale.

AI remains outside the scanner core. Candidate research is downstream in Phase 2B; AI opportunity ranking and strategy selection remain a later Phase 2C.
