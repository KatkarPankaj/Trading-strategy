# Codebase Structure Review

Last reviewed: 2026-05-21 (post-architecture-migration).

This document describes the repository as it stands after the phase 1–9 architecture migration on `arch-migration-plan`. The legacy MVC stack (`controllers/`, `models/`, `views/components.py`, the MVC `persistence/factory.py` + `file_storage.py` pair) has been deleted; the supported runtime is now `app.py` → `dashboard_simple.render_simple_dashboard()` over the new `domain` / `state` / `cycle` / `persistence` / `backtest` / `optimization` / `ml` / `views` packages.

## Surfaces

- `app.py` — supported Streamlit launcher. Injects the theme and calls `dashboard_simple.render_simple_dashboard(standalone=False)`.
- `dashboard_simple.py` — primary live paper-trading simulator. Owns the Streamlit UI shell, sidebar, market selection, and orchestration; trade execution and view rendering are delegated to the cycle and views packages (see below).
- `src/stockmarket/cli.py` — command-line research interface (`backtest`, `signals`, `sweep`, `replay-best`, `optimize`).
- `src/stockmarket/webapp.py` — secondary Streamlit research UI; retained behind a deprecation banner.
- `dashboard.py` — 31-line deprecation stub. `streamlit run dashboard.py` shows a banner and stops; importers won't `ImportError`. Do not extend.

## Top-Level Layout

- `README.md` — user-facing setup and run guide.
- `MIGRATION.md` — consolidated record of phases 1–9 (replaces the per-phase plan markdowns).
- `CODEBASE_STRUCTURE.md` / `CODEBASE_DATAFLOW.md` — this and its sibling.
- `app.py`, `dashboard_simple.py`, `dashboard.py` — Streamlit entrypoints described above.
- `config.json` / `config.example.json` — CLI/backtest `TradingConfig` files.
- `dashboard_simple_data_config.json` — simple-dashboard data-fetch settings.
- `config/` — JSON config used by `AppSettings` and the persistence factory (`database_config.json` for `paper_repo_backend` / SQLite path).
- `src/stockmarket/` — reusable package, see below.
- `scripts/` — operational scripts. Currently only `migrate_paper_state_json_to_sqlite.py` (one-shot JSON → SQLite migration; retained as future-proofing for a SQLite default flip).
- `tests/` — pytest suite (160 tests at the time of writing).
- `.cache/market_data/` — intraday OHLCV pickle cache.
- `outputs/` — generated artifacts (paper state JSON, sweeps, optimizer reports, ML cache).
- `.database/paper_state.db` — SQLite paper-state DB when `PAPER_REPO_BACKEND=sqlite`.

## Package Layout (`src/stockmarket/`)

Domain & state:
- `domain/` — frozen dataclasses: `Position`, `TradeLogEntry` (now with `tradebookid: int = 0`), `PaperState`, `DailyCounters`, `RiskSettings`, `SignalSettings`, `GuardSettings`, `CycleSettings`, `PortfolioSnapshot`. Also `domain/scorer.py` (`SymbolScorer` protocol).
- `state/session_bridge.py` — pure mappers between `st.session_state` dicts and the domain dataclasses.

Persistence:
- `persistence/paper_repo.py` — `PaperRepo` Protocol + `get_paper_repo()` factory. Reads `PAPER_REPO_BACKEND` env (or `config/database_config.json`), wraps SQLite with `_DualWriteSqlitePaperRepo` when `PAPER_REPO_DUAL_WRITE=1` / `PAPER_REPO_FALLBACK_JSON=1`.
- `persistence/json_paper_repo.py` — `JsonPaperRepo` (default backend; reads/writes `outputs/simple_paper_state*.json`).
- `persistence/sqlite_paper_repo.py` — `SqlitePaperRepo` (`.database/paper_state.db`). Schema: `paper_state`, `positions`, `prices`, `trade_log` (now includes `tradebookid INTEGER NOT NULL DEFAULT 0` plus an idempotent `ALTER TABLE ADD COLUMN` migration shim), `daily_counters`. `ui_config` / `agent_memory` stored as JSON `TEXT`.
- `persistence/db_storage.py` — `open_sqlite_connection()` helper (row factory, foreign keys, WAL). Retained.

Trading cycle (gated by `USE_TRADING_CYCLE=1`):
- `cycle/ports.py` — protocols: `Clock`, `SignalsProvider`, `Broker`, `LogHistory`, `Repo`, `PriceStore`.
- `cycle/services.py` — `Services` aggregate (clock, signals, broker, log_history, repo, prices, scorer).
- `cycle/runner.py` — `run_cycle(state, services, settings)` driving the step pipeline.
- `cycle/factory.py` — composition root that builds `Services` from Streamlit session state.
- `cycle/scoring.py` — bias overlay scoring (extracted from market learning during phase 7).
- `cycle/steps/` — `prices.py`, `gates.py`, `entries.py`, `exits.py`, `counters.py`.
- `cycle/entry/` — `cooldown.py`, `idle_fallback.py`, `sizing.py` (entry sub-rules).
- `cycle/adapters/` — Streamlit-backed adapters: `streamlit_broker`, `session_repo`, `session_prices`, `dashboard_signals`, `live_clock`, `log_history`.

Backtest (gated by `BACKTEST_USE_CYCLE=1`):
- `backtest/__init__.py`, `backtest/cycle.py` — `run_backtest_via_cycle()` drives `run_cycle` over historical bars.
- `backtest/settings.py`, `backtest/sizer.py`, `backtest/steps.py` — backtest-specific helpers.
- `backtest/adapters/` — `clock.py`, `broker.py`, `signals.py` (historical-bar implementations of the cycle ports).

Optimization:
- `optimization/` — split out from the legacy `optimizer.py` monolith. `trade_history.py` (CSV normalization), `features.py` (walk-forward feature engineering), `reports.py` (export). `optimizer.py` at the package root is a thin shim that re-exports with `DeprecationWarning`.

ML / scoring:
- `ml/null_scorer.py`, `ml/sklearn_scorer.py`, `ml/bias.py` — `SymbolScorer` implementations. Disabled with `DISABLE_ML_SCORER=1`.
- `market_learning.py` — legacy module; the active scorer code path lives under `ml/` + `cycle/scoring.py`.

Views (Streamlit, gated previously by `USE_SIMPLE_VIEWS*`; views are now the only path — the flag family was removed in phase-8b slice 6):
- `views/simple_top_panels.py`, `simple_activity_and_logs.py`, `simple_signals_tables.py`, `simple_tomorrow_plan.py`, `simple_portfolio_metrics.py`, `simple_auto_refresh.py` — pure-input view modules.
- `views/theme.py` — theme injection used by `app.py`.
- AST-guard tests (`tests/test_phase8b_top_panels.py`) prevent any `views/*.py` from importing `dashboard_simple` or reading `st.session_state`.

Shared engine (used by both dashboard and CLI):
- `config.py` (`TradingConfig`), `settings.py` (`AppSettings` + `load_app_settings()`, gated by `USE_APP_SETTINGS=1`).
- `data.py`, `nse_intraday.py`, `finnhub_client.py`, `quotes/` — market data + quote service.
- `strategy.py`, `simple_signals.py`, `ranking.py`, `charges.py` — strategy + scoring + cost helpers.
- `backtest.py` (legacy module-level callable), `sweep.py` — backtest engine + parameter sweep runner.

## Feature flags in play (today)

All default OFF unless noted; production behavior is preserved when unset.

- `USE_PAPER_REPO=1` — route dashboard load/save through the `PaperRepo` factory.
- `USE_TRADING_CYCLE=1` — route auto-trading through `cycle.runner.run_cycle`.
- `USE_APP_SETTINGS=1` — read `AppSettings` instead of the inline JSON config block.
- `BACKTEST_USE_CYCLE=1` — drive legacy backtest via `run_backtest_via_cycle()`.
- `DISABLE_ML_SCORER=1` — force `NullSymbolScorer` (skip sklearn + bias overlay).
- `PAPER_REPO_BACKEND=json|sqlite` — phase-9 backend selector. Default `json` today.
- `PAPER_REPO_DUAL_WRITE=1` — when on SQLite, also write the JSON file. Migration aid; retire once SQLite is the default.
- `PAPER_REPO_FALLBACK_JSON=1` — when on SQLite, fall back to JSON on a load miss. Migration aid; same retirement plan.

The `USE_SIMPLE_VIEWS` and `USE_SIMPLE_VIEWS_TOP_PANELS` flags were removed in phase-8b slice 6 — the views package is now the only render path.

## Runtime Flow (high level)

Supported dashboard:
1. `streamlit run app.py` — `app.py` injects the theme and calls `dashboard_simple.render_simple_dashboard(standalone=False)`.
2. `dashboard_simple.py` owns layout, sidebar, market selection, and lifecycle hooks. UI panels are rendered via `views/simple_*` modules; trade execution runs through `cycle.runner.run_cycle()` (when `USE_TRADING_CYCLE=1`) over a `Services` graph built by `cycle.factory.build_services_from_session()`.
3. State persistence goes through `persistence.paper_repo.get_paper_repo()` (when `USE_PAPER_REPO=1`); the factory chooses `JsonPaperRepo` (default) or `SqlitePaperRepo` based on `PAPER_REPO_BACKEND` and may wrap SQLite in `_DualWriteSqlitePaperRepo` for the migration window.
4. Quotes come from `stockmarket.quotes.get_default_quote_service()`; ranking via `ranking.py` + `simple_signals.py`; costs via `charges.py`. Optional ML scoring lazy-loads `ml/sklearn_scorer.py` and `ml/bias.py`.

CLI research:
1. `python -m stockmarket <command>` parses in `cli.py` against `config.json` (`TradingConfig`).
2. `backtest`/`signals`/`sweep`/`replay-best` use `data.py` → `strategy.py` → `backtest.py` (or `run_backtest_via_cycle()` when `BACKTEST_USE_CYCLE=1`) → `sweep.py`.
3. `optimize` uses `stockmarket.optimization` and writes reports under `outputs/`.

## Testing surface

The pytest suite (`source .venv/bin/activate && python -m pytest tests/ -q`) currently reports 160 passed, with known deprecation warnings only (the `stockmarket.optimizer` shim and a couple of pandas/datetime deprecations). Notable test groups:

- `test_dashboard_simple.py`, `test_simple_signals.py`, `test_ranking.py`, `test_charges.py`, `test_data_batch.py`, `test_quotes.py` — pre-existing helper / behavioral tests.
- `test_domain_bridge.py` — `state/session_bridge.py` round-trips.
- `test_json_paper_repo.py`, `test_sqlite_paper_repo.py`, `test_paper_repo_backend_selection.py` — persistence backends + factory.
- `test_migrate_paper_state.py` — JSON → SQLite migration script.
- `test_cycle_steps.py`, `test_cycle_parity.py`, `test_streamlit_broker.py` — trading cycle.
- `test_backtest_convergence.py` — phase-5 golden parity.
- `test_optimization_split.py`, `test_optimizer_market.py` — phase-6 + US optimizer market awareness.
- `test_market_learning_port.py` — phase-7 scorer port.
- `test_app_settings.py` — phase-4 unified settings.
- `test_phase8_ui_thinning.py`, `test_phase8b_*` — phase-8/8b view extractions and AST guards.

## Practical maintenance notes

- Prefer new feature work in `dashboard_simple.py` (orchestration only), `views/simple_*` (rendering), `cycle/steps/` (decisions), and `domain/` / `state/` (data shapes). Keep `views/*.py` free of `st.session_state` and `dashboard_simple` imports — the AST guard tests will flag violations.
- Do not extend `dashboard.py` (deprecation stub) or `optimizer.py` (shim with `DeprecationWarning`); migrate callers off the shim opportunistically before removing it.
- When adding a column to `trade_log` (or any `paper_state.db` table), add an `ALTER TABLE ADD COLUMN IF MISSING` shim in `_ensure_schema` (see `tradebookid` for the pattern).
- Generated paths (`.cache/`, `outputs/`, `.database/`) are never committed; treat them as runtime state.
