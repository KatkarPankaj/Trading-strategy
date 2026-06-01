# Architecture Migration — Consolidated Record

This is the post-flight summary of the architecture migration that ran on branch `arch-migration-plan`. It replaces the per-phase plan markdowns (`01-…md` through `09-…md`, `08b-ui-thinning.md`, `arch migration plan.md`, and the running `HANDOVER.md`).

## Context

The pre-migration codebase had two overlapping architectures: a "primary" Streamlit path (`app.py` → `dashboard_simple.py` — a ~4,500-line module that owned UI shell, sidebar widgets, market selection, ranking, scoring, auto-trading, persistence, and ML hooks) and a "secondary" MVC stack (`controllers/`, `models/`, `views/components.py`, the `persistence/factory.py` + `file_storage.py` pair) that was real code but not on the runtime path. The dashboard's auto-trading function (`_auto_paper_cycle()`) was a 500+ line monolith that mixed Streamlit session state, trading rules, persistence, and ML scoring. Plain MVC mapped poorly onto a polling pipeline with embedded state machines (profit ladder, profit guard, daily counters, re-entry cooldowns).

The goal: re-anchor the codebase on **hexagonal / ports-and-adapters** at the boundaries (UI, market data, persistence, ML), a **`TradingCycle` pipeline** for live and historical decision loops, a pure **domain core**, and **strategy objects** for scoring, ranking, sizing. MVC-style separation kept its place at the UI edge only. Every behavior change shipped behind a feature flag, with parity tests where applicable, so production behavior was preserved at every step.

## Phase ledger

| Phase | What shipped | Commit(s) | Flag introduced |
|-------|--------------|-----------|-----------------|
| 01 — Foundation: domain types + session bridge | Frozen dataclasses (`Position`, `TradeLogEntry`, `PaperState`, `DailyCounters`); `state/session_bridge.py` mappers; fixtures + regression tests pinning the JSON state shape. | `60bf751` (rolled into 1–3) | — |
| 02 — Persistence repository | `PaperRepo` Protocol and dashboard `_save_state` / `_init_state` routed through a repo boundary. The initial JSON backend has since been retired. | `60bf751` | retired |
| 03 — Cycle pipeline extraction | `cycle/` package: ports, `Services`, `runner.run_cycle`, ordered steps (`prices`, `gates`, `entries`, `exits`, `counters`), entry sub-rules (`cooldown`, `idle_fallback`, `sizing`), Streamlit-backed adapters (`streamlit_broker`, `session_repo`, `session_prices`, `dashboard_signals`, `live_clock`, `log_history`). Strangler swap of `_auto_paper_cycle()`. | `60bf751` | `USE_TRADING_CYCLE=1` |
| 04 — Config reconciliation | `AppSettings` dataclass + `load_app_settings()`; Streamlit-free `market_controller`; JSON US entry cutoff fixed at 13:30. | `13bcf1f` | `USE_APP_SETTINGS=1` |
| (intermezzo) US optimizer hang fix | Market-aware benchmark (SPY for US, ^NSEI for NSE), tighter retries, per-market last-run timer, skip auto-run on market switch. | `5d95af4` | — |
| 05 — Backtest convergence | `stockmarket.backtest/` package: historical adapters (`clock`, `broker`, `signals`), `run_backtest_via_cycle()`, golden parity tests. | `1517c28` | `BACKTEST_USE_CYCLE=1` |
| 06 — Optimizer split | `stockmarket/optimization/` subpackage (`trade_history`, `features`, `reports`); legacy `stockmarket.optimizer` shim removed. | `cc65e41` | — |
| 07 — Market learning port | `SymbolScorer` Protocol; `NullSymbolScorer`, `SklearnSymbolScorer`, `BiasOverlayScorer`; scoring extracted to `cycle/scoring.py`; injection via `Services`. | `92a2d14` | `DISABLE_ML_SCORER=1` |
| 08 — UI thinning (scoped) | Live tables fragment → `views/simple_signals_tables.py`; cycle composition → `cycle/factory.py`; legacy banners on `dashboard.py` and `webapp.py`; AST guards (no `st.session_state`, no `dashboard_simple` imports inside `views/*.py`). | `9c3fc24` | `USE_SIMPLE_VIEWS=1` (master) |
| 08b s1 — Top read-only panels | `views/simple_top_panels.py` (auto-trade actions, clean-trades caption, AI best action, optimizer summary). | `7e18b9d` | `USE_SIMPLE_VIEWS_TOP_PANELS=1` |
| 08b s2 — Activity & logs panel | `views/simple_activity_and_logs.py`. | `8e9700d` | `USE_SIMPLE_VIEWS_ACTIVITY=1` |
| 08b s3 — Tomorrow Plan expander | `views/simple_tomorrow_plan.py`. | `cd4ca3f` | `USE_SIMPLE_VIEWS_TOMORROW_PLAN=1` |
| 08b s4 — Portfolio metrics block | `views/simple_portfolio_metrics.py`; new `PortfolioSnapshot` dataclass; caller pre-computes `equity_delta` and `net_realized` to keep the view formatting-only. | `a1e2bd4` | `USE_SIMPLE_VIEWS_PORTFOLIO=1` |
| 08b — Auto-refresh footer | `views/simple_auto_refresh.py` extracted from the footer block. | `727a721` | `USE_SIMPLE_VIEWS_AUTO_REFRESH=1` |
| 08b s5 — Drop legacy live-tables fragment | Removed `_fragment_live_tables_and_errors` (~300 LOC). | `3d6e828` | — |
| 08b s6 — Final cleanup | Flipped views to be the only path; removed every inline legacy branch and the entire `USE_SIMPLE_VIEWS*` flag family. | `100b5c2` | — (flag family deleted) |
| 08b — Docs + legacy entrypoints | Replaced legacy `dashboard.py` with a 31-line deprecation stub; dropped legacy MVC docs/instructions; pointed users at `app.py`. | `9abd860`, `faae202` | — |
| 08b — Orphan deletions | Deleted `controllers/`, `models/`, `persistence/file_storage.py`, `persistence/factory.py`; deleted `views/components.py`; dropped dead `yesterday_net` + disambiguated `_hhmm_to_time`. | `62f0979`, `1d15e0f`, `c39ec91` | — |
| 09 a — `SqlitePaperRepo` | `persistence/sqlite_paper_repo.py`; schema for `paper_state`, `positions`, `prices`, `trade_log`, `daily_counters`; `ui_config` / `agent_memory` as JSON `TEXT`. | `cbcf4c8` | — |
| 09 b — JSON migration script | A one-shot JSON → SQLite migration existed briefly, then was removed when the project chose a fresh SQLite start. | `c94eace` | retired |
| 09 c — SQLite-only factory | `persistence/paper_repo.py::get_paper_repo()` always returns `SqlitePaperRepo`; optional DB path comes from `storage.paper_state_database_path`. | `3a2bc51` | — |
| Post-09 — `tradebookid` field | `tradebookid: int = 0` on `TradeLogEntry`; column added to `trade_log` DDL with idempotent `ALTER TABLE ADD COLUMN` shim; INSERT/SELECT/marshalling helpers updated; defensive read in session bridge; field appended to the dashboard's plain-dict log row. | `87757c6` | — |

## Deferred (not picked up; revisit only with a new plan)

- **Sidebar extraction.** 47 pre-initialized widget keys, market-switch side effects, a full-auto override block that mutates widget values + writes `s_ui_config` + calls `_save_state()`, and the learning-apply re-rank block. Splitting forces marshaling adapters and risks widget-key drift; the cost outweighs the win.
- **ML Market Learning panel.** Mid-render `_save_state()`, lazy imports of scorer/state/model paths, sklearn-cache decorators. Forced indirection is worse than the current shape — the port plumbing landed in phase 07 but the panel still renders inline.
- **Composition root reduction beyond what slice 6 removed.** `dashboard_simple.py` is still ~3.6k–3.8k lines. The user explicitly relaxed the line-budget constraint; don't extract just to shrink. Composition that survives is in the file because moving it would force adapter classes that hide more than they reveal.

## Deprecated / removed

- **Legacy `dashboard.py`.** Replaced with a 31-line deprecation stub (`9abd860`). `streamlit run dashboard.py` shows a banner; old `from dashboard import render_complex_dashboard` callers won't `ImportError`.
- **MVC stack.** `src/stockmarket/controllers/`, `src/stockmarket/models/`, and `src/stockmarket/views/components.py` are gone. The persistence-side casualties (`persistence/file_storage.py`, `persistence/factory.py`) went with them; `db_storage.py` survived as a SQLite connection helper.
- **`USE_SIMPLE_VIEWS*` flag family.** Master `USE_SIMPLE_VIEWS` plus the per-slice overrides (`USE_SIMPLE_VIEWS_TOP_PANELS`, `_ACTIVITY`, `_TOMORROW_PLAN`, `_PORTFOLIO`, `_AUTO_REFRESH`) were all deleted in phase-8b slice 6; views are the only render path now.
- **JSON paper-state storage.** `JsonPaperRepo`, `PAPER_REPO_BACKEND`, `USE_PAPER_REPO`, and the JSON migration script are gone. SQLite is the only runtime paper-state store.
- **`yesterday_net` helper** and the duplicate `_hhmm_to_time` overload — dead code dropped during phase-9 housekeeping (`c39ec91`).

## Final state

### File-tree shape (top of `src/stockmarket/`)

```
src/stockmarket/
├─ domain/             # frozen dataclasses + SymbolScorer protocol
├─ state/              # session_bridge.py (dict ↔ domain)
├─ persistence/        # PaperRepo + SqlitePaperRepo + db_storage
├─ cycle/              # ports, services, runner, factory, scoring, steps/, entry/, adapters/
├─ backtest/           # cycle.py, settings.py, sizer.py, steps.py, adapters/
├─ optimization/       # trade_history.py, features.py, reports.py
├─ ml/                 # null_scorer.py, sklearn_scorer.py, bias.py
├─ views/              # simple_top_panels, simple_activity_and_logs,
│                      # simple_signals_tables, simple_tomorrow_plan,
│                      # simple_portfolio_metrics, simple_auto_refresh, theme
├─ quotes/             # QuoteService + providers
├─ utils/              # shared helpers
├─ cli.py / __main__.py
├─ webapp.py
├─ data.py / nse_intraday.py / finnhub_client.py
├─ strategy.py / simple_signals.py / ranking.py / charges.py
├─ backtest.py / sweep.py
├─ market_learning.py
├─ optimization/       # trade_history, features, reports, run_intelligent_optimization
├─ config.py           # TradingConfig
└─ settings.py         # AppSettings + loader
```

Workspace root keeps `app.py`, `dashboard_simple.py`, the deprecation-stub `dashboard.py`, `config.json` / `config.example.json`, `dashboard_simple_data_config.json`, `config/`, `tests/`, `MIGRATION.md`, `CODEBASE_STRUCTURE.md`, `CODEBASE_DATAFLOW.md`, `README.md`.

### Feature flags still in play

All default OFF; production behavior is preserved when unset.

| Flag | Effect |
|------|--------|
| `USE_TRADING_CYCLE=1` | Route auto-trading through `cycle.runner.run_cycle`. |
| `USE_APP_SETTINGS=1` | Read `AppSettings` instead of the inline JSON config block. |
| `DISABLE_ML_SCORER=1` | Force `NullSymbolScorer` (skip sklearn + bias overlay). |

### Public packages

`stockmarket.domain`, `stockmarket.state`, `stockmarket.persistence`, `stockmarket.cycle` (+ `cycle.adapters`, `cycle.steps`, `cycle.entry`, `cycle.scoring`, `cycle.factory`), `stockmarket.backtest` (+ `backtest.adapters`), `stockmarket.optimization`, `stockmarket.ml`, `stockmarket.views`, `stockmarket.quotes`. Legacy modules still imported by callers: `stockmarket.config`, `stockmarket.settings`, `stockmarket.data`, `stockmarket.strategy`, `stockmarket.simple_signals`, `stockmarket.ranking`, `stockmarket.charges`, `stockmarket.backtest` (legacy callable), `stockmarket.sweep`, `stockmarket.market_learning`, `stockmarket.cli`, `stockmarket.webapp`.

## Known follow-ups

These were deliberately left for after the migration. Each points back at the code, since the per-phase plan markdowns are now gone.

- **`tradebookid` population.** The field is wired end-to-end with default `0`. Plug in a real broker tradebook source as a follow-up; consider an index or unique constraint only once non-zero values are flowing.

## Where to look for more detail

For full historical detail, consult git history on branch `arch-migration-plan` — every phase shipped as a discrete commit (or a small sequence of commits with shared prefixes), and the commit messages explain the *why* in addition to the *what*.
