# Platform Architecture

## Product objective

Build an international market-research and algorithmic-execution platform. AI is the research and strategy-selection intelligence layer: it interprets market/news context, ranks opportunities, recommends a deterministic strategy to evaluate, and explains its reasoning. AI is not an execution authority.

Deterministic strategies produce structured signals. Signal aggregation combines validated evidence. `RiskEngine` makes the final deterministic pre-trade decision, and `OrderManager` owns order lifecycle and submission. Paper is the supported execution mode; live trading remains disabled and unsupported until an explicitly approved implementation supplies a concrete broker adapter and passes all safety gates.

## Authoritative target flow

```text
Market definitions and instruments
    -> market session/calendar
    -> market-data providers and quality checks
    -> technical / news / fundamental / sentiment inputs
    -> market-regime analysis
    -> AI research and opportunity ranking
    -> AI strategy recommendation
    -> deterministic strategy signal
    -> signal aggregation
    -> RiskEngine
    -> position sizing
    -> OrderManager
    -> PaperExecutor / PaperBroker
    -> PortfolioManager
    -> persistence, audit and monitoring
```

The stages before the deterministic signal may inform research and candidate selection, but may not submit orders. Invalid, missing or stale data, unavailable research, insufficient evidence, or rejected risk decisions must produce no trade. AI failure is not a trade signal or permission to skip a stage.

## Existing implementation map

| Responsibility | Existing implementation | Integration status |
|---|---|---|
| Domain/instrument definitions | `src/stockmarket/core/models.py`, `markets.py` | Instrument metadata is validated; helper checks cover tick-grid prices and lot/minimum quantities. Market factories inject currency, timezone and local trading hours. |
| Sessions and calendars | `core/market_session.py`, `core/trading_calendar.py`, `core/calendars/` | Time-aware abstractions handle weekdays, explicit holidays, covered-year fail-closed behavior, early closes and recurring intraday pauses. Calendar data is currently present for US only; do not infer holiday coverage for India or Europe. |
| Market data | `core/data/provider.py`, `mock.py`, `yahoo.py`, `yahoo_fundamentals.py`, `quality.py`, `resilient.py`, `factory.py` | The API paper path uses the configured `mock` or `yahoo` price provider through `ResilientProvider` and quote quality checks (identity, freshness and instrument tick grid). Quote failures are logged and become missing data, which RiskEngine rejects. Yahoo and mock remain research-only; legacy dashboards are not migrated. |
| News and AI research | `src/stockmarket/news/`, `core/ai/analyst.py`, `core/ai/schemas.py`, `core/ai/openai_compatible.py`, `core/research.py`, `core/data/yahoo_fundamentals.py`, `core/data/nse_sector_indices.py` | `NewsEvidenceProducer` validates recent instrument-scoped events before analysis. `SectorEvidenceProducer` and `FundamentalEvidenceProducer` accept bounded, timestamped, cited `ResearchObservation` snapshots from provider adapters, validate their instrument, market and freshness, then request a strict AI directional score. Evidence scores are the model score multiplied by its uncalibrated model-reported confidence; this is advisory and not a calibrated forecast. Empty or invalid data yields a warning or a fail-closed pipeline result, never a default score. The opt-in NSE adapter reads the public NSE live-indices endpoint and requires an explicit `NSE_SECTOR_INDEX_MAP` of operator-reviewed NSE symbol→index assignments (`SECTOR_PROVIDER=nse`). Unmapped instruments receive no sector evidence; access can be blocked/rate-limited and failures stop the research request. Membership is not inferred, and the adapter is research-only. An opt-in Yahoo adapter provides only timestamped past earnings-event EPS evidence (`FUNDAMENTAL_PROVIDER=yahoo`); undated profile metrics are excluded. The optional OpenAI-compatible adapter requires an explicitly configured HTTPS endpoint, model and secret key; the bootstrap additionally requires per-market `RESEARCH_SESSIONS` before enabling research. `AIAnalyst.select_strategies()` ranks only caller-supplied deterministic strategy names. |
| Deterministic strategies | `core/strategies/base.py`, `core/strategies/orb_vwap.py`; `src/stockmarket/strategy.py` remains a compatibility surface | The platform `Strategy` contract and `OrbVwapStrategy` produce validated domain `Signal` objects from instrument-, session- and time-aware inputs. Long entries are deterministic; shorts require explicit opt-in. Legacy backtest/dashboard callers still use the DataFrame interface and have not been switched. |
| Market regime | `core/regime.py` | A deterministic, provider-independent evaluator classifies recent validated bars as trending up/down, range-bound or high-volatility and returns a bounded directional score plus per-bar volatility. The score can be explicitly supplied as supporting `SignalInputs.regime`; it is not a signal or risk approval. |
| Strategy research pipeline | `core/strategy_pipeline.py`, `core/research.py` | Composes resilient OHLCV retrieval, regime evaluation, optional news/sector/fundamental producers, AI ranking of registered strategies, deterministic strategy evaluation and signal aggregation. All `ResearchEvidence` must be instrument-matched, timestamped, fresh and unique by component; invalid evidence stops the pipeline. `submit_decision()` is a separate, explicit PAPER-only call through `TradingService.submit_signal()` and its RiskEngine gate. It cannot submit HOLD/SKIP or override risk rejection. On submission, the aggregate decision, deterministic signal, AI ranking metadata, research evidence, quote, risk decision and broker response are linked in the existing audit/persistence layer. |
| Signal aggregation | `core/aggregation.py` | Deterministic strategy signals can now be passed directly to aggregation. The signal replaces the caller-supplied technical score, is identity/freshness/pricing checked, is included in the decision hash and explanation, and gates the aggregate from reversing or inventing a strategy direction. Research scores (including optional AI-derived news scores) remain supporting evidence, never standalone authority to submit an order. |
| Risk and sizing | `core/risk.py`, `risk_portfolio.py`, `sizing.py` | Platform risk and sizing services exist; legacy dashboards are not universally routed through them. |
| Portfolio and order lifecycle | `core/portfolio.py`, `core/order_management.py`, `core/trading_service.py` | `TradingService` creates a risk-checked order path through `OrderManager`; use this platform path for new service integrations. |
| Paper broker/execution | `core/brokers.py`, `core/executors.py` | `PaperBroker` and `OrderManager` are exercised end-to-end from an actionable research-pipeline result through RiskEngine approval, fill synchronization and portfolio accounting. No concrete live broker adapter is implemented. |
| Persistence and audit | `core/persistence/`, `core/audit_trail.py`, `core/recovery.py` | Database/repository, hash-chained audit and recovery foundations exist. Pipeline submissions persist linked strategy decisions, signals, research provenance, risk/broker outcomes and fills; tests rebuild portfolio state and reconcile a restored filled order. Legacy JSON/CSV state remains separate. |
| API and monitoring UI | `api/`, `dashboard/` | Authenticated `POST /research` runs the configured pipeline and returns advisory regime, strategy selection, deterministic signal, aggregation and evidence. It does not submit an order. Bootstrap supports an explicitly configured OpenAI-compatible HTTPS endpoint (`AI_BASE_URL`, `AI_MODEL`, secret `AI_API_KEY`) and per-market `RESEARCH_SESSIONS`, while still allowing injected components. These settings are intentionally absent by default, and the endpoint returns 503 if research, market sessions, or exchange-calendar coverage are unavailable. The monitoring dashboard exposes a read-only research form backed by authenticated `GET /instruments` and `POST /research`; operator-supplied evidence is labeled, results are advisory, and the page has no order-submission action. Legacy dashboards remain separate workflows. |
| CI and operations | `.github/workflows/ci.yml`, `docs/OPERATIONS.md`, `Dockerfile`, `docker-compose.yml`, `deploy/` | GitHub Actions runs the unit suite, Bandit and pip-audit. Operations guidance covers local and Compose setup, required secrets, explicit schema migration, backup/restore, and known limitations. The Compose deployment remains a single-process, loopback-bound paper setup; it is not production/live readiness. |

## Single-authority rules

1. For new order-producing platform code, use `TradingService` as the entry boundary. It evaluates the proposal through `RiskEngine`, then delegates lifecycle handling to `OrderManager`; do not submit directly to a broker or executor.
2. `core/order_management.py` and `core/executors.py` are the current order-lifecycle and executor modules.
3. `core/orders.py` and `core/execution.py` are retained compatibility implementations used by existing tests and exposed through explicit `Legacy*` aliases. Do not extend these as a second platform pipeline; migrate any callers deliberately and preserve compatibility until removal is approved.
4. Strategies return structured domain signals; AI returns validated research data or recommendations. Neither is an order source.
5. `RiskEngine` is the final deterministic entry gate. UI, AI, aggregation scores and API handlers cannot override its rejection.
6. `OrderManager` owns order state transitions and idempotency. Executors/brokers handle execution only after a risk-approved request reaches that boundary.
7. `PortfolioManager` and repositories own platform portfolio/accounting state. Legacy dashboard file formats remain supported only as legacy paths until migrated explicitly.
8. Live execution remains disabled. The API bootstrap's paper wiring is not evidence of a working live path.

## Global-market boundary

Market-specific behavior belongs in market definitions, instrument definitions and trading calendars, not in strategies or risk logic. Keep symbol, exchange, asset class, currency, timezone, tick/lot constraints and trading status explicit. `Instrument.is_valid_price()` and `Instrument.is_valid_order_quantity()` expose instrument-constraint checks; order-creating and approval services must apply them at the appropriate boundary. `TradingCalendar` treats regular sessions as half-open (open included, close excluded); a pause interval `[start, end)` closes at `start` and reopens at `end`. Unknown calendar years are non-trading days. Preserve India support; do not assume a single market's hours, holidays or currency apply globally. Missing calendar or instrument facts must fail closed when they affect a trading decision.

## Integration sequence

Continue incrementally from the existing October platform layer:

1. Migrate legacy ORB/VWAP backtest/dashboard callers deliberately, preserving their existing behavior behind a compatibility adapter and regression tests.
2. Add and validate real provider adapters and source-to-instrument/sector mappings for the new sector/fundamental producer contracts; the core does not supply vendor data.
3. Configure an approved OpenAI-compatible endpoint and explicit session definitions for the API research endpoint; keep it advisory and do not couple research requests to order submission.
4. Extend the API-backed read-only dashboard beyond research as appropriate; keep advisory research separate from all order-submission controls and preserve needed legacy workflows.
5. Expand service/integration tests, then CI and deployment validation.

Do not implement the full pipeline as one rewrite. Do not enable live trading or claim production readiness as a result of architecture scaffolding alone.
