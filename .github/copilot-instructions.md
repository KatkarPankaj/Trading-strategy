# Copilot Instructions

## Project Purpose and Scope

This repository is building a globally extensible AI-driven algorithmic trading platform for US equities/ETFs and Indian equities, with Europe-ready market architecture. Its objective is to research markets continuously, understand news and market conditions, identify and rank opportunities, select an appropriate strategy, and explain trade ideas.

AI is the market-research and strategy-selection intelligence layer. AI may analyze and rank opportunities and recommend which deterministic strategy to evaluate, but it must not create executable orders or replace quantitative strategy rules. Deterministic strategies generate structured signals; RiskEngine makes the final deterministic risk decision; OrderManager controls order lifecycle and submission. AI must never bypass or weaken those safety and execution boundaries.

The current application remains paper-trading only. The October 2026 platform layer is the active architecture baseline, not a complete or production-ready live trading system. Treat code predating October 1, 2026 only as legacy context or as a workflow to preserve when still needed; do not use it as the design authority, let historical assumptions drive new architecture, or rewrite it indiscriminately. Do not claim that a strategy is profitable, deployment is production-ready, or live trading is supported without relevant implementation and validation.

## Current Architecture

- The intended authoritative flow is market data and research → market/news intelligence and regime → AI opportunity research and strategy selection → deterministic strategy signal → signal aggregation → RiskEngine → position sizing → OrderManager → paper executor/broker → portfolio and audit. AI stays on the research and strategy-selection side of this boundary.
- `src/stockmarket/core/` contains the developing platform layer: domain models, markets/calendars/sessions, data-provider contracts and quality/resilience helpers, risk and sizing, portfolio/order services, paper/live executor contracts, persistence, recovery, audit, AI/news research, backtesting validation, monitoring and kill-switch modules.
- Legacy entry points include `dashboard.py`, `dashboard_simple.py`, `src/stockmarket/webapp.py`, and the CLI. Older dashboard logic may remain for backward-compatible workflows, but it is not the target architecture. Preserve needed workflows during incremental migration; do not expand legacy coupling or treat pre-October behavior as the product objective.
- `src/stockmarket/api/` and `src/stockmarket/dashboard/` provide newer service/UI surfaces. The API bootstrap currently wires a paper broker and paper trading service.
- `src/stockmarket/config.py`, `src/stockmarket/data.py`, `src/stockmarket/strategy.py`, `src/stockmarket/backtest.py`, `src/stockmarket/sweep.py` and `src/stockmarket/cli.py` remain established library/CLI paths. The legacy market-data cache uses Parquet; do not assume it uses pickle.
- `src/stockmarket/core/persistence/` provides database/repository and migration code. Legacy JSON/CSV outputs remain local paper artifacts and are not transactional or concurrency-safe.
- `tests/` contains focused automated test modules. Coverage is incomplete, especially for newer platform services. Check declared development dependencies and available tools before claiming tests ran.
- `Dockerfile`, `docker-compose.yml` and `deploy/` assets exist but have not thereby been validated as production deployment. `.github/workflows/ci.yml` runs the existing unittest suite, Bandit and pip-audit; it is not a production deployment gate.

The legacy dashboards do not yet consistently use the new platform services. Before behavior changes, inspect the owning module and its callers; do not assume a platform abstraction is wired into a dashboard or API simply because it exists. Change legacy code only when needed to preserve a required workflow or deliberately migrate it toward the platform objective.

## Python and Code Standards

- Support Python 3.10 or newer. Follow the touched module's formatting and patterns.
- Use clear names, focused functions and type annotations for new public interfaces. Prefer the standard library or existing dependencies; justify new dependencies.
- Keep domain logic deterministic and independent of Streamlit where practical. Pass configuration, clocks, providers and state explicitly rather than adding global mutable state.
- Make small, compatible changes. Avoid unrelated refactors and preserve working paper workflows unless the request intentionally changes them.
- Use timezone-aware datetimes for market/session decisions. Do not introduce naive datetime comparisons or exchange-specific time rules into strategies.
- Validate external data and user input at module boundaries. Fail closed for trading decisions when data is stale, missing, invalid or contradictory.

## Testing Requirements

- Add focused tests for every behavior change. Risk, sizing, order, portfolio and data-validation changes need valid and rejected-case coverage.
- Keep tests deterministic using fixtures or injected clocks/providers; do not require network access, real orders or broker credentials.
- Run the narrow relevant tests first, then broader tests when available. Report commands and results accurately; distinguish unavailable tooling from test failures.
- Test modules exist, but the suite and dependencies may not be installed in every environment. Check `requirements-dev.txt` and the configured interpreter before running tests. Do not claim the suite passed if it was not executed.

## Linting, Formatting and Type Checking

- There is no established repository-wide lint, formatting or static type-check gate. CI currently enforces tests, Bandit and pip-audit only. Do not claim compliance with unconfigured checks.
- Use existing configured tools where available. Do not add conflicting tools or make them mandatory without configuring and documenting a consistent workflow.
- Keep imports, whitespace and annotations clean in changed files.

## Security and Secrets

- Never commit API keys, broker credentials, tokens, passwords, private certificates or account identifiers.
- Keep `.env` and secret-bearing local configuration out of Git. Use environment variables or an approved secret manager for secrets; examples must use placeholders.
- Do not log credentials or sensitive account data. Redact secrets from errors, structured logs and audit events.
- Treat provider responses, config, persisted state and user input as untrusted. Validate types, ranges, symbols, quantities, prices and timestamps.
- Avoid unsafe deserialization. The current market-data cache is Parquet. Do not introduce pickle for state, orders or other persistent/untrusted inputs.
- Do not execute shell commands using user-controlled or external values. Use HTTPS for external services and least-privilege credentials.
- Keep dependency versions controlled and review new dependencies for maintenance and security risk.

## Persistence and Database

- Legacy JSON/CSV files are local paper-trading artifacts, not transactional storage. Do not describe them as concurrency-safe.
- Preserve user-created state and output files. Never reset or delete them implicitly. Handle partial or corrupt state explicitly; do not silently replace unreadable state with a blank portfolio.
- New database-backed features should use the repository/data-access boundary and schema migrations. SQLite is suitable for local development; production configuration is intended to support PostgreSQL.
- Never store API or broker secrets in the database. Persist auditable domain events and order/risk decisions with explicit timestamps and stable identifiers.
- Make writes atomic where practical and surface recovery risks.

## Logging and Observability

- Do not silently swallow execution, persistence or provider errors. Catch expected exceptions at an appropriate boundary, preserve useful context and make failures visible.
- Use the platform structured-logging helpers for newly extracted services where appropriate. Include component, timestamp, symbol, strategy, correlation/signal/order identifiers and severity when available.
- Never log secrets. Distinguish expected skips/rejections from infrastructure failures.
- Health, alerts and kill-switch modules exist in the platform layer, but integration and operational behavior must be verified before claiming they are complete or production-ready.
- AI/research failures mean that research is unavailable; they must never be interpreted as a positive signal, a fallback trade, or permission to bypass deterministic validation.

## Broker and Order Management

- There is no concrete live broker adapter in the current platform implementation. `BrokerAdapter` and live-execution contracts are architectural interfaces; `PaperBroker` and the API bootstrap are paper-oriented.
- Do not add direct broker calls to a strategy, dashboard or data provider. Strategies produce structured signals; order-producing flows must pass through the order-management and risk boundaries.
- Any future broker adapter must be isolated from strategy code, validate instrument/order constraints, make submission idempotent and preserve broker responses.
- Tests, demos, backtests and paper workflows must never place real orders or require broker credentials. Live integration requires separate explicit authorization and safety review.

## Risk Management

- Legacy safeguards exist but are distributed across dashboard functions. `src/stockmarket/core/risk.py` and related modules provide a developing RiskEngine; legacy dashboards are not yet consistently wired through it. Do not describe it as a universal, non-bypassable gate until all order paths enforce it.
- Do not remove, weaken or bypass an existing risk control without an explicitly approved replacement that is at least as strong and covered by tests.
- New order-producing paper code must validate positive price and quantity, available cash/margin, position limits and applicable configured risk limits before mutating portfolio state.
- Keep risk decisions explicit and explainable; rejections must include a reason. Missing, stale, invalid or contradictory market data must prevent new entries.
- Dashboard controls alone are not portfolio-wide protection. Keep risk policy in testable domain/services rather than UI rendering.
- AI confidence, sentiment or strategy recommendations are never risk approvals. RiskEngine remains the final deterministic pre-trade gate.

## Paper and Live Separation

- Preserve the paper-only behavior of the current application. Paper execution must remain simulated and must never fall through to a live broker path.
- Paper is the default. Do not enable live trading, add a production live-order route, or connect a real broker without a separately approved implementation phase.
- Any future live capability must require explicit opt-in configuration, startup readiness checks, a kill switch, broker reconciliation, auditability and focused tests. A gated class/interface alone does not establish live support.
- Always display the active execution mode prominently in any interface that presents trading or order status.

## International Markets and Market Data

- India/NSE/BSE support must be preserved. New core logic must not assume INR, IST, Indian holidays, exchange symbols or 09:15-15:30 hours.
- Obtain market, session, holiday, currency, tick size, lot size and timezone facts from explicit market/instrument/calendar definitions. Current market definitions include US, India and Germany, but only US holiday-calendar data is present; do not imply full calendar coverage for other markets.
- Use timezone-aware timestamps and convert only at clear provider, storage and display boundaries. Strategies must not contain exchange-specific session logic.
- Use the `MarketDataProvider` abstraction for new service/strategy integrations rather than calling vendor SDKs directly. Yahoo Finance is research-only, not a guaranteed production execution feed.
- New order-producing logic must fail closed on stale or invalid data. Preserve errors and quality issues rather than silently substituting plausible values.

## Deployment

- Docker, Compose, a service unit and backup scripts exist, but their presence is not proof of a validated or production-ready deployment. Verify relevant build, configuration, health and recovery behavior before making such claims.
- Use portable paths and explicit configuration. Do not introduce machine-specific absolute paths.
- Never include secrets in images, committed configuration, logs or sample commands.
- Deployment changes should account for safe startup defaults, health checks, persistent data and backups, dependency/secret scanning, and documented rollback/recovery.

## Incremental Change Process

1. Inspect the relevant implementation, tests and call sites before editing.
2. Identify the behavior changing and any paper-trading/risk safety implications.
3. Make the smallest complete, reviewable change; do not attempt an uncontrolled platform rewrite.
4. Add/update focused tests and directly related documentation.
5. Run the narrowest relevant validation, then broader checks when available. State unavailable checks explicitly.
6. Summarize changed files, behavior, validation and remaining risk. Keep Phase 0 findings current in `docs/ARCHITECTURE_AUDIT.md`.
