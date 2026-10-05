# Copilot Instructions

## Project Purpose

This repository is a personal-use, NSE-focused intraday market research and paper-trading simulator. It currently supports Yahoo Finance historical/intraday data, an NSE quote path in the Streamlit dashboards, ORB/VWAP-related signals, backtesting, parameter sweeps, and local paper portfolio tracking. It is not a production trading platform and does not currently include a live broker integration.

The long-term direction is a globally extensible algorithmic trading platform. Preserve existing Indian-market functionality while evolving toward provider-independent instruments, market calendars, strategies, risk, execution, persistence, and monitoring. Do not claim that a strategy is profitable or that this application is production-ready without relevant validation.

## Current Architecture

- `src/stockmarket/config.py`: `TradingConfig` dataclass loaded from JSON.
- `src/stockmarket/data.py`: Yahoo Finance download, normalization, retry/backoff, and local pickle cache.
- `src/stockmarket/strategy.py`: ORB/VWAP/volume indicators and signal columns.
- `src/stockmarket/backtest.py`: simplified single-instrument backtest with slippage, commission, stop/target/time exits, and square-off.
- `src/stockmarket/sweep.py`: parameter sweep over the existing backtest.
- `src/stockmarket/cli.py`: backtest, signal, sweep, and replay-best commands.
- `src/stockmarket/webapp.py`, `dashboard.py`, and `dashboard_simple.py`: Streamlit interfaces. The dashboard scripts currently contain business logic, paper execution, and file persistence as well as rendering.
- `outputs/`: local paper state, snapshots, and CSV exports. These are local artifacts, not a transactional database.

Keep changes small and compatible with current entry points unless the task explicitly approves a migration. Before changing a behavior, inspect its owning module and nearby call sites. Prefer extracting logic behind testable interfaces over adding more business logic to dashboard rendering code.

## Python and Code Standards

- Support Python 3.10 or newer; existing code uses modern type annotations and union syntax.
- Follow the style of the touched module. Use clear names, focused functions, type annotations for new public interfaces, and standard-library or existing dependencies before adding a new dependency.
- Keep domain logic deterministic and independent of Streamlit where practical. Pass configuration, clocks, providers, and state explicitly rather than introducing new global mutable state.
- Avoid unrelated refactors. Preserve existing paper workflows unless the requested change intentionally alters them.
- Use timezone-aware datetimes for market/session decisions. Do not introduce naive datetime comparisons or hard-code exchange times into new strategies.
- Validate external data and user-provided values at module boundaries. Fail closed for trading decisions when data validity is uncertain.

## Testing Requirements

- Add focused tests for every behavior change. Risk, sizing, order, portfolio, and data-validation changes require tests for both valid and rejected cases.
- Keep tests deterministic: use fixtures or injected clocks/providers rather than network calls or current market time.
- Tests must not place real orders or require broker credentials. Mock external services and provider responses.
- Run the narrow relevant tests first, then the full suite when available. Report test commands and results accurately.
- The repository currently has no detected automated test suite. When adding one, establish a repeatable test command and document it; do not imply existing tests passed if none were run.

## Linting, Formatting, and Type Checking

- No repository-wide lint, formatter, or static type-check configuration is currently established. Do not silently introduce conflicting tools or claim compliance with checks that are not configured.
- For new tooling, choose and configure one consistent linter/formatter/type checker, document the commands, and add them to CI before treating them as required gates.
- Keep imports, whitespace, and annotations clean in changed files. Run available diagnostics and focused checks after edits.

## Security and Secrets

- Never commit API keys, broker credentials, tokens, passwords, private certificates, or account identifiers.
- Keep `.env` and secret-bearing local configuration out of Git. Use environment variables or an approved secret manager for secrets; provide only placeholders in example files.
- Do not log credentials or sensitive account data. Redact secrets from errors, structured logs, and audit events.
- Treat provider responses, config values, persisted state, and user input as untrusted. Validate types, ranges, symbols, quantities, prices, and timestamps.
- Avoid unsafe deserialization. The current market-data cache uses pickle; do not extend pickle to state, orders, or other untrusted/persistent inputs. Prefer a safe, versioned format or replace the cache format as a separate migration.
- Do not add shell execution based on user-controlled or external values. Use HTTPS for external services and least-privilege credentials.
- Keep dependency versions controlled and review new dependencies for maintenance and security risk.

## Database and Persistence Rules

- Current JSON/CSV files are local paper-trading persistence, not a production database. Do not describe them as transactional or concurrency-safe.
- Keep current file formats compatible unless a migration is explicitly requested. Protect user-created state and output files; never delete or reset them implicitly.
- When introducing a database, use a repository/data-access boundary and schema migrations. SQLite is suitable for local development; production requirements should be designed for PostgreSQL.
- Do not store API or broker secrets in the database. Persist auditable domain events and order/risk decisions with explicit timestamps and stable identifiers.
- Make writes atomic where practical and handle partial/corrupt state explicitly; never silently replace unreadable state with a blank portfolio without surfacing the recovery risk.

## Logging and Observability

- Do not silently swallow execution, persistence, or data-provider errors. Catch expected exceptions at an appropriate boundary, preserve useful context, and make failures visible to the user/operator.
- Use structured logging for newly extracted services. Include relevant component, timestamp, symbol, strategy, correlation/signal/order identifiers, and severity when available.
- Never log secrets. Distinguish expected skips/rejections from infrastructure failures.
- Health checks and alerting are future capabilities; do not claim they exist until implemented and tested.

## Broker Integration Rules

- There is currently no broker adapter or live order path. Do not add direct broker calls to a strategy, dashboard, or data provider.
- Future integrations must implement a broker-independent interface behind an execution service, with provider-specific code isolated in an adapter.
- Broker order submission must be idempotent, validate order state and instrument constraints, preserve broker responses, and have tests using mocks or a sandbox.
- Never place a live order as part of a test, demo, backtest, or paper workflow. Live integration requires explicit user authorization and separate safety review.

## Risk Management Rules

- Existing safeguards include some per-trade sizing, cash/margin checks, quantity/notional caps, stop-loss/take-profit behavior, entry windows, trade-count limits, and quality filters. These are distributed across modules and are not a centralized, non-bypassable risk engine.
- Do not remove, weaken, or bypass an existing risk control without an explicitly approved replacement that is at least as strong and has tests.
- New order-producing paper code must validate positive price and quantity, available cash/margin, position limits, and applicable configured risk limits before mutating portfolio state.
- Keep risk decisions explicit and explainable. Rejections must include a reason. Missing, stale, invalid, or contradictory market data must prevent new entries.
- Do not infer that dashboard controls alone provide portfolio-wide protection. A centralized RiskEngine is a planned migration, not a current feature.

## Paper and Live Separation

- The current application is paper-trading only. Preserve this invariant.
- Paper execution must remain simulated and must never fall through to a live broker path.
- Do not add live mode, live order endpoints, or implicit broker submission without a separately approved phase that implements explicit opt-in configuration, startup safety gates, a kill switch, reconciliation, auditability, and tests.
- If execution mode is added later, default to PAPER, require live trading to be disabled by default, and display the active mode prominently.

## International Markets

- India is the currently supported market; do not remove NSE/BSE support.
- Do not add new assumptions that all markets use INR, Asia/Kolkata, NSE symbols, Indian holidays, or 09:15-15:30 sessions.
- Keep exchange/session calendars, currency, tick size, lot size, timezone, and instrument identity in explicit market/instrument definitions as those abstractions are introduced.
- Use timezone-aware timestamps and convert only at clear provider, storage, and display boundaries. Strategies should not contain exchange-specific time rules.
- Treat Yahoo Finance as a research data source, not a guaranteed production execution feed.

## Deployment Requirements

- The current project is a local Windows-friendly Streamlit application. Docker, CI/CD, production API, database migrations, health checks, and Linux deployment are not yet established.
- Do not claim deployment readiness or introduce environment-specific absolute paths. Use portable paths and explicit configuration.
- Never include secrets in images, committed configuration, logs, or sample commands.
- Future deployment changes should include health checks, safe startup defaults, persistent-data/backup guidance, dependency and secret scanning, and a documented rollback/recovery approach.

## Incremental Change Process

1. Inspect the relevant implementation and tests before editing.
2. State the local behavior being changed and its safety implications.
3. Make the smallest reviewable change; do not attempt the entire platform migration in one patch.
4. Add or update focused tests and documentation.
5. Run the most targeted available validation, then broader checks when available.
6. Summarize changed files, behavior, validation, and any remaining risk. Keep Phase 0 findings in `docs/ARCHITECTURE_AUDIT.md` as the baseline for future migration work.
