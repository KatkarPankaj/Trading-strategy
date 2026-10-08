# Global Market Research and Paper-Trading Platform

The current development path is the October platform layer: provider-aware market
research, deterministic strategy evaluation, risk-gated paper execution, and an
API-backed monitor with explicit paper-proposal acceptance. It remains paper-only
and is not a production/live trading system. The earlier NSE-focused Streamlit simulators and backtest
workflows are retained separately for compatibility; they are not the authority
for new platform architecture.

See [Platform Architecture](docs/ARCHITECTURE.md) for system boundaries and
[Platform Operations](docs/OPERATIONS.md) for API setup, secrets, deployment,
backup, and recovery.

## Personal research and human decisions

Use `PERSONAL_RESEARCH=true` for a recommendation-only application with no broker
execution, including no paper order submission. An explicit development master
or configurable JSON master populates the existing instrument repository at
startup. The dashboard then uses the existing scanner, deterministic ORB/VWAP
strategy, candidate research and optional AI assessment/ranking. Missing AI does
not disable discovery, data or strategy signals; it withholds final BUY/SHORT
recommendations and is shown explicitly. No recommendation guarantees profit.
Choose Auto, India, US or Germany in the dashboard. Backend profiles resolve
exchange-local sessions and single-country universes without per-switch
environment changes. US/Xetra calendar coverage is limited to 2026; India
remains unsupported and Germany's development universe is empty.

See [Personal Research Setup](docs/PERSONAL_RESEARCH.md) for exact launch commands,
configuration, diagnostics, API workflow and current India calendar limitations.
The older research and paper-execution paths below remain available when this
mode is disabled, using separately verified instrument metadata.

## Product Goal
- Start with a defined budget. Default budget is `Rs 200000`.
- Scan market data and generate actionable intraday buy/sell candidates.
- Simulate trades automatically using paper money only.
- Track current capital, open trades, closed trades, and PnL.
- Learn from past trade outcomes to favor stronger conditions and avoid weaker ones.

## Core Simulator Scope
- Budget setup: total capital, risk per trade, and max trades per day.
- Strategy signals: top intraday candidates from predefined strategy rules.
- Auto trade simulation: paper-only trade execution with portfolio updates.
- Portfolio tracking: capital used, available cash, unrealized/realized PnL.
- Trade history: entry, exit, PnL, and reason for trade.
- Learning feedback: summaries of what worked and what failed.

## What this includes
- Intraday data fetch from Yahoo Finance via `yfinance`.
- NSE/BSE symbols support with Yahoo suffixes:
  - NSE: `RELIANCE.NS`, `TCS.NS`
  - BSE: `RELIANCE.BO`, `TCS.BO`
- Opening Range Breakout strategy with VWAP and volume filters.
- Backtest engine with slippage, commission, stop-loss, take-profit, time-based exits, and end-of-day square-off.
- CLI for backtesting and latest signal inspection.
- CLI parameter sweep for quick tuning and ranking.

## Setup
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Configure
Create `config.json` from the template and adjust values.
```powershell
Copy-Item config.example.json config.json
```

Time handling is controlled by `market_timezone` in `config.json`.
For NSE/BSE use:
```json
"market_timezone": "Asia/Kolkata"
```
This keeps strategy/session times correct even if your Windows region is not India.

## Run backtest
```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m stockmarket.cli backtest --config config.json
```

## Launch browser app
```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m streamlit run src/stockmarket/webapp.py
```

Then open the local URL shown in terminal (usually http://localhost:8501).

## Launch API-backed platform dashboard
This separate Streamlit page reads data from the platform API. It includes advisory research and opportunity ranking, server-backed order review, and an explicit paper-only proposal acceptance form. It cannot create arbitrary orders or bypass the platform RiskEngine.

The API must be running and `API_TOKEN` must match its configured bearer token. `API_BASE_URL` defaults to `http://127.0.0.1:8000` for local use; non-loopback API URLs must use HTTPS. The legacy advisory `/research` route requires an approved OpenAI-compatible endpoint configured through `AI_BASE_URL`, `AI_MODEL`, and `AI_API_KEY`/`AI_API_KEY_FILE`, plus explicit `RESEARCH_SESSIONS` for each enabled market. Personal Research instead supplies backend market-profile sessions and permits deterministic research without AI. The dashboard does not supply credentials or invent session boundaries; if research components or calendar coverage are unavailable, it reports an error or diagnostic.

```powershell
$env:PYTHONPATH = "src"
# Set API_TOKEN using your approved local secret-handling method.
.\.venv\Scripts\python.exe -m streamlit run dashboard_app.py
```

The research form accepts optional operator-entered evidence scores. Enter only scores backed by evidence you have verified; the UI labels these as operator input, not vendor data. The API can opt in to timestamped Yahoo Finance reported-EPS event evidence with `FUNDAMENTAL_PROVIDER=yahoo`; this does not supply sector direction or mappings. AI strategy rankings and model-reported confidence are advisory and are not calibrated forecasts.

The API's separate market-intelligence service exposes `POST /intelligence/opportunities` for timestamped multi-instrument proposals. It requires the configured AI provider, per-market `RESEARCH_SESSIONS`, `NEWS_PROVIDER=finnhub`, and a Finnhub API key. Results combine regime/news/strategy research and deterministic signal aggregation, returning explainable proposals marked `risk_status=NOT_EVALUATED` and `execution=NOT_SUBMITTED`; those in-memory objects are advisory and cannot be submitted. Only an approved proposal persisted by the Phase 2C-3 risk workflow can be accepted through `POST /intelligence/proposals/{proposal_id}/submit`. Automatic risk-based sizing is the default; manual quantity requires explicit `MANUAL_OVERRIDE`. Submission reloads linked run/signal/version provenance, requires fresh market data and a regular session, and routes through `TradingService`/`RiskEngine`/`OrderManager`.

Authenticated `POST /paper/cycles` manually runs a bounded PAPER-only research-to-order cycle with durable idempotency and per-candidate checkpoints; cycle status and explicit recovery are available through `/paper/cycles` routes and the `paper-cycle*` CLI commands. Paper executor orders/attempts and fill sequence are persisted for restart reconciliation (migrations V13/V14), and retry is authorized only when durable executor state proves the stable client order was not accepted. `POST /paper/positions/manage` evaluates fresh quotes against persisted entry stops/targets and routes exits through the same boundary. Cycles and exit monitoring are not scheduled or unattended, and this does not establish production readiness. Both legacy Streamlit dashboard order mutators fail closed before changing their separate local paper state; see [docs/EXECUTION_BOUNDARY.md](docs/EXECUTION_BOUNDARY.md). Active-order reservation remains process-local, so only one API worker is supported.

Platform API setup, secret handling, migration, backup, restore, health checks, and current operational limitations are documented in [docs/OPERATIONS.md](docs/OPERATIONS.md).

## Launch legacy research dashboard
The dashboard can still display:
- Top 5 intraday candidates scanner
- Buy strategy guidance with signal/risk levels

**Legacy order execution is disabled.** The former Buy/Exit paths fail closed
and do not change the dashboard's local portfolio or ledger. Use the
authenticated platform PAPER API for risk-checked orders. Existing dashboard
state files are left untouched and are not imported into the platform portfolio.

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m streamlit run dashboard.py
```

## Launch simple legacy research page (fast page)
This page is optimized for quick load and minimal controls:
- Budget-first setup (default `Rs 200000`)
- Top 5 buy/sell signals only
- Legacy simulator order mutations are disabled
- Daily target progress tracking (default `Rs 4000`)

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m streamlit run dashboard_simple.py --server.port 8507
```

## Legacy Dashboard Research Behavior
The advanced dashboard in [dashboard.py](dashboard.py) retains scanner and
signal-display behavior. Its former local paper-execution helper has been
removed; remaining legacy order controls fail closed. Use the platform API for
paper execution.

1. Market window logic
- Market open: 09:15 IST
- Entry cutoff: from `config.json` (`entry_cutoff_time`, default 13:30 IST)
- Square-off: from `config.json` (`square_off_time`, default 15:15 IST)
- All time calculations use `market_timezone` from `config.json`.

2. Top 5 candidate ranking
- Universe: internal watchlist of liquid NSE symbols.
- Ranking factors: strategy score, intraday range percent, momentum context.
- Data source options:
  - NSE quote mode (faster, avoids Yahoo limits)
  - Yahoo OHLC mode (bar-based signals)

3. Entry strategy modes
- `ORB + VWAP`: breakout above OR high with VWAP support.
- `VWAP Trend`: continuation above VWAP with momentum/volume confirmation.
- `OR Reversal`: reversal near OR low with recovery confirmation.

4. Buy/Exit button logic
- `Buy Now` is disabled until strategy entry trigger is reached.
- `Buy Now` is also disabled when a position already exists for that symbol.
- `Exit` is enabled only when one of these is true:
  - stop-loss reached
  - target reached
  - square-off time reached

5. Tracking and PnL
- Strategy tracking list stores expected entry, trigger state, SL/TP, and benefit %.
- Dummy portfolio tracks cash, holdings, average price, unrealized PnL, and trade log.

6. Top 5 after time-up
- After entry cutoff time, Top 5 scanner and recommendations are hidden automatically.
- This is intentional to prevent new intraday entries outside planned window.

## Dashboard Usage (Recommended Flow)
1. Start app:
```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m streamlit run dashboard.py --server.port 8505
```
2. Choose data source and strategy mode in sidebar.
3. During entry window, review Top 5 list and trigger states.
4. Treat `READY` as research output only; it does not authorize an order.
5. Do not use legacy Buy/Exit controls; they fail closed.
6. Review historical dashboard state only as a separate legacy record.

## Historical Legacy Dashboard Notes
The following notes describe old dashboard behavior and are not permission to
restore dashboard-local order execution. Any future order path must use the
platform PAPER service; the execution boundary is documented in
[docs/EXECUTION_BOUNDARY.md](docs/EXECUTION_BOUNDARY.md).

1. Data and refresh behavior
- Primary live mode is NSE quote mode (`NSE Quote API (non-Yahoo)`).
- Auto refresh is enabled from sidebar and defaults to 30 seconds.
- Refresh should update Top candidates table prices and trigger fields, not only reload page chrome.
- Top scan can re-run during refresh cycles in NSE mode.

2. Trading windows and safety gates
- No entries before OR end (observation-only during opening range formation).
- New entries only inside entry window (`OR_END` to `entry_cutoff_time`).
- Exit logic remains active for SL/TP and square-off windows.
- Top candidates are hidden after cutoff to prevent fresh entries.

3. Position and notional risk caps (hard constraints)
- Per-symbol quantity cap: `MAX_POSITION_QTY_PER_STOCK = 200`.
- Per-symbol notional cap: `MAX_BUY_NOTIONAL_PER_STOCK = 50000`.
- Both caps must be enforced for new buys and existing positions.
- Existing oversized positions are auto-adjusted and logged in trade history.

4. Auto-buy sizing
- Auto-buy can run fixed quantity or balance-spread mode.
- Balance-spread mode allocates by available cash and remaining slots, then clamps by risk caps.
- Estimated margin + charges must be affordable before order execution.

5. Buy quality gate (required before auto-buy)
- Candidate must pass all quality filters:
  - minimum candidate score
  - minimum reward:risk
  - minimum expected edge after costs (round-trip estimate)
- If any check fails, buy is skipped with explicit reason in engine actions.

6. Learning-aware ranking and suggestions
- Scanner scores are adjusted with a small learning bonus from closed-trade history.
- Learning profile is symbol-level and strategy-aware when enough data exists.
- UI includes "Learning Agent Suggestions" for additional historically favorable symbols.

7. Self-tuning mode
- Optional auto-tune uses recent daily outcomes (last 5 days) to adjust quality filters.
- Weak recent performance should tighten filters; strong consistent performance may relax slightly.
- With no recent closed-trade days, system falls back to base thresholds.

8. Logging and explainability
- Every order/adjustment writes to in-memory log and persistent history file.
- Trade history is used for learning summaries, strategy comparison, and daily PnL export.
- AI changes must preserve human-readable action reasons in UI and logs.

9. Non-functional expectations for AI changes
- Prefer minimal, targeted edits.
- Keep paper-trading-only semantics.
- Do not remove or weaken hard risk caps.
- Any new buy logic must remain cost-aware (charges and margin).

Optional symbol override:
```powershell
.\.venv\Scripts\python.exe -m stockmarket.cli backtest --config config.json --symbol TCS.NS
```

## Check latest signals (no order execution)
```powershell
.\.venv\Scripts\python.exe -m stockmarket.cli signals --config config.json
```

## Run parameter sweep (fast tuning)
```powershell
.\.venv\Scripts\python.exe -m stockmarket.cli sweep --config config.json
```

Custom sweep values example:
```powershell
.\.venv\Scripts\python.exe -m stockmarket.cli sweep --config config.json --opening-ranges 10,15,20 --stop-losses 0.003,0.004 --take-profits 0.006,0.008,0.01 --volume-spikes 1.1,1.2 --top 10
```

The sweep command saves a ranked CSV in `outputs/`.

## Replay top sweep result
Run a full backtest using the best ranked sweep row (rank 1 by default):
```powershell
.\.venv\Scripts\python.exe -m stockmarket.cli replay-best --config config.json
```

Replay a specific rank from a specific sweep file:
```powershell
.\.venv\Scripts\python.exe -m stockmarket.cli replay-best --config config.json --sweep-file outputs/sweep_RELIANCE_NS_20260417_120000.csv --rank 3
```

Replay with quality filters (minimum trades and drawdown cap):
```powershell
.\.venv\Scripts\python.exe -m stockmarket.cli replay-best --config config.json --min-trades 25 --max-drawdown-pct 0.08 --rank 1
```

This command exports:
- `replay_trades_*.csv` (detailed trades)
- `replay_config_*.json` (exact config used)

## Walk-Forward Validation and Robustness

Run offline chronological train/test validation for the configured symbol:
```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m stockmarket.cli validate --config config.json --period 60d
```

Use rolling or expanding training windows, an optional session gap, and explicit parameter grids:
```powershell
.\.venv\Scripts\python.exe -m stockmarket.cli validate --config config.json --period 60d --window-mode rolling --train-sessions 20 --test-sessions 5 --step-sessions 5 --gap-sessions 1 --min-train-trades 5
```

For each fold, parameters are selected from training sessions only and then frozen for the subsequent out-of-sample sessions. OOS windows do not overlap. The command also runs one-factor robustness scenarios for opening range, stop loss, target, volume settings, VWAP price source, commission, and slippage. `typical` remains the default VWAP source; `close` is an experimental sensitivity case and is not selected into dashboard settings.

Validation writes three files under `outputs/` (or `--output-dir`):
- `validation_<symbol>_<run>.json`: versioned machine-readable report.
- `validation_<symbol>_<run>.csv`: fold/scenario rows with separately prefixed train and OOS metrics.
- `validation_<symbol>_<run>.md`: human-readable report.

Reports include total return, win rate, profit factor, expectancy, maximum drawdown, daily-equity Sharpe and Sortino, average win/loss, consecutive wins/losses, trade count, exposure, and fold-level/aggregate OOS results. Undefined metrics are represented as `null` in JSON rather than `Infinity` or `NaN`.

The validation backtest uses a signal-at-close, fill-at-next-observed-bar-open model with adverse percentage slippage. Stop/target checks begin after that fill; if both are crossed within a later OHLC bar, stop is assumed first. Input bars with invalid timestamps or OHLCV values are rejected. Missing bars are not synthesized. Yahoo Finance data and these simulated fills are research assumptions, not guarantees of executable or profitable results. Existing dashboard learning/self-tuning and paper execution are unchanged and are not part of this validation command.

## Notes
- Yahoo intraday history has period/interval limits. If data is missing, reduce `period` or change interval.
- Yahoo can temporarily rate-limit requests. If this happens, wait and retry, reduce request frequency, or use a different symbol/time window.
- This is a research and education tool, not investment advice.
- Start with paper testing before any real trading.
