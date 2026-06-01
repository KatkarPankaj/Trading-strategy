# Codebase Dataflow Diagrams

Last reviewed: 2026-05-21 (post-architecture-migration).

These Mermaid diagrams describe the runtime after the phase 1–9 migration on `arch-migration-plan`. The legacy MVC stack is gone; the supported runtime is `app.py` → `dashboard_simple.render_simple_dashboard()` over the `domain` / `state` / `cycle` / `persistence` / `views` packages.

Companion docs: `CODEBASE_STRUCTURE.md` (what exists and where), `MIGRATION.md` (phase ledger), `README.md` (install + CLI reference), `ML_MARKET_LEARNING.md` (scorer subsystem deep dive).

## Runtime entrypoints

`streamlit run app.py` is the supported launcher; `streamlit run dashboard_simple.py` is the dev-time alternative (same code, runs `render_simple_dashboard(standalone=True)`). `dashboard.py` is a 31-line deprecation stub.

```mermaid
flowchart TD
  User["User"]

  subgraph UI["Streamlit UI Entrypoints"]
    App["app.py\nsupported launcher"]
    Simple["dashboard_simple.py\nprimary simulator"]
    Legacy["dashboard.py\n31-line deprecation stub"]
    WebApp["src/stockmarket/webapp.py\nresearch UI (banner)"]
  end

  subgraph CLI["CLI Entrypoints"]
    Module["python -m stockmarket"]
    CliPy["src/stockmarket/cli.py"]
  end

  User -->|"streamlit run app.py"| App
  App --> Theme["src/stockmarket/views/theme.py"]
  App --> Simple

  User -. "streamlit run dashboard_simple.py (dev)" .-> Simple
  User -. "shows banner & stops" .-> Legacy
  User -.-> WebApp

  User -->|"python -m stockmarket ..."| Module
  Module --> CliPy
```

## Supported dashboard dataflow

```mermaid
flowchart TD
  App["app.py"] --> Simple["dashboard_simple.render_simple_dashboard"]

  subgraph Orchestration["dashboard_simple.py owns"]
    UIShell["Streamlit shell + sidebar"]
    MarketSelect["market selection (NSE / US)"]
    AutoRun["auto-trading orchestration"]
  end

  Simple --> UIShell
  UIShell --> MarketSelect

  subgraph Views["views/ (UI rendering)"]
    Top["simple_top_panels"]
    Activity["simple_activity_and_logs"]
    Tables["simple_signals_tables"]
    Tomorrow["simple_tomorrow_plan"]
    Portfolio["simple_portfolio_metrics"]
    Refresh["simple_auto_refresh"]
  end

  Simple --> Top
  Simple --> Activity
  Simple --> Tables
  Simple --> Tomorrow
  Simple --> Portfolio
  Simple --> Refresh

  subgraph Cycle["cycle/ (trade execution, USE_TRADING_CYCLE=1)"]
    Factory["cycle.factory.build_services_from_session"]
    Runner["cycle.runner.run_cycle"]
    Steps["steps/: prices, gates, entries, exits, counters"]
    Adapters["adapters/: streamlit_broker, session_repo,\nsession_prices, dashboard_signals,\nlive_clock, log_history"]
    Scoring["cycle/scoring.py"]
  end

  AutoRun --> Factory
  Factory --> Runner
  Runner --> Steps
  Runner --> Adapters
  Runner --> Scoring

  subgraph Bridge["state/"]
    SessionBridge["session_bridge.py\n(dict ⇄ domain dataclasses)"]
  end

  subgraph Domain["domain/"]
    Types["types.py: PaperState,\nTradeLogEntry (tradebookid),\nPosition, DailyCounters,\nRiskSettings/SignalSettings/GuardSettings/\nCycleSettings, PortfolioSnapshot"]
    Scorer["scorer.py: SymbolScorer protocol"]
  end

  Simple --> SessionBridge
  Adapters --> SessionBridge
  SessionBridge --> Types
  Scoring --> Scorer

  subgraph Persistence["persistence/"]
    Factory2["paper_repo.get_paper_repo()"]
    SqliteRepo["SqlitePaperRepo"]
  end

  Adapters --> Factory2
  Factory2 --> SqliteRepo

  subgraph ML["ml/ (gated by DISABLE_ML_SCORER)"]
    Null["NullSymbolScorer"]
    Sklearn["SklearnSymbolScorer"]
    Bias["BiasOverlayScorer"]
  end

  Scoring --> Null
  Scoring --> Sklearn
  Scoring --> Bias

  subgraph DataPath["Quotes & data"]
    Quotes["stockmarket.quotes.QuoteService"]
    SimpleSignals["simple_signals"]
    Charges["charges"]
    Ranking["ranking"]
  end

  Simple --> Quotes
  Simple --> SimpleSignals
  Simple --> Charges
  Simple --> Ranking
  Adapters --> Quotes

  subgraph Files["Paper state"]
    Sqlite[".database/paper_state.db"]
  end

  SqliteRepo <--> Sqlite
```

### One auto-paper cycle, in order

Whether driven by the legacy `_auto_paper_cycle()` body or by `cycle.runner.run_cycle()` (when `USE_TRADING_CYCLE=1`), each polling tick executes the same ordered pipeline. The cycle package makes the order explicit; the legacy path inlines it.

```mermaid
flowchart LR
  Tick["dashboard tick"] --> S1["1. refresh holding prices\n(cycle/steps/prices.py)"]
  S1 --> S2["2. market-open / weekend gate"]
  S2 --> S3["3. roll daily counters\n(cycle/steps/counters.py)"]
  S3 --> S4["4. profit ladder state machine"]
  S4 --> S5["5. profit guard state machine"]
  S5 --> S6["6. forced exits: SL / TP / time /\nladder / guard / square-off\n(cycle/steps/exits.py)"]
  S6 --> S7["7. signal-based exits"]
  S7 --> S8["8. re-entry cooldown + regime gate\n(cycle/entry/cooldown.py)"]
  S8 --> S9["9. rank + effective scores\n(_rank_signals_for_cycle rank only;\napply_scorer in entries/exits;\ncycle/entry/sizing.py)"]
  S9 --> S10["10. place entries (long/short)\n(cycle/steps/entries.py +\nstreamlit_broker adapter)"]
  S10 --> S11["11. record trades, mutate state,\nPaperRepo.save()"]
```

### View layer boundary

Views are pure-input modules. They take dataclasses or simple primitives, render Streamlit widgets, and must not import `dashboard_simple` or touch `st.session_state`. Violations are caught by AST walkers in `tests/test_phase8b_top_panels.py`.

```mermaid
flowchart LR
  Simple["dashboard_simple.py\n(collects state, computes derived values)"]
  Domain["domain/types.py\n(PortfolioSnapshot etc.)"]
  Views["views/simple_*.py\n(pure input, formatting only)"]
  StreamlitOut["Streamlit widgets"]

  Simple --> Domain
  Domain --> Views
  Simple --> Views
  Views --> StreamlitOut

  Guard["tests/test_phase8b_top_panels.py\n(AST guard)"] -. "blocks st.session_state\nor dashboard_simple imports\ninside views/*.py" .-> Views
```

| Module | Inputs | Outputs |
|---|---|---|
| `views/simple_top_panels.py` | auto-trade flags, clean-trades stats, AI best-action payload, optimizer summary | top control + status row |
| `views/simple_activity_and_logs.py` | activity feed list, log buffer | activity / log panel |
| `views/simple_signals_tables.py` | top-5 buy / sell candidate rows, error banners | live tables fragment |
| `views/simple_tomorrow_plan.py` | next-session plan rows | Tomorrow Plan expander |
| `views/simple_portfolio_metrics.py` | `PortfolioSnapshot` (with pre-computed `equity_delta` + `net_realized`) | portfolio metrics block |
| `views/simple_auto_refresh.py` | refresh-interval seconds, current tick state | footer auto-refresh widget |
| `views/theme.py` | (none) | injected CSS / Streamlit theme |

## Persistence factory

`get_paper_repo(path, market)` is the single composition site for paper-state storage. It always returns `SqlitePaperRepo`; `path` is accepted only for compatibility with older callers.

```mermaid
flowchart LR
  Caller["dashboard / cycle adapter"] --> Factory["get_paper_repo(path, market)"]
  Factory --> SqliteRepo["SqlitePaperRepo\n.database/paper_state.db"]
```

## Backtest dataflow

CLI is the entry; `run_backtest()` routes through the cycle adapters (`run_backtest_via_cycle()`). `run_backtest_legacy()` remains for parity tests only.

```mermaid
flowchart LR
  User["User"] --> Cli["src/stockmarket/cli.py"]
  Cli --> Config["config.json → TradingConfig"]

  Cli --> BacktestCmd["backtest"]
  Cli --> SignalsCmd["signals"]
  Cli --> SweepCmd["sweep"]
  Cli --> ReplayCmd["replay-best"]
  Cli --> OptimizeCmd["optimize"]

  subgraph Engine["Research engine"]
    Data["data.py"]
    Strategy["strategy.py"]
    LegacyBacktest["backtest.py\n(legacy callable)"]
    Sweep["sweep.py"]
  end

  subgraph CycleBacktest["backtest/ (default)"]
    BTRun["run_backtest_via_cycle()"]
    BTAdapters["adapters/: clock,\nbroker, signals"]
    BTSteps["backtest/steps.py + sizer.py"]
  end

  subgraph Optim["optimization/"]
    Trades["trade_history.py"]
    Features["features.py"]
    Reports["reports.py"]
  end

  BacktestCmd --> Data
  Data --> Strategy
  Strategy --> LegacyBacktest
  Strategy --> BTRun
  BTRun --> BTSteps
  BTRun --> BTAdapters
  Sweep --> LegacyBacktest

  OptimizeCmd --> Trades
  Trades --> Features
  Features --> Reports

  subgraph Outputs["outputs/"]
    Trades2["trades_*.csv"]
    Sweeps["sweep_*.csv"]
    OptReports["*_recommendations.json,\n*_symbol_scores.csv,\n*_feature_scores.csv,\n*_model_feature_importance.csv"]
  end

  BacktestCmd --> Trades2
  SweepCmd --> Sweeps
  OptimizeCmd --> OptReports
```

## Optimizer dataflow

Two entry points share the same engine: the dashboard's "Run Optimizer Now" button and the CLI `optimize` command. Both land in `stockmarket.optimization`.

```mermaid
flowchart LR
  DashBtn["dashboard 'Run Optimizer Now'"] --> DashHandler["_run_optimizer_from_dashboard"]
  CliOpt["python -m stockmarket optimize"] --> CliOptHandler["cli.optimize subcommand"]

  DashHandler --> SqliteLog["PaperState.log\nloaded from SQLite"]
  SqliteLog --> OptPkg["stockmarket.optimization"]
  CliOptHandler --> OptPkg

  subgraph OptPkg["stockmarket.optimization"]
    TH["trade_history.py\n(CSV + paper-log normalization)"]
    FE["features.py\n(walk-forward feature engineering)"]
    RP["reports.py\n(export)"]
  end

  TH --> FE --> RP
  RP --> OutOpt["outputs/optimize_*_recommendations.json\noutputs/optimize_*_symbol_scores.csv\noutputs/optimize_*_feature_scores.csv\noutputs/optimize_*_model_feature_importance.csv\noutputs/optimize_*_enriched_trades.csv"]
```

## ML scoring dataflow

`SymbolScorer` is the Protocol the cycle consumes. Three implementations live under `ml/`; the historical-bootstrap pipeline that feeds the sklearn scorer lives in the legacy `market_learning.py` module plus `cycle/scoring.py`. A deeper subsystem walkthrough (training data, accuracy expectations, retrain cadence) is in `ML_MARKET_LEARNING.md`.

```mermaid
flowchart LR
  Cycle["cycle/scoring.py"] --> Protocol["domain/scorer.py::SymbolScorer"]
  Protocol --> Null["ml/null_scorer.py\n(DISABLE_ML_SCORER=1)"]
  Protocol --> Sklearn["ml/sklearn_scorer.py"]
  Protocol --> BiasS["ml/bias.py\n(BiasOverlayScorer)"]

  subgraph Training["Training / data sources"]
    HistOHLCV["market_learning.MarketDataFetcher\n(60d daily OHLCV)"]
    PersTrades["personal trade history\nPaperState.log from SQLite"]
    LiveTrend["live quote → trend metrics"]
  end

  HistOHLCV --> Sklearn
  PersTrades --> Sklearn
  PersTrades --> BiasS
  LiveTrend --> Sklearn

  subgraph Artifacts["outputs/"]
    Cache["market_data_cache/*.csv"]
    Model["market_learning_model.pkl"]
  end

  HistOHLCV <--> Cache
  Sklearn <--> Model
```

## Fresh local start

Paper state starts blank when `.database/paper_state.db` is absent. Old `outputs/simple_paper_state*.json` files are not read or written and can be removed with other local artifacts.

## Source-of-truth pointers

- `MIGRATION.md` — phase-by-phase record of what shipped and why.
- `CODEBASE_STRUCTURE.md` — package layout, design patterns, feature flags, common commands.
- `README.md` — how to install, run, and drive the CLI.
- `ML_MARKET_LEARNING.md` — ML scorer subsystem deep dive.
