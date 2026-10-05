from __future__ import annotations

import argparse
import json
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .backtest import run_backtest
from .config import TradingConfig
from .core.market_session import MarketSession
from .data import fetch_intraday_data, latest_bars
from .strategy import add_strategy_columns
from .sweep import run_parameter_sweep
from .validation.robustness import default_parameter_variations, run_robustness_analysis
from .validation.reports import write_validation_report
from .validation.walk_forward import WalkForwardConfig, walk_forward_validate


def _market_now(cfg: TradingConfig) -> datetime:
    return datetime.now(ZoneInfo(cfg.market_timezone))


def _summarize_observed_data(df: pd.DataFrame, cfg: TradingConfig) -> dict[str, object]:
    session = MarketSession.from_config(cfg)
    local_index = df.index.tz_convert(session.zone)
    interval_minutes: int | None = None
    if cfg.interval.endswith("m"):
        try:
            interval_minutes = int(cfg.interval[:-1])
        except ValueError:
            interval_minutes = None
    missing_bars: int | None = None
    if interval_minutes is not None and interval_minutes > 0:
        missing_bars = 0
        for _, day_index in pd.Series(local_index, index=local_index).groupby(local_index.date):
            deltas = day_index.diff().dropna()
            cadence = pd.Timedelta(minutes=interval_minutes)
            for delta in deltas:
                if delta > cadence:
                    missing_bars += max(1, int(round(delta / cadence)) - 1)
    return {
        "observations": int(len(df)),
        "observed_sessions": int(len(set(local_index.date))),
        "first_timestamp": local_index[0].isoformat(),
        "last_timestamp": local_index[-1].isoformat(),
        "missing_in_session_bars": missing_bars,
        "gap_detection": (
            f"same-date timestamp deltas above {interval_minutes} minute cadence"
            if interval_minutes is not None
            else "not available for non-minute interval"
        ),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Intraday strategy research CLI for NSE/BSE symbols")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backtest_parser = subparsers.add_parser("backtest", help="Run backtest")
    backtest_parser.add_argument(
        "--config", default="config.json", help="Path to config JSON")
    backtest_parser.add_argument(
        "--symbol", default=None, help="Override symbol in config")

    signal_parser = subparsers.add_parser(
        "signals", help="Show latest bars and current signals")
    signal_parser.add_argument(
        "--config", default="config.json", help="Path to config JSON")
    signal_parser.add_argument(
        "--symbol", default=None, help="Override symbol in config")
    signal_parser.add_argument(
        "--bars", type=int, default=5, help="Number of latest bars to display")

    sweep_parser = subparsers.add_parser(
        "sweep", help="Run parameter sweep and rank combinations")
    sweep_parser.add_argument(
        "--config", default="config.json", help="Path to config JSON")
    sweep_parser.add_argument(
        "--symbol", default=None, help="Override symbol in config")
    sweep_parser.add_argument(
        "--opening-ranges",
        default="10,15,20",
        help="Comma-separated opening range minutes",
    )
    sweep_parser.add_argument(
        "--stop-losses",
        default="0.003,0.004,0.005",
        help="Comma-separated stop loss percentages",
    )
    sweep_parser.add_argument(
        "--take-profits",
        default="0.006,0.008,0.01",
        help="Comma-separated take profit percentages",
    )
    sweep_parser.add_argument(
        "--volume-spikes",
        default="1.1,1.2,1.4",
        help="Comma-separated volume spike thresholds",
    )
    sweep_parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="Number of top rows to print in console",
    )

    replay_parser = subparsers.add_parser(
        "replay-best",
        help="Replay best sweep result and export full trade log",
    )
    replay_parser.add_argument(
        "--config", default="config.json", help="Path to config JSON")
    replay_parser.add_argument(
        "--symbol", default=None, help="Override symbol in config")
    replay_parser.add_argument(
        "--sweep-file",
        default=None,
        help="Path to sweep CSV; if omitted latest matching file is used",
    )
    replay_parser.add_argument(
        "--rank",
        type=int,
        default=1,
        help="1-based row rank from sorted sweep CSV",
    )
    replay_parser.add_argument(
        "--min-trades",
        type=float,
        default=0.0,
        help="Minimum total trades required in sweep row",
    )
    replay_parser.add_argument(
        "--max-drawdown-pct",
        type=float,
        default=None,
        help="Maximum allowed absolute drawdown as decimal (example: 0.08 for 8%)",
    )

    validation_parser = subparsers.add_parser(
        "validate",
        help="Run time-series walk-forward and robustness validation",
    )
    validation_parser.add_argument("--config", default="config.json")
    validation_parser.add_argument("--symbol", default=None)
    validation_parser.add_argument(
        "--period", default=None,
        help="Historical lookback override; enough sessions are required for folds",
    )
    validation_parser.add_argument("--train-sessions", type=int, default=10)
    validation_parser.add_argument("--test-sessions", type=int, default=3)
    validation_parser.add_argument("--step-sessions", type=int, default=3)
    validation_parser.add_argument("--gap-sessions", type=int, default=0)
    validation_parser.add_argument(
        "--window-mode", choices=["rolling", "expanding"], default="rolling")
    validation_parser.add_argument("--min-train-trades", type=int, default=1)
    validation_parser.add_argument("--opening-ranges", default="10,15,20")
    validation_parser.add_argument(
        "--stop-losses", default="0.003,0.004,0.005")
    validation_parser.add_argument(
        "--take-profits", default="0.006,0.008,0.01")
    validation_parser.add_argument("--volume-spikes", default="1.1,1.2,1.4")
    validation_parser.add_argument("--volume-ma-windows", default="20")
    validation_parser.add_argument(
        "--vwap-price-sources", default="typical,close")
    validation_parser.add_argument(
        "--commission-multipliers", default="0.5,1,1.5")
    validation_parser.add_argument(
        "--slippage-multipliers", default="0.5,1,1.5")
    validation_parser.add_argument("--max-scenarios", type=int, default=30)
    validation_parser.add_argument("--output-dir", default="outputs")

    return parser


def _load_config(config_path: str, symbol_override: str | None) -> TradingConfig:
    cfg = TradingConfig.from_json(config_path)
    if symbol_override:
        cfg.symbol = symbol_override
    return cfg


def cmd_backtest(cfg: TradingConfig) -> int:
    df = fetch_intraday_data(
        cfg.symbol, cfg.interval, cfg.period,
        tz=cfg.market_timezone, session=MarketSession.from_config(cfg)
    )
    result = run_backtest(df, cfg)

    print("Backtest Summary")
    _print_backtest_summary(result)

    if result.trades.empty:
        print("No trades generated for the selected period/config.")
        return 0

    out_dir = Path("outputs")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = _market_now(cfg).strftime("%Y%m%d_%H%M%S")
    out_file = out_dir / f"trades_{cfg.symbol.replace('.', '_')}_{ts}.csv"
    result.trades.to_csv(out_file, index=False)
    print(f"Saved trades: {out_file}")

    return 0


def cmd_signals(cfg: TradingConfig, bars: int) -> int:
    df = fetch_intraday_data(
        cfg.symbol, cfg.interval, cfg.period,
        tz=cfg.market_timezone, session=MarketSession.from_config(cfg)
    )
    sdf = add_strategy_columns(df, cfg)
    view_cols = [
        "open",
        "high",
        "low",
        "close",
        "volume",
        "vwap",
        "or_high",
        "or_low",
        "vol_spike",
        "long_signal",
        "short_signal",
    ]
    latest = latest_bars(sdf[view_cols], count=bars)
    print(latest.to_string())
    return 0


def _parse_float_list(value: str) -> list[float]:
    return [float(v.strip()) for v in value.split(",") if v.strip()]


def _parse_int_list(value: str) -> list[int]:
    return [int(v.strip()) for v in value.split(",") if v.strip()]


def cmd_sweep(
    cfg: TradingConfig,
    opening_ranges: str,
    stop_losses: str,
    take_profits: str,
    volume_spikes: str,
    top: int,
) -> int:
    df = fetch_intraday_data(
        cfg.symbol, cfg.interval, cfg.period,
        tz=cfg.market_timezone, session=MarketSession.from_config(cfg)
    )

    table = run_parameter_sweep(
        df=df,
        base_cfg=cfg,
        opening_ranges=_parse_int_list(opening_ranges),
        stop_losses=_parse_float_list(stop_losses),
        take_profits=_parse_float_list(take_profits),
        volume_spikes=_parse_float_list(volume_spikes),
    )

    if table.empty:
        print("No sweep results generated.")
        return 0

    out_dir = Path("outputs")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = _market_now(cfg).strftime("%Y%m%d_%H%M%S")
    out_file = out_dir / f"sweep_{cfg.symbol.replace('.', '_')}_{ts}.csv"
    table.to_csv(out_file, index=False)

    print(f"Saved sweep results: {out_file}")
    print("Top combinations")
    print(table.head(max(1, top)).to_string(index=False))
    return 0


def _find_latest_sweep_file(symbol: str) -> Path | None:
    out_dir = Path("outputs")
    if not out_dir.exists():
        return None
    pattern = f"sweep_{symbol.replace('.', '_')}_*.csv"
    candidates = sorted(out_dir.glob(pattern),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def cmd_replay_best(
    cfg: TradingConfig,
    sweep_file: str | None,
    rank: int,
    min_trades: float,
    max_drawdown_pct: float | None,
) -> int:
    if rank < 1:
        raise ValueError("rank must be >= 1")
    if min_trades < 0:
        raise ValueError("min_trades must be >= 0")
    if max_drawdown_pct is not None and max_drawdown_pct < 0:
        raise ValueError("max_drawdown_pct must be >= 0")

    sweep_path = Path(
        sweep_file) if sweep_file else _find_latest_sweep_file(cfg.symbol)
    if sweep_path is None:
        raise FileNotFoundError(
            "No sweep file found. Run 'sweep' first or pass --sweep-file explicitly."
        )
    if not sweep_path.exists():
        raise FileNotFoundError(f"Sweep file does not exist: {sweep_path}")

    table = pd.read_csv(sweep_path)
    if table.empty:
        print("Sweep file is empty.")
        return 0

    filtered = table[table["total_trades"] >= min_trades].copy()
    if max_drawdown_pct is not None:
        filtered = filtered[filtered["max_drawdown_pct"].abs()
                            <= max_drawdown_pct]

    if filtered.empty:
        raise ValueError(
            "No sweep rows satisfy filters. "
            f"min_trades={min_trades}, max_drawdown_pct={max_drawdown_pct}"
        )
    if rank > len(filtered):
        raise IndexError(
            f"rank {rank} exceeds filtered rows ({len(filtered)})")

    row = filtered.iloc[rank - 1]
    tuned_cfg = replace(
        cfg,
        opening_range_minutes=int(row["opening_range_minutes"]),
        stop_loss_pct=float(row["stop_loss_pct"]),
        take_profit_pct=float(row["take_profit_pct"]),
        volume_spike_threshold=float(row["volume_spike_threshold"]),
    )

    print(f"Using sweep file: {sweep_path}")
    print(
        f"Eligible rows after filters: {len(filtered)}/{len(table)} "
        f"(min_trades={min_trades}, max_drawdown_pct={max_drawdown_pct})"
    )
    print(
        "Selected parameters: "
        f"opening_range_minutes={tuned_cfg.opening_range_minutes}, "
        f"stop_loss_pct={tuned_cfg.stop_loss_pct}, "
        f"take_profit_pct={tuned_cfg.take_profit_pct}, "
        f"volume_spike_threshold={tuned_cfg.volume_spike_threshold}"
    )

    df = fetch_intraday_data(
        tuned_cfg.symbol,
        tuned_cfg.interval,
        tuned_cfg.period,
        tz=tuned_cfg.market_timezone,
        session=MarketSession.from_config(tuned_cfg),
    )
    result = run_backtest(df, tuned_cfg)

    print("Replay Backtest Summary")
    _print_backtest_summary(result)

    out_dir = Path("outputs")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = _market_now(tuned_cfg).strftime("%Y%m%d_%H%M%S")

    trades_file = out_dir / \
        f"replay_trades_{tuned_cfg.symbol.replace('.', '_')}_{ts}.csv"
    result.trades.to_csv(trades_file, index=False)

    config_file = out_dir / \
        f"replay_config_{tuned_cfg.symbol.replace('.', '_')}_{ts}.json"
    config_file.write_text(json.dumps(
        asdict(tuned_cfg), indent=2), encoding="utf-8")

    print(f"Saved replay trades: {trades_file}")
    print(f"Saved replay config: {config_file}")
    return 0


def cmd_validate(
    cfg: TradingConfig,
    *,
    period: str | None,
    train_sessions: int,
    test_sessions: int,
    step_sessions: int,
    gap_sessions: int,
    window_mode: str,
    min_train_trades: int,
    opening_ranges: str,
    stop_losses: str,
    take_profits: str,
    volume_spikes: str,
    volume_ma_windows: str,
    vwap_price_sources: str,
    commission_multipliers: str,
    slippage_multipliers: str,
    max_scenarios: int,
    output_dir: str,
) -> int:
    if period:
        cfg.period = period
    data = fetch_intraday_data(
        cfg.symbol,
        cfg.interval,
        cfg.period,
        tz=cfg.market_timezone,
        session=MarketSession.from_config(cfg),
    )
    validation_config = WalkForwardConfig(
        train_sessions=train_sessions,
        test_sessions=test_sessions,
        step_sessions=step_sessions,
        gap_sessions=gap_sessions,
        mode=window_mode,
        min_train_trades=min_train_trades,
    )
    parameter_grid = {
        "opening_range_minutes": tuple(_parse_int_list(opening_ranges)),
        "stop_loss_pct": tuple(_parse_float_list(stop_losses)),
        "take_profit_pct": tuple(_parse_float_list(take_profits)),
        "volume_spike_threshold": tuple(_parse_float_list(volume_spikes)),
        "volume_ma_window": tuple(_parse_int_list(volume_ma_windows)),
        "vwap_price_source": tuple(
            value.strip()
            for value in vwap_price_sources.split(",")
            if value.strip()
        ),
    }
    walk_forward = walk_forward_validate(
        data,
        cfg,
        validation_config,
        parameter_grid=parameter_grid,
    )
    robustness = run_robustness_analysis(
        data,
        cfg,
        validation_config,
        parameter_variations=default_parameter_variations(cfg),
        commission_multipliers=_parse_float_list(commission_multipliers),
        slippage_multipliers=_parse_float_list(slippage_multipliers),
        max_scenarios=max_scenarios,
    )
    report_paths = write_validation_report(
        output_dir,
        symbol=cfg.symbol,
        config=cfg,
        walk_forward=walk_forward,
        robustness=robustness,
        data_source=f"Yahoo Finance: {cfg.symbol} {cfg.interval} {cfg.period}",
        data_summary=_summarize_observed_data(data, cfg),
        report_id=_market_now(cfg).strftime("%Y%m%d_%H%M%S_%f"),
    )
    print("Walk-forward out-of-sample summary")
    for key, value in walk_forward.aggregate_oos_metrics.items():
        print(f"- {key}: {'N/A' if value is None else value}")
    evaluated_folds = sum(
        fold.status == "evaluated" for fold in walk_forward.folds)
    print(f"Evaluated folds: {evaluated_folds}/{len(walk_forward.folds)}")
    print(f"Robustness scenarios: {len(robustness.scenarios)}")
    for kind, path in report_paths.items():
        print(f"Saved {kind} report: {path}")
    return 0


def _print_backtest_summary(result) -> None:
    for key, value in result.summary.items():
        if value is None:
            status = result.summary.get(f"{key}_status", "undefined")
            print(f"- {key}: N/A ({status})")
        elif key in {"win_rate", "return_pct", "total_return", "exposure", "max_drawdown_pct", "maximum_drawdown"}:
            print(f"- {key}: {value:.4%}")
        elif isinstance(value, (int, float)):
            print(f"- {key}: {value:.4f}")
        else:
            print(f"- {key}: {value}")


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    cfg = _load_config(args.config, getattr(args, "symbol", None))

    if args.command == "backtest":
        return cmd_backtest(cfg)
    if args.command == "signals":
        return cmd_signals(cfg, bars=args.bars)
    if args.command == "sweep":
        return cmd_sweep(
            cfg,
            opening_ranges=args.opening_ranges,
            stop_losses=args.stop_losses,
            take_profits=args.take_profits,
            volume_spikes=args.volume_spikes,
            top=args.top,
        )
    if args.command == "replay-best":
        return cmd_replay_best(
            cfg,
            sweep_file=args.sweep_file,
            rank=args.rank,
            min_trades=args.min_trades,
            max_drawdown_pct=args.max_drawdown_pct,
        )
    if args.command == "validate":
        return cmd_validate(
            cfg,
            period=args.period,
            train_sessions=args.train_sessions,
            test_sessions=args.test_sessions,
            step_sessions=args.step_sessions,
            gap_sessions=args.gap_sessions,
            window_mode=args.window_mode,
            min_train_trades=args.min_train_trades,
            opening_ranges=args.opening_ranges,
            stop_losses=args.stop_losses,
            take_profits=args.take_profits,
            volume_spikes=args.volume_spikes,
            volume_ma_windows=args.volume_ma_windows,
            vwap_price_sources=args.vwap_price_sources,
            commission_multipliers=args.commission_multipliers,
            slippage_multipliers=args.slippage_multipliers,
            max_scenarios=args.max_scenarios,
            output_dir=args.output_dir,
        )

    parser.error("Unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
