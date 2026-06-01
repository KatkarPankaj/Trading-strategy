# Codebase Structure Review

Last reviewed: 2026-05-21 (post-architecture-migration).

This document describes the repository as it stands after the phase 1–9 architecture migration on `arch-migration-plan`. The legacy MVC stack (`controllers/`, `models/`, `views/components.py`, the MVC `persistence/factory.py` + `file_storage.py` pair) has been deleted; the supported runtime is now `app.py` → `dashboard_simple.render_simple_dashboard()` over the new `domain` / `state` / `cycle` / `persistence` / `backtest` / `optimization` / `ml` / `views` packages.

Companion docs: `CODEBASE_DATAFLOW.md` (runtime sequencing + mermaid diagrams), `MIGRATION.md` (phase ledger), `README.md` (install/run/CLI reference), `ML_MARKET_LEARNING.md` (deep dive on the scorer subsystem).

## Surfaces

- `app.py` — supported Streamlit launcher. Injects the theme and calls `dashboard_simple.render_simple_dashboard(standalone=False)`.
- `dashboard_simple.py` (repo root) — primary live paper-trading simulator. Owns the Streamlit UI shell, sidebar, market selection, and orchestration; trade execution and view rendering are delegated to the cycle and views packages (see below).
- `src/stockmarket/cli.py` — command-line research interface (`backtest`, `signals`, `sweep`, `replay-best`, `optimize`).
- `src/stockmarket/webapp.py` — secondary Streamlit research UI; retained behind a deprecation banner.
- `dashboard.py` (repo root) — 31-line deprecation stub. `streamlit run dashboard.py` shows a banner and stops; importers won't `ImportError`. Do not extend.

## Top-Level Layout

- `README.md` — user-facing setup, install, and full CLI reference.
- `MIGRATION.md` — consolidated record of phases 1–9 (replaces the per-phase plan markdowns).
- `CODEBASE_STRUCTURE.md` / `CODEBASE_DATAFLOW.md` — this and its sibling.
- `ML_MARKET_LEARNING.md` — deep dive on the historical-bootstrap + personal-trade ensemble scorer.
- `app.py`, `dashboard_simple.py`, `dashboard.py` — Streamlit entrypoints described above.
- `config.json` / `config.example.json` — CLI/backtest `TradingConfig` files.
- `dashboard_simple_data_config.json` — simple-dashboard data-fetch settings (e.g. `us_market_data_batch_size`).
- `config/` — JSON config used by `AppSettings` and the persistence factory: `app_config.json`, `market_config.json` (NSE/US watchlists, session times, intraday charges), `trading_config.json`, `database_config.json` (`storage.paper_state_database_path`).
- `src/stockmarket/` — reusable package, see below.
- `scripts/` — operational scripts.
- `tests/` — pytest suite (160 tests at the time of writing).
- `.cache/market_data/` — intraday OHLCV pickle cache (untracked).
- `outputs/` — generated artifacts: `paper_trade_history.csv`, `daily_pnl_history.csv`, `sweep_*.csv`, `optimize_*_recommendations.json` / `*_symbol_scores.csv` / `*_feature_scores.csv` / `*_model_feature_importance.csv` / `*_enriched_trades.csv`, `market_learning_model*.pkl`, `market_data_cache/*.csv`.
- `.database/paper_state.db` — SQLite paper-state DB (untracked).

## Package Layout (`src/stockmarket/`)

### Domain & state

- `domain/types.py` — frozen dataclasses:
  - `Position(qty, avg, stop, target)`
  - `TradeLogEntry(ts, symbol, side, qty, price, charges, realized_delta, reason, cash_after, tradebookid: int = 0)` — `tradebookid` is the new broker-tradebook hook, default `0` until a real broker feed is wired.
  - `PaperState(start_capital, cash, realized, charges, holdings, shorts, prices, log, ui_config, agent_memory, market)`
  - `DailyCounters(day, peak_open_pnl, peak_open_pnl_day, profit_guard_triggered_day, profit_ladder_day, profit_ladder_armed, profit_ladder_pullback_started, profit_ladder_exited_day)`
  - `RiskSettings`, `SignalSettings`, `GuardSettings`, `CycleSettings` — frozen settings bags consumed by the cycle pipeline.
  - `PortfolioSnapshot` — immutable view-model used by `views/simple_portfolio_metrics.py`; caller pre-computes `equity_delta` and `net_realized` so the view stays formatting-only.
  - `Side = Literal["BUY", "SELL", "SHORT", "COVER"]`.
- `domain/scorer.py` — `SymbolScorer` Protocol consumed by the cycle.
- `state/session_bridge.py` — pure mappers between `st.session_state` dicts and the domain dataclasses. The bridge is the only place that touches the legacy dict shape.

### Persistence

- `persistence/paper_repo.py` — `PaperRepo` Protocol + `get_paper_repo(path, market)` factory. Always returns `SqlitePaperRepo`; the path arg is retained only for old call compatibility.
- `persistence/sqlite_paper_repo.py` — `SqlitePaperRepo` (`.database/paper_state.db`). Schema: `paper_state`, `positions`, `prices`, `trade_log` (includes `tradebookid INTEGER NOT NULL DEFAULT 0` plus an idempotent `ALTER TABLE ADD COLUMN` migration shim), `daily_counters`. `ui_config` / `agent_memory` stored as JSON `TEXT`.
- `persistence/db_storage.py` — `open_sqlite_connection()` helper (row factory, foreign keys, WAL). Retained for SQLite plumbing reuse.

### Trading cycle (gated by `USE_TRADING_CYCLE=1`)

- `cycle/ports.py` — protocols: `Clock`, `SignalsProvider`, `Broker`, `LogHistory`, `Repo`, `PriceStore`.
- `cycle/services.py` — `Services` aggregate (clock, signals, broker, log_history, repo, prices, scorer).
- `cycle/runner.py` — `run_cycle(state, services, settings)` driving the step pipeline.
- `cycle/factory.py` — composition root that builds `Services` from Streamlit session state.
- `cycle/scoring.py` — bias-overlay scoring (extracted from market learning in phase 7).
- `cycle/context.py` — shared in-flight cycle context.
- `cycle/steps/` — `prices.py`, `gates.py`, `entries.py`, `exits.py`, `counters.py`.
- `cycle/entry/` — `cooldown.py`, `idle_fallback.py`, `sizing.py` (entry sub-rules).
- `cycle/adapters/` — Streamlit-backed adapters: `streamlit_broker`, `session_repo`, `session_prices`, `dashboard_signals`, `live_clock`, `log_history`.

### Backtest (cycle pipeline default)

- `backtest/__init__.py`, `backtest/cycle.py` — `run_backtest_via_cycle()` drives `run_cycle` over historical bars.
- `backtest/settings.py`, `backtest/sizer.py`, `backtest/steps.py` — backtest-specific helpers.
- `backtest/adapters/` — `clock.py`, `broker.py`, `signals.py` (historical-bar implementations of the cycle ports).

### Optimization

- `optimization/trade_history.py` — CSV normalization plus SQLite-loaded paper trade-log normalization for dashboard optimizer runs.
- `optimization/features.py` — walk-forward feature engineering (candle strength, wick/body, prev-day breakout, ATR, NIFTY vs VWAP, etc.).
- `optimization/reports.py` — exports (`*_recommendations.json`, `*_symbol_scores.csv`, `*_feature_scores.csv`, `*_model_feature_importance.csv`, `*_enriched_trades.csv`).

### ML / scoring

- `ml/null_scorer.py`, `ml/sklearn_scorer.py`, `ml/bias.py` — `SymbolScorer` implementations. Disabled with `DISABLE_ML_SCORER=1`.
- `market_learning.py` (package root) — legacy module containing the historical-bootstrap pipeline + `MarketLearningModel` ensemble. The active scorer code path lives under `ml/` + `cycle/scoring.py`. Deeper subsystem doc: `ML_MARKET_LEARNING.md`.

### Views (Streamlit)

Views are now the only render path; the `USE_SIMPLE_VIEWS*` flag family was removed in phase-8b slice 6. Each module takes pure-input dataclasses (no `st.session_state`, no `dashboard_simple` imports); AST-guard tests in `tests/test_phase8b_top_panels.py` block regressions.

| Module | Purpose |
|---|---|
| `views/simple_top_panels.py` | Auto-trade actions, clean-trades caption, AI best action, optimizer summary. |
| `views/simple_activity_and_logs.py` | Activity feed + app/engine logs panel. |
| `views/simple_signals_tables.py` | Live top-5 buy/sell tables fragment + error banners. |
| `views/simple_tomorrow_plan.py` | Tomorrow Plan expander (next-session plan rendering). |
| `views/simple_portfolio_metrics.py` | Portfolio metrics block (consumes `PortfolioSnapshot`). |
| `views/simple_auto_refresh.py` | Footer auto-refresh tick. |
| `views/theme.py` | Theme injection used by `app.py`. |

### Shared engine (used by both dashboard and CLI)

- `config.py` — `TradingConfig` dataclass.
- `settings.py` — `AppSettings` + `load_app_settings()`, gated by `USE_APP_SETTINGS=1`.
- `data.py`, `nse_intraday.py`, `finnhub_client.py` — market data fetch + caching.
- `quotes/` — `QuoteService` + providers: `nse.py`, `finnhub_quotes.py`, `ttl_cache.py`, `types.py`, `service.py`.
- `strategy.py` — ORB + VWAP signal generation (`add_strategy_columns()`).
- `simple_signals.py` — dashboard signal helpers.
- `ranking.py` — buy/sell score ranking.
- `charges.py` — intraday brokerage / GST / STT / slippage cost model.
- `backtest.py` (module-level callable at package root) — legacy backtest entrypoint still used by `sweep.py` and the CLI fallback path.
- `sweep.py` — parameter sweep runner.
- `utils/` — shared helpers.

## Active design patterns

The patterns absorbed from the (now-deleted) `DesignPattern.md` that still describe live code. Use these as a guide when extending; they're enforced or assumed throughout the post-migration packages.

- **Ports & adapters (hexagonal) at the boundaries.** `cycle/ports.py` declares `Clock`, `SignalsProvider`, `Broker`, `LogHistory`, `Repo`, `PriceStore`; Streamlit-backed implementations live under `cycle/adapters/` and historical-bar implementations under `backtest/adapters/`. The cycle core doesn't import Streamlit.
- **Dataclass-as-domain.** `domain/types.py` holds frozen dataclasses (`Position`, `TradeLogEntry`, `RiskSettings`, `SignalSettings`, `GuardSettings`, `CycleSettings`, `PortfolioSnapshot`) plus the mutable `PaperState`. Domain code never imports Streamlit, persistence, or sklearn.
- **Repository pattern.** `PaperRepo` Protocol with the SQLite implementation; `get_paper_repo()` is the single composition site for paper state.
- **Dependency injection via `Services`.** `cycle.factory.build_services_from_session()` is the composition root; `run_cycle(state, services, settings)` is pure-args. Tests construct fake `Services` rather than monkeypatching modules.
- **Strategy / scorer protocol.** `SymbolScorer` Protocol with three swappable implementations (`NullSymbolScorer`, `SklearnSymbolScorer`, `BiasOverlayScorer`); `DISABLE_ML_SCORER=1` selects null.
- **Pipeline of small steps.** `cycle/steps/` (prices → gates → entries → exits → counters) plus `cycle/entry/` sub-rules; each step has a single responsibility and is testable in isolation.
- **State bridge.** `state/session_bridge.py` is the only place that converts between `st.session_state` dicts and domain dataclasses; everything downstream consumes dataclasses.
- **View purity (AST-guarded).** `views/simple_*.py` modules take pure inputs and must not import `dashboard_simple` or read `st.session_state`. `tests/test_phase8b_top_panels.py` walks the AST and fails the suite on violations.
- **Strangler / feature-flag rollout.** New code paths landed behind default-off flags (`USE_TRADING_CYCLE`, `USE_APP_SETTINGS`, `BACKTEST_USE_CYCLE`, etc.) so production behavior was preserved at each step. Flags retire once the new path becomes the only path.
- **Streamlit observer pattern (at the edge only).** Session-state mutations trigger reruns; the dashboard owns this. Cycle/domain/persistence code is rerun-agnostic.

## Feature flags in play

All default OFF unless noted; production behavior is preserved when unset.

- `USE_TRADING_CYCLE=1` — route auto-trading through `cycle.runner.run_cycle`.
- `USE_APP_SETTINGS=1` — read `AppSettings` (resolved from `config/market_config.json`) instead of the inline JSON config block.
- `DISABLE_ML_SCORER=1` — force `NullSymbolScorer` (skip sklearn + bias overlay).

External (not Cursor-managed) but relevant:

- `FINNHUB_API_KEY` — required for US symbol quotes/candles.
- `PYTHONPATH=src` — set by all documented run commands so `python -m stockmarket …` and `streamlit run app.py` resolve the package.

The `USE_SIMPLE_VIEWS` / `USE_SIMPLE_VIEWS_TOP_PANELS` / `_ACTIVITY` / `_TOMORROW_PLAN` / `_PORTFOLIO` / `_AUTO_REFRESH` flag family was removed in phase-8b slice 6 — the views package is now the only render path.

## Common commands

PowerShell shown (Windows is the documented environment). On macOS/Linux replace `$env:VAR = "…"` with `export VAR=…` and `.\.venv\Scripts\python.exe` with `.venv/bin/python`.

```powershell
# Setup
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item config.example.json config.json

# Launch the supported dashboard (loads dashboard_simple via app.py)
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m streamlit run app.py

# Dev: run dashboard_simple directly (same code, different port)
.\.venv\Scripts\python.exe -m streamlit run dashboard_simple.py --server.port 8507

# CLI: backtest / signals / sweep / replay-best / optimize
.\.venv\Scripts\python.exe -m stockmarket.cli backtest    --config config.json
.\.venv\Scripts\python.exe -m stockmarket.cli signals     --config config.json
.\.venv\Scripts\python.exe -m stockmarket.cli sweep       --config config.json
.\.venv\Scripts\python.exe -m stockmarket.cli replay-best --config config.json --rank 1
.\.venv\Scripts\python.exe -m stockmarket.cli optimize    --config config.json \
    --trade-file outputs/paper_trade_history.csv --lookback-trades 200

# Tests (160 passing as of this review)
.\.venv\Scripts\python.exe -m pytest tests/ -q
```

See `README.md` for full CLI flag reference (custom sweeps, replay filters, optimizer artifacts) and `ML_MARKET_LEARNING.md` for the ML model training flow.

## Runtime flow (high level)

Detailed mermaid sequencing lives in `CODEBASE_DATAFLOW.md`. The narrative:

Supported dashboard:
1. `streamlit run app.py` — `app.py` injects the theme and calls `dashboard_simple.render_simple_dashboard(standalone=False)`.
2. `dashboard_simple.py` owns layout, sidebar, market selection, and lifecycle hooks. UI panels are rendered via `views/simple_*` modules; trade execution runs through `cycle.runner.run_cycle()` (when `USE_TRADING_CYCLE=1`) over a `Services` graph built by `cycle.factory.build_services_from_session()`.
3. State persistence goes through `persistence.paper_repo.get_paper_repo()` and SQLite at `.database/paper_state.db`.
4. Quotes come from `stockmarket.quotes.get_default_quote_service()`; ranking via `ranking.py` + `simple_signals.py`; costs via `charges.py`. Optional ML scoring lazy-loads `ml/sklearn_scorer.py` and `ml/bias.py`.

CLI research:
1. `python -m stockmarket <command>` parses in `cli.py` against `config.json` (`TradingConfig`).
2. `backtest`/`signals`/`sweep`/`replay-best` use `data.py` → `strategy.py` → `backtest.run_backtest()` (cycle pipeline) → `sweep.py`.
3. `optimize` uses `stockmarket.optimization` and writes reports under `outputs/`.

One auto-paper cycle, as ordered by `_auto_paper_cycle()` / `run_cycle()`:

1. Refresh holding prices.
2. Check market open / weekend.
3. Roll daily counters (peak PnL, ladder day, guard day).
4. Evaluate profit ladder state machine.
5. Evaluate profit guard state machine.
6. Process forced exits (SL / TP / time / ladder / guard).
7. Process signal-based exits.
8. Apply re-entry cooldowns and regime gates.
9. Rank candidates, size positions, apply allocation caps.
10. Place entries (long / short).
11. Record trades, update state, persist via the repo.

## Testing surface

The pytest suite (`source .venv/bin/activate && python -m pytest tests/ -q`) should be run after changes. Notable test groups:

- `test_dashboard_simple.py`, `test_simple_signals.py`, `test_ranking.py`, `test_charges.py`, `test_data_batch.py`, `test_quotes.py` — pre-existing helper / behavioral tests.
- `test_domain_bridge.py` — `state/session_bridge.py` round-trips.
- `test_sqlite_paper_repo.py`, `test_paper_repo_backend_selection.py` — SQLite persistence + factory.
- `test_cycle_steps.py`, `test_cycle_parity.py`, `test_streamlit_broker.py` — trading cycle.
- `test_backtest_convergence.py` — phase-5 golden parity.
- `test_optimization_split.py`, `test_optimizer_market.py` — phase-6 + US optimizer market awareness.
- `test_market_learning_port.py` — phase-7 scorer port.
- `test_app_settings.py` — phase-4 unified settings.
- `test_phase8_ui_thinning.py`, `test_phase8b_*` — phase-8/8b view extractions and AST guards.

## Practical maintenance notes

- Prefer new feature work in `dashboard_simple.py` (orchestration only), `views/simple_*` (rendering), `cycle/steps/` (decisions), and `domain/` / `state/` (data shapes). Keep `views/*.py` free of `st.session_state` and `dashboard_simple` imports — the AST guard tests will flag violations.
- Do not extend `dashboard.py` (deprecation stub).
- When adding a column to `trade_log` (or any `paper_state.db` table), add an `ALTER TABLE ADD COLUMN IF MISSING` shim in `_ensure_schema` (see `tradebookid` for the pattern).
- For a fresh local paper-trading start, remove `.database/paper_state.db`. Old `outputs/simple_paper_state*.json` files can also be removed; they are not live storage.
- Generated paths (`.cache/`, `outputs/`, `.database/`) are never committed; treat them as runtime state.
- When introducing a new boundary, add a Protocol to `cycle/ports.py` (or a sibling) and a fake adapter for tests rather than monkeypatching the concrete one.
