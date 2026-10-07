# Phase 2B — Candidate Research Snapshots

## Purpose and boundaries

Candidate research is a separate advisory service downstream of the global scanner. It creates an auditable, point-in-time research snapshot for accepted candidates from a persisted `RESEARCH` scanner run.

```text
Persisted RESEARCH scan → ranked accepted candidates
    → quality-checked historical bars → technical evidence + market regime
    → optional source-attributed news / fundamentals / sector facts
    → timestamped ResearchSnapshot + durable ResearchRun
```

It does **not** invoke AI, choose strategies, create deterministic strategy signals, evaluate risk, size positions, stage proposals, submit orders, or call the paper executor. The existing market-intelligence `TradeProposal` and paper submission APIs remain separate. RiskEngine remains the final pre-order gate in that downstream workflow.

## Snapshot contract

Each snapshot is tied to an instrument, a scanner run, and a timezone-aware `as_of`. It includes:

- Deterministic log-return momentum, fast/slow moving-average trend, and relative-volume scores, with the input window, bar interval, measurements, and market-data source.
- The existing `MarketRegimeEvaluator` assessment: label, directional score, volatility, and start/end timestamps.
- Raw news records with publication time, source, headline, source/provider identity, event identity, age, and source-reported classifications. The service does not invent sentiment or ask an AI model to interpret the headline.
- Source-provided fundamental and sector observations when configured, including subject, content, source, reference, observed time, and retrieval time.
- Explicit component state (`AVAILABLE`, `MISSING`, `UNAVAILABLE`, or `REJECTED`), quality, warnings, and provenance. Macro and sentiment remain `MISSING` until dedicated providers are wired.
- A tuple of existing `ResearchEvidence` scores for deterministic components. These can become input to a later AI phase but are not a signal or trade authority.

`as_of` and observation timestamps must be timezone-aware. Observations after `as_of` are rejected, and the research timestamp cannot predate the scanner run timestamp. Migration V7 stores scanner `as_of` and selected Top-N membership; older scanner runs without that timestamp are rejected as research inputs. Historical news is withheld unless an archive-capable adapter is added; current Finnhub calls are not historical retrieval proof. Likewise, current Yahoo earnings and NSE sector adapters do not declare point-in-time archive support, so their facts are withheld for historical queries. Historical OHLCV can still carry provider restatement/corporate-action risk; it is not an immutable point-in-time data archive.

## API

All endpoints require the configured bearer token:

- `POST /research/candidates` accepts `{ "scan_id": "...", "limit": 20, "as_of": "2026-10-08T20:00:00Z" }`. `as_of` may be omitted to use the service clock, but cannot predate the scanner run's persisted `as_of`. Only selected Top-N candidates from a persisted `RESEARCH` scanner run are eligible; `PAPER` runs are rejected.
- `GET /research/runs/{run_id}` returns the durable run and its snapshots.
- `GET /research/snapshots/{snapshot_id}` returns the snapshot and its individually persisted evidence records.

Responses explicitly set `research_only: true`, `execution: "NOT_SUBMITTED"`, and `risk_status: "NOT_EVALUATED"`. Run/snapshot status is `COMPLETE`, `PARTIAL`, or `FAILED`. Missing optional sources are not assigned neutral scores. Because macro and sentiment do not yet have providers, runs will normally be `PARTIAL`.

## CLI

```powershell
python -m stockmarket research --scan-id <research-scan-id> --limit 20
python -m stockmarket research --scan-id <research-scan-id> --as-of 2026-10-08T20:00:00Z
python -m stockmarket research-show --run-id <research-run-id>
python -m stockmarket research-show --snapshot-id <snapshot-id>
```

The command uses the configured database, instrument registry, and market-data provider. Optional news/fundamental/sector adapters are currently wired in the API bootstrap; the CLI run uses the available market-data source and reports other components as missing. CLI output is JSON suitable for manual inspection. Exit code `0` means all requested snapshots completed with all components available; `2` indicates partial or failed coverage.

`CANDIDATE_RESEARCH_SETTINGS` accepts a JSON object with these optional fields:

```json
{
  "interval": "1d",
  "lookback_days": 120,
  "regime_lookback_bars": 20,
  "regime_max_bar_age_seconds": 432000,
  "fundamental_max_age_seconds": 7776000,
  "sector_max_age_seconds": 86400,
  "news_max_age_seconds": 259200,
  "news_current_window_seconds": 300,
  "max_concurrency": 4,
  "max_candidates": 50,
  "cache_ttl_seconds": 30,
  "cache_capacity": 256
}
```

Values are validated and bounded. The in-process cache is keyed by instrument, exact `as_of`, provider identities, and settings; it is bounded by both TTL and capacity. Persisted runs and evidence are not served from the process cache.

## Phase readiness

| Area | Status | Evidence / boundary |
|---|---|---|
| Candidate research snapshot | IMPLEMENTED | Deterministic technical evidence, existing regime evaluation, optional raw news and timestamped source observations. |
| Point-in-time controls | PARTIAL | Aware timestamps, future-observation rejection, historical provider restrictions, and explicit provenance; historical OHLCV revisions and archive lineage are not solved. |
| Evidence quality | PARTIAL | Component availability/failure and evidence quality are explicit; no macro/sentiment feed or independent source-quality calibration exists. |
| Persistence and inspection | IMPLEMENTED | Migration V7 persists research runs, snapshots, and component evidence; authenticated API and CLI expose manual inspection. |
| AI input/output and opportunity ranking | NOT IMPLEMENTED IN PHASE 2B | Phase 2C should define a strict AI contract over snapshots, rank opportunities, and recommend only registered strategies. |
| Execution and risk | UNCHANGED | No signals or orders are produced here. Downstream paper acceptance still requires explicit action and RiskEngine evaluation. |

## Validation and future work

Focused tests cover persistence and API inspection, `as_of` rejection, paper-scan rejection, source-attributed news, historical-news suppression, and future evidence rejection. Phase 2C must add its own schema, prompt/model provenance, deterministic ranking, unavailable-AI behavior, and tests; it must not bypass strategy validation or RiskEngine.

Historical provider revisions, point-in-time fundamentals and news archives, macro/sentiment providers, source licensing, cache/load behavior at broad-universe scale, and independent calibration remain open work. Nothing in this phase establishes profitability, live readiness, or production suitability.
