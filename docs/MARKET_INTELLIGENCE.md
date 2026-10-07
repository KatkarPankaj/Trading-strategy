# Phase 2B — Candidate Research Snapshots

## Purpose and boundaries

Candidate research is a separate advisory service downstream of the global scanner. It creates an auditable, point-in-time research snapshot for accepted candidates from a persisted `RESEARCH` scanner run.

```text
Persisted RESEARCH scan → ranked accepted candidates
    → quality-checked historical bars → technical evidence + market regime
    → optional source-attributed news / fundamentals / sector facts
    → timestamped ResearchSnapshot + durable ResearchRun
```

Snapshot creation does **not** invoke AI, choose strategies, create deterministic strategy signals, evaluate risk, size positions, stage proposals, submit orders, or call the paper executor. The separate, manually triggered Phase 2B-F assessment below reads only persisted snapshots and stops at `STRATEGY_SELECTED` or `REJECTED`. The existing market-intelligence `TradeProposal` and paper submission APIs remain separate. RiskEngine remains the final pre-order gate in that downstream workflow.

## Snapshot contract

Each snapshot is tied to an instrument, a scanner run, and a timezone-aware `as_of`. It includes:

- Deterministic log-return momentum, fast/slow moving-average trend, and relative-volume scores, with the input window, bar interval, measurements, and market-data source.
- The existing `MarketRegimeEvaluator` assessment: label, directional score, volatility, and start/end timestamps.
- Raw news records with publication time, source, headline, source/provider identity, event identity, age, and source-reported classifications. The service does not invent sentiment or ask an AI model to interpret the headline.
- Source-provided fundamental and sector observations when configured, including subject, content, source, reference, observed time, and retrieval time.
- Explicit component state (`AVAILABLE`, `MISSING`, `UNAVAILABLE`, or `REJECTED`), quality, warnings, and provenance. Macro and sentiment remain `MISSING` until dedicated providers are wired.
- A tuple of existing `ResearchEvidence` scores for deterministic components. These can become input to a later AI phase but are not a signal or trade authority.

`as_of` and observation timestamps must be timezone-aware. Observations after `as_of` are rejected, and the research timestamp cannot predate the scanner run timestamp. Migration V7 stores scanner `as_of` and selected Top-N membership; older scanner runs without that timestamp are rejected as research inputs. Historical news is withheld unless an archive-capable adapter is added; current Finnhub calls are not historical retrieval proof. Likewise, current Yahoo earnings and NSE sector adapters do not declare point-in-time archive support, so their facts are withheld for historical queries. Historical OHLCV can still carry provider restatement/corporate-action risk; it is not an immutable point-in-time data archive.

## Phase 2B-F: AI assessment and deterministic ranking

`CandidateAssessmentService` accepts a persisted snapshot ID only. It constructs a bounded context from the stored snapshot, its source-attributed evidence, the registered instrument/market definitions and the market-calendar phase at the snapshot's `as_of`. It does not call market-data, news, web or other fact providers. It rejects inconsistent identities, non-aware or future timestamps, and future-observed technical/regime scores before invoking AI. Missing, unavailable and rejected components remain distinct; AI cannot override absent critical technical or regime evidence.

The strict `candidate_assessment` schema requires snapshot/instrument identity, directional bias (including `INSUFFICIENT_EVIDENCE`), confidence, an advisory 0–100 AI score, risk flags, cited evidence IDs, invalidating conditions and an explanation. Citation IDs must belong to the persisted snapshot. Model/provider errors, invalid output, insufficient critical evidence and invalid strategy selections are recorded as explicit rejected states. The service then reuses `AIAnalyst.select_strategies()` against the already registered deterministic strategy catalog; an unknown or duplicate strategy cannot be selected.

The final opportunity score is deterministic and bounded to 0–100: evidence completeness 20%, data quality 15%, technical alignment 20%, regime compatibility 15%, AI opportunity score 15%, AI-reported confidence 5%, and registered-strategy availability 10%; each AI risk flag subtracts 5 points up to a 25-point cap. The component contributions and penalty are persisted in the explanation. This is an advisory ranking, not a probability, forecast, trade recommendation, risk approval or profitability claim.

Successful/rejected assessment records persist the complete input context, assessment time, snapshot link, provider, configured model name (`AI_MODEL`, or `unspecified`), prompt/schema versions, prompt/response hashes, registered strategy names and implementation/version metadata, strategy validation, lifecycle and ranking. The current ORB/VWAP implementation declares version `1.0.0`; older persisted assessments with an unspecified version are not silently upgraded for signal generation. Migration V8 adds the assessment/opportunity records. The successful lifecycle ends at `STRATEGY_SELECTED`; failures end at `REJECTED`. Neither state creates a signal, a `TradeProposal`, a risk decision, an order, a broker call or an execution event. Phase 2C begins after this boundary.

## Phase 2C-1: autonomous PAPER opportunity orchestration

`AutonomousResearchService` composes the persisted scanner, candidate-research and assessment services into a bounded manually triggered run. It always invokes the scanner in `RESEARCH` mode; `LIVE` is rejected. The service filters the requested universe to configured market and asset-class allowlists, rejects uncovered market calendars and non-trading dates, and passes an explicit maximum concurrency and Top-N cap. Assessment is restricted to the configured registered-strategy allowlist. It does not call `Strategy.evaluate()`, create a signal or `TradeProposal`, or call sizing, risk, order, broker, executor or portfolio services.

Migration V9 persists an idempotency key, canonical request hash, run stage and timestamps, plus per-instrument snapshot/opportunity outcomes. Scanner and research stage IDs are deterministic from the autonomous run ID. Repeating a completed key returns the stored result; a different request with the same key is rejected. Interrupted runs reuse persisted scan/research snapshots and previously stored assessments when their as-of and strategy catalog match. Candidate assessment errors are isolated and persisted as `FAILED`, allowing later retry; the run otherwise stops at `STRATEGY_SELECTED` with candidate-level `STRATEGY_SELECTED` or `REJECTED` outcomes. Every response explicitly reports `execution: "NOT_SUBMITTED"` and `risk_status: "NOT_EVALUATED"`. This is orchestration of research, not unattended trading or evidence of strategy profitability.

Authenticated API:

- `POST /research/autonomous` accepts `universe_id`, `idempotency_key`, optional PAPER-only `mode`, optional aware `as_of`, and `top_n` (maximum 10).
- `GET /research/autonomous/{run_id}` returns the persisted checkpoint and candidate outcomes.

CLI:

```powershell
python -m stockmarket research-autonomous --universe <universe-id> --idempotency-key <stable-run-key> --top 5
python -m stockmarket research-autonomous-show --run-id <run-id>
```

`AUTONOMOUS_RESEARCH_SETTINGS` optionally bounds `max_candidates` (1–10), `max_concurrency` (1–4 and no greater than scanner/research limits), `allowed_markets`, `allowed_asset_classes`, and `strategy_allowlist`. Defaults inherit enabled markets, supported asset classes, and registered research strategies. This feature requires configured AI assessment and is not an order automation path.

## Phase 2C-2: deterministic signal generation

`SignalGenerationService` is a separate, explicit next step consuming persisted `STRATEGY_SELECTED` candidate, opportunity, and snapshot records. It checks the persisted identities, strategy implementation/version, market/asset compatibility, configured market session and calendar coverage, timestamp ordering/age, and provider OHLCV quality before invoking the already-registered strategy's existing `evaluate()` contract. It does not add another strategy engine or call AI during evaluation. Unknown strategy versions, absent session configuration, stale/future/invalid data, invalid candidate state, and strategy failures fail closed with an explicit rejection.

Migration V10 stores generated results and provenance with database-backed idempotency. A successful result changes that candidate's terminal research checkpoint to `SIGNAL_GENERATED`; `HOLD` remains the domain `SignalSide.HOLD` and is surfaced as `NO_SIGNAL`. Failures are retained as rejected attempts when their persisted opportunity and snapshot references are valid. Results include the opportunity/snapshot/run identifiers, strategy/version/config, evaluation and market-data timestamps, provider, data quality, and input fingerprint. The service has no `RiskEngine`, sizing, order, broker, executor, or portfolio dependency. Every result explicitly says `execution: "NOT_SUBMITTED"` and `risk_status: "NOT_EVALUATED"`; deterministic risk remains the final gate in any separately authorized later workflow.

Authenticated API:

- `POST /research/autonomous/{run_id}/candidates/{candidate_id}/signal` accepts a required timezone-aware `evaluation_as_of`.
- `GET /research/autonomous/{run_id}/candidates/{candidate_id}/signal` reads the latest persisted result. `candidate_id` may be the instrument ID or its selected opportunity ID.

CLI:

```powershell
python -m stockmarket research-signal --run-id <run-id> --candidate-id <instrument-or-opportunity-id> --as-of 2026-10-05T13:50:00Z
```

`SIGNAL_GENERATION_SETTINGS` optionally sets positive `max_opportunity_age_seconds` and `max_market_data_age_seconds`, each capped at seven days (defaults: five minutes). Evaluation does not occur automatically after opportunity selection. Historical evaluation also cannot establish that a mutable third-party data source is point-in-time immutable. This phase generates observations only: it does not create proposals, approve risk, size or submit paper orders, or establish profitability/live readiness.

## Phase 2C-3: persisted signal sizing and risk evaluation

`TradeProposalService` reads the persisted `SIGNAL_GENERATED` record; it does not re-run the strategy or ask AI to generate or alter a signal. Before evaluation it verifies the PAPER run, candidate, generated-signal, opportunity, snapshot and registered strategy-version links; actionable BUY/SELL side; timezone-aware, fresh and non-future timestamps; covered regular market session; tradable instrument constraints; configured FX; and current portfolio/order/proposal exposure. Unsupported SELL/short entries fail closed unless the instrument is explicitly marked shortable. HOLD remains non-actionable. Evaluation is serialized within one service process, but this does not provide cross-process portfolio reservation or support multi-worker risk evaluation.

The existing deterministic `size_position()` computes account-currency quantity from the stored entry/stop, account equity and cash, configured risk and exposure limits, instrument constraints, and portfolio exposure. `RiskEngine` evaluates that sized proposal as the final deterministic gate. Migration V11 persists each risk evaluation and its input fingerprint; a `TradeProposal` row is created only for an approved decision. A persisted approved proposal for the same signal is returned on repeat rather than duplicated. FX provenance identifies `PortfolioManager.rate_to_base` as the configured source; this phase does not provide a timestamped FX quote feed.

This is a terminal advisory artifact. The approved result includes the risk decision, sizing and proposal provenance and explicitly reports `execution: "NOT_SUBMITTED"` and `order_id: null`. It does not call `TradingService.submit()`, `OrderManager`, a broker or executor, create an order, or mutate portfolio positions/cash. It does not implement Phase 2C-4 or enable autonomous/paper/live order placement.

Authenticated API:

- `POST /research/autonomous/{run_id}/candidates/{candidate_id}/risk` evaluates the candidate's persisted signal, optionally at a required-to-be-timezone-aware `evaluation_as_of`.
- `GET /research/autonomous/{run_id}/candidates/{candidate_id}/risk` returns the latest persisted evaluation.

CLI:

```powershell
python -m stockmarket research-risk --run-id <run-id> --candidate-id <instrument-or-opportunity-id> --as-of 2026-10-05T13:50:00Z
```

The timestamp option is optional. Migration V11 must be applied before using the workflow. Automatic evaluation after signal generation is deliberately disabled. Missing calendar coverage, expired research/data, missing configured FX, unsupported instrument state, failed sizing, or a RiskEngine rejection cannot produce an approved proposal.

## API

All endpoints require the configured bearer token:

- `POST /research/candidates` accepts `{ "scan_id": "...", "limit": 20, "as_of": "2026-10-08T20:00:00Z" }`. `as_of` may be omitted to use the service clock, but cannot predate the scanner run's persisted `as_of`. Only selected Top-N candidates from a persisted `RESEARCH` scanner run are eligible; `PAPER` runs are rejected.
- `GET /research/runs/{run_id}` returns the durable run and its snapshots.
- `GET /research/snapshots/{snapshot_id}` returns the snapshot and its individually persisted evidence records.
- `POST /research/assessments/{snapshot_id}` explicitly assesses a persisted snapshot and returns a research-only opportunity. It does not refresh evidence or execute anything.
- `GET /research/assessments/{assessment_id}` returns the persisted assessment and associated opportunity.
- `GET /research/opportunities?limit=100&offset=0` lists persisted opportunities in deterministic rank order.

Responses explicitly set `research_only: true`, `execution: "NOT_SUBMITTED"`, and `risk_status: "NOT_EVALUATED"`. Snapshot/research status is `COMPLETE`, `PARTIAL`, or `FAILED`. Autonomous-run status also includes `INTERRUPTED` while a failed stage awaits safe retry. Missing optional sources are not assigned neutral scores. Because macro and sentiment do not yet have providers, runs will normally be `PARTIAL`.

## CLI

```powershell
python -m stockmarket research --scan-id <research-scan-id> --limit 20
python -m stockmarket research --scan-id <research-scan-id> --as-of 2026-10-08T20:00:00Z
python -m stockmarket research-show --run-id <research-run-id>
python -m stockmarket research-show --snapshot-id <snapshot-id>
python -m stockmarket research-assess --snapshot-id <snapshot-id>
python -m stockmarket research-assessment-show --assessment-id <assessment-id>
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
| AI input/output and opportunity ranking | IMPLEMENTED IN PHASE 2B-F | Manual snapshot-only assessment, strict output/citation validation, registered-strategy selection, deterministic explainable ranking, provenance and V8 persistence; ends at `STRATEGY_SELECTED` or `REJECTED`. |
| Execution and risk | UNCHANGED | No signals or orders are produced here. Downstream paper acceptance still requires explicit action and RiskEngine evaluation. |

## Validation and future work

Focused tests cover snapshot persistence and inspection, `as_of` rejection, paper-scan rejection, source-attributed news, historical-news suppression, future evidence rejection, AI schema/citation and provider failures, registered-strategy validation, deterministic ranking, V8 persistence and authenticated assessment API behavior. Phase 2C-1 through 2C-3 are separate explicit PAPER research stages. Phase 2C-3 preserves deterministic strategy and research provenance, uses RiskEngine as the final gate, and stops at a persisted advisory proposal; Phase 2C-4 is not implemented.

Historical provider revisions, point-in-time fundamentals and news archives, macro/sentiment providers, source licensing, cache/load behavior at broad-universe scale, and independent calibration remain open work. Nothing in this phase establishes profitability, live readiness, or production suitability.
