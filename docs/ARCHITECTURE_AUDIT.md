# Architecture Audit (Phase 0)

Date: 2026-10-05  
Repository: KatkarPankaj/Trading-strategy  
Scope: Non-destructive audit of current implementation only

## 1) Executive Summary

The repository is a working paper-trading/research simulator with useful features (strategy scoring, backtest, parameter sweep, paper portfolio, and Streamlit dashboards), but it is currently a monolith around India intraday assumptions and UI-driven orchestration.

It is not yet production-grade for live execution and not yet globally extensible by architecture. The highest-risk gaps are:

- Tight coupling of business logic to Streamlit UI state.
- No broker abstraction and no live/paper execution boundary as separate engines.
- No formal risk engine with mandatory pre-trade checks and explicit reject reasons.
- Market assumptions hard-coded across modules (time window, timezone, symbols, session hours).
- File-based mutable state with no transactional guarantees.
- Data-provider reliability and validation safeguards are partial.
- Limited automated testing footprint (no test suite detected in repository tree).

## 2) Current Architecture

## 2.1 Source Layout

Primary code paths are:

- dashboard.py (large advanced Streamlit app with scanning, paper execution, strategy tracking, persistent pending orders, and learning heuristics)
- dashboard_simple.py (lightweight Streamlit app with signal ranking + auto paper cycle + state snapshots)
- src/stockmarket/config.py (TradingConfig dataclass)
- src/stockmarket/data.py (Yahoo fetch + cache)
- src/stockmarket/strategy.py (ORB + VWAP signal columns)
- src/stockmarket/backtest.py (single-position-at-a-time backtest)
- src/stockmarket/sweep.py (parameter sweeps)
- src/stockmarket/cli.py (backtest/signal/sweep/replay commands)
- src/stockmarket/webapp.py (legacy streamlit frontend invoking core functions)

## 2.2 Architectural Style Observed

Current style is a hybrid:

- Library-style modules in src/stockmarket for data, strategy, backtest, CLI.
- Two UI-first apps that also host core domain logic and execution logic.
- File persistence (JSON/CSV) used as system-of-record for paper state and history.

No explicit domain-layer boundaries currently exist for:

- instruments
- orders and order lifecycle
- broker adapters
- risk decisions
- portfolio engine
- market sessions/calendars
- audit and event store

## 3) Current Data Flow

## 3.1 Market Data

- Yahoo path:
  - src/stockmarket/data.py fetch_intraday_data downloads yfinance bars, standardizes columns, converts timezone, filters fixed session window, and caches with pickle.
- NSE quote path:
  - dashboard.py and dashboard_simple.py call nsepython directly for quote-equity endpoint.

Observations:

- Data-provider calls are made directly in dashboards and strategy orchestration, not behind an abstract provider interface.
- Session filtering in src/stockmarket/data.py is hard-coded to 09:15-15:30.
- There is retry/backoff for Yahoo only; no comprehensive stale/missing/duplicate timestamp quality gate that blocks trading globally.

## 3.2 Configuration

- config.json and config.example.json load into TradingConfig.
- Many runtime behaviors in dashboards are side-bar controls and session_state values beyond TradingConfig.

Observations:

- Typed config is present but incomplete for production concerns (environment, secrets, runtime mode, broker config, risk policies).

## 4) Current Strategy Flow

- Core indicator/signal generation in src/stockmarket/strategy.py:
  - OR high/low, VWAP, volume spike, long_signal, short_signal.
- Dashboard-level strategy scoring and plan generation:
  - dashboard.py has mode-specific score_signal and separate quote-based/yahoo-based planning.
  - dashboard_simple.py has separate ranking logic with regime-like hints and learning score adjustments.

Observations:

- Strategy semantics are duplicated across files.
- No common Strategy interface returning a normalized Signal object.
- Multiple ad-hoc signal formats exist (DataFrame columns, dict plans, UI labels).

## 5) Current Execution Flow

## 5.1 Backtest

- src/stockmarket/backtest.py simulates entries/exits from long_signal/short_signal.
- Applies slippage and commission, with stop/target/time/square-off exits.

Limitations:

- Simplified fill model (single-bar checks, no partial fills, no latency model, no order queue model).
- One-in/one-out intraday position model per day loop; no portfolio-level cross-symbol constraint engine.

## 5.2 Paper Trading (Dashboards)

- Both dashboards contain direct order execution functions mutating state and writing files.
- Position and cash calculations are embedded in UI script runtime.

Limitations:

- No standalone OrderManager state machine.
- No broker adapter abstraction even for paper path.
- Idempotency and duplicate-order defenses are not formalized as a reusable service.

## 6) Current Persistence

Current persistent artifacts:

- JSON state files (for active paper session)
- CSV ledgers/history/daily summaries
- Snapshot JSON files
- Cached market data pickle files in .cache

Risks:

- No transactional integrity or locking model.
- Concurrent writes/race conditions possible if multiple instances run.
- Pickle usage in cache has integrity/security implications if cache files are tampered with.
- No normalized relational schema for orders/fills/positions/risk decisions.

## 7) Current Risk Controls (What Exists)

Positive controls detected:

- Per-trade and per-position checks in dashboards (qty/notional limits, margin/cash checks).
- Entry windows and square-off windows.
- Stop-loss and take-profit calculations.
- Max trades/day and max open positions controls.
- Additional quality gates in advanced dashboard (score, reward/risk, expected edge).

Critical gaps:

- No centralized non-bypassable RiskEngine component.
- No explicit RiskDecision object with approved/rejected + reason ID.
- No comprehensive portfolio-level exposure/correlation/sector constraints at engine level.
- No global kill switch framework decoupled from UI.

## 8) Key Weaknesses and Coupling

## 8.1 UI-Business Logic Coupling

- Streamlit session_state is used as a de facto domain store and orchestration layer.
- Order execution, risk checks, ranking, persistence, and analytics are mixed with rendering code.

Impact:

- Hard to test deterministically.
- Hard to reuse logic for API/live execution.
- Increased regression risk when changing UI.

## 8.2 Duplicated Logic

Examples:

- Charges and trade accounting logic appear in both dashboards.
- Signal ranking and strategy plan logic are split and partially duplicated.
- Separate persistence conventions across dashboards (different state formats/files).

## 8.3 Error Handling Pattern

- Many broad except Exception blocks with continue/pass fallback behavior.

Impact:

- Operational faults may be suppressed.
- Difficult root-cause analysis.

## 8.4 Documentation Drift

- DOCUMENTATION.md contains statements that diverge from current code in places (for example, references to implementation details not matching current file behavior and legacy descriptions).

## 9) Security Issues and Risks

1. config.json is tracked and currently includes runtime settings. No dedicated secret handling strategy is implemented (even though values shown are non-secret at present).
2. .env is ignored, but no .env.example exists to enforce secret separation conventions.
3. Cache uses pickle load/dump; tampered local cache files can be a code-execution vector in hostile environments.
4. Extensive file writes from UI loops without integrity checks/audit signatures.
5. No structured redaction rules for logs/history if sensitive fields are introduced later.

## 10) Production Blockers

1. No paper/live hard separation with safety gates.
2. No broker adapter interface or live execution service.
3. No typed, environment-aware production settings model.
4. No database persistence layer with migration/versioning.
5. No service/API layer for headless operation; logic tied to dashboards.
6. No comprehensive automated tests for risk/order/execution invariants.
7. No observability baseline (structured logs, health checks, alerts).

## 11) Internationalization Blockers

1. Hard-coded market assumptions:
   - Asia/Kolkata defaults
   - 09:15 / 15:15 / 15:30 session logic
   - Fixed India watchlists and .NS-centric symbol sets
2. Session filtering in data layer fixed to India regular hours.
3. Strategy execution windows and day-boundary logic are exchange-specific and embedded in dashboards.
4. Currency assumptions are INR display-centric in UI/accounting labels.

## 12) Dead Code / Fragility Indicators

- src/stockmarket/webapp.py appears to be a legacy UI path while dashboards dominate interactive usage.
- Multiple paths produce overlapping outputs, increasing maintenance burden.
- No tests directory detected; behavior relies on manual run-time validation.

## 13) Recommended Migration Plan (High-Level)

The following plan is incremental and preserves current working functionality.

Phase A: Foundation (no behavior break)

1. Introduce new package skeleton for layered architecture (core/data/strategies/risk/execution/brokers/persistence/monitoring/config/api).
2. Add typed settings loader (env + json overlays) and explicit runtime mode flags.
3. Create domain models for Instrument, Signal, Order, Position, RiskDecision.

Phase B: Extract and Stabilize Engines

1. Extract paper order execution from dashboards into PaperExecutor + OrderManager.
2. Introduce RiskEngine as mandatory pre-trade gate used by paper executor.
3. Add MarketSession abstraction and move all time-window logic out of strategy/UI.

Phase C: Provider Abstraction

1. Define MarketDataProvider interface and adapters:
   - YahooResearchProvider
   - NSEQuoteProvider (research/paper)
2. Implement data quality checks (stale/missing/duplicate timestamp validation) and fail-closed trade gating.

Phase D: Persistence and Audit

1. Add repository layer with SQLite first, PostgreSQL-ready schema.
2. Persist signals, orders, fills, positions, risk decisions, and event logs.
3. Keep CSV exports as derived artifacts, not source-of-truth.

Phase E: API + Dashboard Decoupling

1. Create FastAPI read/write endpoints.
2. Refactor Streamlit dashboards to consume service/API interfaces only.
3. Mark trading mode prominently (PAPER/LIVE).

Phase F: Testing + Ops

1. Add unit/integration tests for:
   - risk limits
  - order state transitions
   - data validity gates
   - portfolio accounting
2. Add CI checks: lint, type-check, tests, dependency/secret scanning.
3. Add structured logging + health probes + alerts.

## 14) Priority Risk Matrix

Critical:

- Missing non-bypassable risk engine
- UI-driven execution path
- No hard paper/live gate framework

High:

- Hard-coded market/session assumptions
- File-based mutable state and weak fault tolerance
- Provider abstraction missing

Medium:

- Duplicate strategy/ranking logic
- Documentation drift
- Broad exception suppression

## 15) Phase 0 Deliverables Completed

- Inspected repository source modules, dashboards, config files, requirements, and documentation.
- Produced this non-destructive architecture audit.

No runtime trading logic was modified in this phase.
