# Budget-Based Trading Simulator (NSE)

Personal-use paper-trading app to test whether a strategy can grow a fixed budget without risking real capital.

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

- Intraday OHLCV: **US** symbols via Finnhub (`FINNHUB_API_KEY`); **India** (`.NS`, `^NSEI`) via NSE archives / niftyindices-backed daily series expanded to session bars (see `src/stockmarket/data.py`, `nse_intraday.py`).
- NSE/BSE symbols use common suffixes:
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

For **US** symbols (Finnhub quotes and candles), set `FINNHUB_API_KEY` in your environment **or** create a gitignored `.env` in the repo root (copy from `.env.example`).

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

## Launch browser app (legacy research UI)

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m streamlit run src/stockmarket/webapp.py
```

Then open the local URL shown in terminal (usually [http://localhost:8501](http://localhost:8501)).
This entrypoint is legacy and overlaps CLI research flows.

## Launch unified dashboard (recommended)

Starts in **Simple** mode; switch to **Complex scanner** from the sidebar.

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m streamlit run app.py
```

Run the command from the repo root so `.streamlit/config.toml` applies: saving Python files triggers an automatic app rerun (“hot reload”) without restarting the server.

## Launch advanced scanner + dummy trading app (legacy)

This app includes:

- Top 5 intraday candidates scanner
- Buy strategy guidance with signal/risk levels
- Dummy buy/sell orders, holdings, cash, and PnL tracking

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m streamlit run dashboard.py
```

This scanner is deprecated and kept for backward compatibility only.
Use `streamlit run app.py` for the supported dashboard. See [arch migration plan.md](arch migration plan.md).

## Launch simple lightweight simulator (fast page)

This page is optimized for quick load and minimal controls:

- Budget-first setup (default `Rs 200000`)
- Top 5 buy/sell signals only
- Auto paper trading with basic risk controls
- Daily target progress tracking (default `Rs 4000`)

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m streamlit run dashboard_simple.py --server.port 8507
```

US quotes for `dashboard_simple.py` use Finnhub; set `FINNHUB_API_KEY` in your environment.
Optional `dashboard_simple_data_config.json` field `us_market_data_batch_size` controls parallel US quote requests in the scanner.
Set `USE_PAPER_REPO=1` to exercise the flagged JSON repository path for simple paper state persistence.
Set `USE_APP_SETTINGS=1` to resolve NSE/US market watchlists and session times from `config/market_config.json` (default uses inline dashboard constants).
Set `DISABLE_ML_SCORER=1` to skip sklearn market-learning scoring in `dashboard_simple.py` (effective scores use rule scores + trade-history bias only).
Set `USE_SIMPLE_VIEWS=1` to render extracted simple dashboard view modules while keeping legacy inline render blocks available for parity checks.

## Dashboard Strategy (Complete)

The advanced dashboard in [dashboard.py](dashboard.py) is built for paper trading only and applies these rules:

1. Market window logic

- Market open: 09:15 IST
- Entry cutoff: from `config.json` (`entry_cutoff_time`, default 13:30 IST)
- Square-off: from `config.json` (`square_off_time`, default 15:15 IST)
- All time calculations use `market_timezone` from `config.json`.

1. Top 5 candidate ranking

- Universe: internal watchlist of liquid NSE symbols.
- Ranking factors: strategy score, intraday range percent, momentum context.
- Data source options:
  - NSE quote mode (faster, avoids Yahoo limits)
  - Yahoo OHLC mode (bar-based signals)

1. Entry strategy modes

- `ORB + VWAP`: breakout above OR high with VWAP support.
- `VWAP Trend`: continuation above VWAP with momentum/volume confirmation.
- `OR Reversal`: reversal near OR low with recovery confirmation.

1. Buy/Exit button logic

- `Buy Now` is disabled until strategy entry trigger is reached.
- `Buy Now` is also disabled when a position already exists for that symbol.
- `Exit` is enabled only when one of these is true:
  - stop-loss reached
  - target reached
  - square-off time reached

1. Tracking and PnL

- Strategy tracking list stores expected entry, trigger state, SL/TP, and benefit %.
- Dummy portfolio tracks cash, holdings, average price, unrealized PnL, and trade log.

1. Top 5 after time-up

- After entry cutoff time, Top 5 scanner and recommendations are hidden automatically.
- This is intentional to prevent new intraday entries outside planned window.

## Dashboard Usage (Recommended Flow)

1. Start app:

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m streamlit run dashboard.py --server.port 8505
```

1. Choose data source and strategy mode in sidebar.
2. During entry window, review Top 5 list and trigger states.
3. Use `Buy Now` only when trigger changes to `READY`.
4. Monitor SL/TP and use `Exit` when enabled.
5. Review Dummy Portfolio and Trade Log for performance.

## AI Implementation Requirements (Current)

Use this section as the source-of-truth requirements for any AI assistant modifying `dashboard.py`.

1. Data and refresh behavior

- Primary live mode is NSE quote mode (`NSE Quote API (non-Yahoo)`).
- Auto refresh is enabled from sidebar and defaults to 30 seconds.
- Refresh should update Top candidates table prices and trigger fields, not only reload page chrome.
- Top scan can re-run during refresh cycles in NSE mode.

1. Trading windows and safety gates

- No entries before OR end (observation-only during opening range formation).
- New entries only inside entry window (`OR_END` to `entry_cutoff_time`).
- Exit logic remains active for SL/TP and square-off windows.
- Top candidates are hidden after cutoff to prevent fresh entries.

1. Position and notional risk caps (hard constraints)

- Per-symbol quantity cap: `MAX_POSITION_QTY_PER_STOCK = 200`.
- Per-symbol notional cap: `MAX_BUY_NOTIONAL_PER_STOCK = 50000`.
- Both caps must be enforced for new buys and existing positions.
- Existing oversized positions are auto-adjusted and logged in trade history.

1. Auto-buy sizing

- Auto-buy can run fixed quantity or balance-spread mode.
- Balance-spread mode allocates by available cash and remaining slots, then clamps by risk caps.
- Estimated margin + charges must be affordable before order execution.

1. Buy quality gate (required before auto-buy)

- Candidate must pass all quality filters:
  - minimum candidate score
  - minimum reward:risk
  - minimum expected edge after costs (round-trip estimate)
- If any check fails, buy is skipped with explicit reason in engine actions.

1. Learning-aware ranking and suggestions

- Scanner scores are adjusted with a small learning bonus from closed-trade history.
- Learning profile is symbol-level and strategy-aware when enough data exists.
- UI includes "Learning Agent Suggestions" for additional historically favorable symbols.

1. Self-tuning mode

- Optional auto-tune uses recent daily outcomes (last 5 days) to adjust quality filters.
- Weak recent performance should tighten filters; strong consistent performance may relax slightly.
- With no recent closed-trade days, system falls back to base thresholds.

1. Logging and explainability

- Every order/adjustment writes to in-memory log and persistent history file.
- Trade history is used for learning summaries, strategy comparison, and daily PnL export.
- AI changes must preserve human-readable action reasons in UI and logs.

1. Non-functional expectations for AI changes

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

## Run intelligent strategy optimization

Use recent closed trades to generate data-driven suggestions for filters, stronger symbols, and SL/TP context.

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m stockmarket.cli optimize --config config.json --trade-file outputs/paper_trade_history.csv --lookback-trades 200
```

What it analyzes:

- entry time bucket
- volume spike ratio
- distance from VWAP
- opening-range breakout strength
- volatility bucket
- recent symbol expectancy

Advanced engineered features now included:

- candle strength (`body / range`) and wick-to-body ratio
- previous-day high/low breakout distance
- ATR-based volatility percentage
- NIFTY session trend and NIFTY vs VWAP context

Trade-quality model:

- trains a lightweight ridge-style model on recent closed trades
- outputs `ml_win_prob`, `ml_expected_return_pct`, and `ml_trade_quality_score`
- saves feature importance so you can inspect what mattered most

Artifacts are saved in `outputs/`:

- `*_recommendations.json`
- `*_symbol_scores.csv`
- `*_feature_scores.csv`
- `*_model_feature_importance.csv`
- `*_enriched_trades.csv`

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



## How it works: 

Looking at `dashboard_simple._auto_paper_cycle()` (`dashboard_simple.py:2320`) and friends, one cycle does this, in order:

1. Refresh holding prices
2. Check market open / weekend
3. Roll daily counters (peak PnL, ladder day, guard day)
4. Evaluate profit ladder state machine
5. Evaluate profit guard state machine
6. Process forced exits (SL/TP/time/ladder/guard)
7. Process signal-based exits
8. Apply re-entry cooldowns and regime gates
9. Rank candidates, size positions, apply allocation caps
10. Place entries (long / short)
11. Record trades, update state, persist

## Notes

- Yahoo intraday history has period/interval limits. If data is missing, reduce `period` or change interval.
- Yahoo can temporarily rate-limit requests. If this happens, wait and retry, reduce request frequency, or use a different symbol/time window.
- This is a research and education tool, not investment advice.
- Start with paper testing before any real trading.

