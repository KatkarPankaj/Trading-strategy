from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .backtest import run_backtest
from .config import TradingConfig
from .data import fetch_intraday_data, latest_bars
from .optimizer import export_optimization_report, run_intelligent_optimization
from .strategy import add_strategy_columns
from .sweep import run_parameter_sweep


def _market_now(cfg: TradingConfig) -> datetime:
    return datetime.now(ZoneInfo(cfg.market_timezone))


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

    optimize_parser = subparsers.add_parser(
        "optimize",
        help="Analyze recent trades and generate data-driven strategy recommendations",
    )
    optimize_parser.add_argument(
        "--config", default="config.json", help="Path to config JSON")
    optimize_parser.add_argument(
        "--symbol", default=None, help="Override symbol in config")
    optimize_parser.add_argument(
        "--trade-file",
        default="outputs/paper_trade_history.csv",
        help="Path to backtest trades CSV or paper trade history CSV",
    )
    optimize_parser.add_argument(
        "--lookback-trades",
        type=int,
        default=200,
        help="Number of most recent closed trades to analyze",
    )
    optimize_parser.add_argument(
        "--min-train-trades",
        type=int,
        default=50,
        help="Minimum past trades required before walk-forward scoring starts",
    )
    optimize_parser.add_argument(
        "--quality-threshold",
        type=float,
        default=55.0,
        help="Minimum walk-forward quality score to keep a trade in filtered comparison",
    )

    return parser


def _load_config(config_path: str, symbol_override: str | None) -> TradingConfig:
    if os.environ.get("USE_APP_SETTINGS") == "1":
        from stockmarket.settings import load_app_settings, to_trading_config

        cfg = to_trading_config(load_app_settings(trading_path=Path(config_path)))
    else:
        cfg = TradingConfig.from_json(config_path)
    if symbol_override:
        cfg.symbol = symbol_override
    return cfg


def cmd_backtest(cfg: TradingConfig) -> int:
    df = fetch_intraday_data(cfg.symbol, cfg.interval,
                             cfg.period, tz=cfg.market_timezone)
    result = run_backtest(df, cfg)

    print("Backtest Summary")
    for k, v in result.summary.items():
        if "pct" in k or k in {"win_rate", "return_pct"}:
            print(f"- {k}: {v:.4%}")
        else:
            print(f"- {k}: {v:.4f}")

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
    df = fetch_intraday_data(cfg.symbol, cfg.interval,
                             cfg.period, tz=cfg.market_timezone)
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
    df = fetch_intraday_data(cfg.symbol, cfg.interval,
                             cfg.period, tz=cfg.market_timezone)

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


def cmd_optimize(
    cfg: TradingConfig,
    trade_file: str,
    lookback_trades: int,
    min_train_trades: int,
    quality_threshold: float,
) -> int:
    report = run_intelligent_optimization(
        trade_file=trade_file,
        cfg=cfg,
        lookback_trades=lookback_trades,
        min_train_trades=min_train_trades,
        quality_threshold=quality_threshold,
    )

    ts = _market_now(cfg).strftime("%Y%m%d_%H%M%S")
    prefix = f"optimize_{Path(trade_file).stem}_{ts}"
    artifacts = export_optimization_report(
        report, out_dir="outputs", prefix=prefix)

    print("Optimization Summary")
    for key, value in report.summary.items():
        if isinstance(value, float):
            if "rate" in key or key.endswith("_pct"):
                print(f"- {key}: {value:.2%}")
            else:
                print(f"- {key}: {value:.4f}")
        else:
            print(f"- {key}: {value}")

    print("\nRecommendations")
    if not report.recommendations:
        print("- No strong recommendations yet. Collect more trades or widen lookback.")
    else:
        for idx, item in enumerate(report.recommendations, start=1):
            evidence = item.get("evidence", {})
            trades = evidence.get("trades", 0)
            avg_net = float(evidence.get("avg_net_pnl", 0.0))
            win_rate = float(evidence.get("win_rate", 0.0))
            print(
                f"{idx}. {item['message']} "
                f"[trades={trades}, avg_net_pnl={avg_net:.2f}, win_rate={win_rate:.2%}]"
            )

    if not report.symbol_scores.empty:
        print("\nTop symbols")
        print(report.symbol_scores.head(5).to_string(index=False))

    if not report.model_feature_importance.empty:
        print("\nTop model features")
        print(report.model_feature_importance.head(8).to_string(index=False))

    print("\nClean trade collection progress")
    print(
        f"- clean_closed_trades: {report.summary.get('clean_closed_trades', 0)} | "
        f"to_200: {report.summary.get('trades_to_200_goal', 0)} | "
        f"to_300: {report.summary.get('trades_to_300_goal', 0)}"
    )

    if not report.walkforward_comparison.empty:
        print("\nWalk-forward baseline vs filtered")
        print(report.walkforward_comparison.to_string(index=False))

    print("\nSaved artifacts")
    for name, path in artifacts.items():
        print(f"- {name}: {path}")
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
    )
    result = run_backtest(df, tuned_cfg)

    print("Replay Backtest Summary")
    for k, v in result.summary.items():
        if "pct" in k or k in {"win_rate", "return_pct"}:
            print(f"- {k}: {v:.4%}")
        else:
            print(f"- {k}: {v:.4f}")

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
    if args.command == "optimize":
        return cmd_optimize(
            cfg,
            trade_file=args.trade_file,
            lookback_trades=args.lookback_trades,
            min_train_trades=args.min_train_trades,
            quality_threshold=args.quality_threshold,
        )

    parser.error("Unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
