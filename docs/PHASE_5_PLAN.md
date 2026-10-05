# Phase 5 Plan: Statistical Validation and Robustness

Status: Approved; implementation complete.  
Scope: Offline research/backtesting and paper-trading validation only. No broker, live execution, API, database, or dashboard redesign.

## 1. Phase 4 Architecture Today

The project has a compact `src/stockmarket` research package and a larger Streamlit layer:

- `config.py` provides the `TradingConfig` dataclass and JSON loading.
- `data.py` fetches Yahoo Finance OHLCV, normalizes columns/timezones, retries requests, filters bars using `MarketSession`, and caches frames locally with pickle.
- `strategy.py` computes session VWAP, opening range, rolling volume average/spike and ORB long/short booleans.
- `backtest.py` simulates a single instrument with one position at a time, risk-based quantity, configured percentage slippage/commission, stop/target/time/square-off exits, and a small summary.
- `sweep.py` runs a Cartesian grid over opening-range minutes, stop loss, take profit, and volume-spike threshold, then ranks on the same input history.
- `cli.py` exposes `backtest`, `signals`, `sweep`, and `replay-best`; `webapp.py` exposes a legacy Streamlit research interface.
- `core/` contains the Phase 1 domain models, Phase 2 `RiskEngine`, Phase 3 `OrderManager` and in-memory `PaperExecutor`, and Phase 4A `MarketSession`.
- `dashboard.py` and `dashboard_simple.py` still contain their own paper execution, persistence, and several paper-history metrics. They do not call the new `OrderManager` or `PaperExecutor`; Phase 5 will not wire them in.
- Runtime state and exports remain JSON/CSV files under `outputs/`; this phase will not migrate persistence.

`MarketSession` supplies timezone-aware session decisions. The configured NSE defaults remain Asia/Kolkata, 09:15 market open, 15-minute opening range ending at 09:30, 13:30 entry cutoff, 15:15 square-off, and 15:30 market close. No holiday calendar is present.

## 2. What Already Exists

### Backtesting and execution assumptions

- Entries use a signal from a bar close and apply percentage slippage to that same close.
- Stops and targets are checked against the current bar's high/low. If both levels are crossed in one bar, the current order of checks gives the stop precedence.
- Per-trade commission is calculated as a percentage of entry plus exit turnover.
- Time exit and square-off are applied, with a final forced day-end close.
- Position size is computed from current capital, configured risk percentage, and stop distance.
- `MarketSession` is used for square-off and data-session filtering.

### Parameter tuning and learning

- `sweep.py` currently ranks in-sample parameter combinations by return, net PnL, profit factor, and drawdown.
- `replay-best` selects a sweep row, then re-runs that configuration on the same configured data period. It is not an out-of-sample replay.
- `dashboard.py` has paper-trade quality gates, historical ranking bonuses, and an optional five-day threshold tuner based on paper history.
- `dashboard_simple.py` has symbol-level paper-history biases and adjustments based on the latest daily outcomes and signal-derived regime hints.
- These dashboard learning/self-tuning mechanisms are heuristic paper workflows, not statistical model selection or walk-forward validation.

### Existing performance summaries

- `BacktestResult.summary` currently reports total trades, win rate, net PnL, return, max drawdown, and profit factor.
- The dashboards show realized/open PnL, win rate, average win/loss, trade counts, and daily summaries from paper logs.
- There is no unified metric contract, exposure series, risk-adjusted performance report, or separate in-sample/out-of-sample result structure.

### Tests, reports, and logging

- Tests use standard-library `unittest`; no third-party test framework is required.
- Current tests cover domain model validation, RiskEngine decisions, OrderManager/PaperExecutor behavior, and MarketSession boundaries.
- Baseline command: `PYTHONPATH=src python -m unittest discover -s tests -v` (PowerShell equivalent is documented in README). At planning time, the full suite passes: 46 tests.
- Backtest/sweep commands print summaries and export CSV; replay exports trades and config JSON. There is no validation report combining folds, robustness scenarios, and OOS statistics.
- No standard structured logger is configured. Operational/CLI output is primarily `print`; dashboard actions are logged to local JSON/CSV paper history.

## 3. Phase 5 Gaps and Risks

1. There is no chronological train/test splitter, rolling or expanding window evaluator, or untouched out-of-sample result.
2. Selecting the best sweep row and replaying it on the same period reuses the selection sample, so the replay is not evidence of generalization.
3. Entry at a bar close followed by stop/target checks against that same bar's high/low can use price movement that occurred before the close-time signal existed. This is a look-ahead/order-of-events defect in the backtest model.
4. The backtest does not validate input index ordering/timezone, finite OHLCV values, OHLC consistency, or non-negative volume before simulation. The downloader removes duplicate timestamps by keeping the first row, but direct callers can pass malformed frames.
5. Its equity points are appended only after closed trades. That is insufficient for reliable periodic Sharpe/Sortino and does not measure mark-to-market exposure or drawdown within an open trade.
6. Costs are simplified to a turnover commission and percentage slippage. There is no explicit spread, cost stress scale, fill delay, or execution-model metadata in results.
7. The current summary omits expectancy, average win/loss, consecutive wins/losses, exposure, Sharpe, Sortino, and fold-level OOS performance.
8. Sweep perturbations do not cover cost assumptions or a VWAP configuration axis. Core VWAP currently uses typical price and session cumulative volume, with no explicit tuning parameter.
9. Dashboard learning metrics and backtest metrics use different definitions and paper logs can contain separate entry/exit rows. Phase 5 must not silently treat a dashboard order row as one independent completed backtest trade.
10. Yahoo history is a research data source; the report must identify the data source/period and must not imply reliable executable fills or profitability.

## 4. Proposed File Structure

Create a small, isolated validation package:

```text
src/stockmarket/validation/
    __init__.py
    statistics.py       # Shared deterministic trade/equity metrics
    walk_forward.py     # Chronological rolling/expanding splits and OOS runner
    robustness.py       # Parameter and cost perturbation scenarios
    reports.py          # Versioned JSON/CSV/Markdown report serialization
```

Add focused tests:

```text
tests/
    test_backtest_validation.py  # Input validation, execution timing, cost assumptions
    test_performance_statistics.py
    test_walk_forward.py
    test_robustness.py
    test_validation_reports.py
```

Modify only these existing modules as needed:

- `src/stockmarket/backtest.py`: input validation, event-order-safe fill timing, periodic equity/exposure observations, and backward-compatible summary fields.
- `src/stockmarket/sweep.py`: reuse shared parameter-grid construction/metrics and offer deterministic sensitivity scenarios without selecting on OOS data.
- `src/stockmarket/strategy.py`: add only the smallest explicit VWAP calculation option needed for controlled sensitivity experiments; keep the current typical-price/session-reset behavior as the unchanged default.
- `src/stockmarket/cli.py`: add a `validate` command/options and write validation artifacts under `outputs/` without replacing existing command outputs.
- `README.md`: document the validation command, split semantics, metrics, execution assumptions, and limitations after implementation.

No changes are planned for `dashboard.py`, `dashboard_simple.py`, Phase 1–4 model contracts, paper state files, broker/execution behavior, or dependency requirements unless implementation review proves a narrow incompatibility.

## 5. Design and Statistical Rules

### Walk-forward protocol

- Split by ordered exchange-local trading dates, never by shuffled rows.
- Support rolling and expanding training windows, a configurable training span, test span, step size, and optional gap/embargo between train and test.
- Require each fold to have non-empty train and test ranges and enough valid observations/trades; report skipped folds with reasons rather than silently dropping them.
- Any parameter search for a fold may inspect only that fold's training data. Freeze the selected parameters before evaluating the immediately subsequent test block.
- Aggregate OOS results only from test blocks, with each timestamp assigned to at most one OOS block by default. Preserve fold-level results as well as aggregate results.
- Keep in-sample selection metrics and OOS evaluation metrics in separate named structures and files/columns.
- Indicators must be causal: trailing windows may use the current completed bar and prior bars only; opening-range values must not enable entries before the range has completed.

### Backtest execution and data quality

- Establish one explicit, reportable execution policy. A signal known only at a completed bar close must not fill at that same bar's close and then be tested against that bar's earlier high/low. The proposed conservative policy is fill at the next available bar open with configured adverse slippage; stop/target checks begin on later bars. Keep deterministic stop-first handling if both stop and target are crossed in a later OHLC bar and label the assumption.
- Preserve commission and slippage parameters; add spread/latency only as explicit optional stress assumptions if they can be modeled deterministically from available inputs. Do not invent unavailable quote depth or partial fills.
- Validate timestamps (aware, sorted, unique), required columns, finite prices/volume, positive OHLC prices, `high >= max(open, close)`, `low <= min(open, close)`, and non-negative volume before computing results.
- Fail with a descriptive validation error for malformed input. Missing bars are not automatically synthesized; without a calendar/feed guarantee, report gaps and do not infer a price. Do not silently drop invalid bars in the backtest layer.

### Performance statistics

- Compute trade-level measures from chronologically ordered completed trades using net PnL after configured costs: number of trades, win rate, average win, average loss, expectancy, profit factor, and consecutive win/loss maxima.
- Compute total return and maximum drawdown from a documented equity curve. Produce mark-to-market equity at each observed bar where a position is open, plus realized cash/equity when flat.
- Compute Sharpe and Sortino from periodic equity returns, with an explicit `periods_per_year` setting and a documented risk-free-rate assumption. Do not calculate them from isolated trade PnLs and present them as daily/annualized portfolio Sharpe.
- Compute exposure as time/bars with an open position divided by valid in-session observation time/bars. Define denominator and treatment of missing bars in report metadata.
- Represent undefined values (for example, no losses for profit factor or too few return observations for a ratio) as JSON `null` plus a status/reason, never non-standard JSON `Infinity`/`NaN`.

### Robustness analysis

- Use a deterministic, explicit grid around baseline parameters; report baseline and every perturbation rather than only the best result.
- Include opening-range minutes, stop-loss, take-profit, volume threshold, slippage, commission/cost multiplier, and sensitivity of the VWAP construction.
- The current VWAP has no independent parameter. Proposed minimal extension: add an experimental price-source option such as `typical` versus `close`, with `typical` remaining the default. Do not change signal formulas or recommend an experimental variant based solely on in-sample PnL.
- Show fold-level and aggregate OOS results for each scenario. Include trade count and missing/failed scenario reasons so sparse samples are visible.
- Do not change the dashboard self-tuner or allow a validation result to update paper/live parameters automatically.

## 6. Implementation Order

1. **Lock contracts and definitions.** Document split units, execution timing, trade outcomes, periodic returns, exposure, undefined metrics, and report schema; preserve current `BacktestResult` consumers through additive fields or compatible mapping keys.
2. **Add deterministic input validation and execution sequencing.** Reject malformed OHLCV and prevent entry-bar look-ahead. Add synthetic-bar regression cases before layering on validation orchestration.
3. **Add performance statistics.** Build shared metric functions from trades and equity observations. Test edge cases (no trades, no losses, all losses, breakeven, short trades, sparse returns, zero drawdown denominator).
4. **Add walk-forward splits and evaluator.** Implement rolling/expanding date splits, training-only parameter selection, gap support, OOS fold execution, and validation of disjoint chronology.
5. **Add robustness scenarios.** Build deterministic parameter and cost perturbations over the same fold protocol; include VWAP-source experiments with the current source as baseline.
6. **Add validation reports and CLI entry point.** Emit versioned JSON, flat CSV rows by fold/scenario, and human-readable Markdown. Include data provenance, config, assumptions, train/test date ranges, selected parameters, and OOS metrics.
7. **Document and regression-test.** Update README and add end-to-end synthetic tests proving train/test separation, no look-ahead, cost sensitivity, reproducibility, report schema, and existing command compatibility.

## 7. Testing Strategy

- Use deterministic synthetic timezone-aware OHLCV frames; never use network data, current market time, or real paper-state files in tests.
- Unit-test split boundaries and prove that training rows precede test rows, train/test do not overlap, and OOS timestamps are not reused.
- Add a signal-at-close fixture where the signal bar's high/low crosses a stop/target; verify the new execution policy cannot exit against pre-fill movement.
- Test every required metric against hand-computed trade/equity examples, including long and short PnL.
- Test that added transaction costs/slippage cannot improve a fixed set of fills' net results, and that parameter perturbations are deterministic.
- Test missing/duplicate/unsorted/naive timestamps, NaN/inf, impossible OHLC, and negative volume; each must fail descriptively.
- Test report JSON strict serialization (no NaN/Infinity), CSV fold labels, Markdown in-sample/OOS separation, and report reproducibility.
- Run the new focused tests first, then the full current `unittest` suite. The current baseline is 46 passing tests.

## 8. Acceptance Criteria

Phase 5 is complete only when:

1. Walk-forward folds are chronological, configurable as rolling or expanding, deterministic, and include non-overlapping OOS evaluation intervals.
2. Parameter selection occurs only within training data; OOS results are never used to select the parameters reported for that fold.
3. Backtesting rejects malformed OHLCV and does not use price movement earlier in a signal bar to fill/exit a trade generated at that bar's close.
4. Commission/slippage assumptions are explicit and recorded; robustness scenarios cover requested parameter and cost dimensions.
5. Metrics include total return, win rate, profit factor, expectancy, max drawdown, Sharpe, Sortino, average win/loss, consecutive wins/losses, trade count, exposure, and OOS performance with documented definitions.
6. Validation produces machine-readable JSON/CSV and a readable Markdown report with IS/OOS clearly separated and undefined metrics represented safely.
7. The default ORB/VWAP behavior and current dashboard learning/self-tuning behavior are unchanged; no validation flow automatically mutates paper settings.
8. All existing Phase 4 tests and new Phase 5 tests pass; existing CLI commands remain compatible.
9. No live execution, broker adapter, FastAPI, database, PostgreSQL, or major dashboard redesign is introduced.

## 9. Explicit Risks and Questions to Resolve During Approved Implementation

- **Execution compatibility:** Preventing same-bar look-ahead changes historical backtest outcomes. Proposed default is next-available-bar-open execution; implementation should version/name this assumption and preserve old command shape, not silently claim old and new results are directly comparable.
- **Sharpe/Sortino period:** Intraday data frequency and Yahoo gaps vary. The evaluator must use daily portfolio returns for primary ratios and require/document annualization assumptions; per-bar ratios should be supplementary only.
- **VWAP sensitivity:** No VWAP period/source setting currently exists. The proposed `typical` default plus experimental `close` source is the smallest testable sensitivity dimension; it is research-only and must not be adopted automatically.
- **Cross-symbol portfolio effects:** Current backtest accepts one instrument at a time. Phase 5 metrics and folds remain single-instrument. Portfolio-wide multi-symbol validation is a later phase.
- **Calendar limitations:** MarketSession has no holiday/early-close calendar. Splits group observed sessions from data, and reports must disclose that market-calendar completeness is not guaranteed.
- **Data source limitations:** Yahoo is research-grade here; OOS statistics measure behavior on historical downloaded data, not executable or predictive guarantees.

---

## 10. Implementation Record

Implemented after approval:

- `src/stockmarket/validation/statistics.py`: trade/equity metrics, daily-equity Sharpe/Sortino, exposure, streaks, drawdown, undefined-value statuses.
- `src/stockmarket/validation/walk_forward.py`: rolling/expanding session-date folds, optional gap, train-only grid selection, sequential OOS capital.
- `src/stockmarket/validation/robustness.py`: deterministic one-factor parameter/VWAP/cost scenarios evaluated with the same OOS fold policy.
- `src/stockmarket/validation/reports.py`: strict JSON, fold/scenario CSV, and Markdown reports with data-quality summary.
- `backtest.py`, `strategy.py`, `config.py`, `sweep.py`, and `cli.py`: validated OHLCV input, next-observed-bar-open fills, gap-aware stop/target fills, mark-to-market equity, cost multipliers, explicit VWAP source, expanded metrics, and `validate` command.
- `README.md`: validation usage and execution/statistics assumptions.
- Added focused tests for backtest validation, statistics, walk-forward, robustness, and reports.

The new backtest execution assumption intentionally changes historical backtest results relative to the earlier same-bar close model. Existing dashboards and paper state were not migrated. No live execution, broker code, API, database, or dependencies were added.
