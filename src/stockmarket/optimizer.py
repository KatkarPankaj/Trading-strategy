from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .config import TradingConfig
from .data import fetch_intraday_data
from .strategy import add_strategy_columns

# #region agent log
_AGENT_DBG_PATH = "/Users/garya/Documents/Work/Learn/python/Trading-strategy/.cursor/debug-d6b078.log"


def _agent_dbg_log(hypothesis_id: str, location: str, message: str, data: dict[str, Any]) -> None:
    try:
        with open(_AGENT_DBG_PATH, "a", encoding="utf-8") as _af:
            _af.write(
                json.dumps(
                    {
                        "sessionId": "d6b078",
                        "hypothesisId": hypothesis_id,
                        "location": location,
                        "message": message,
                        "data": data,
                        "timestamp": int(time.time() * 1000),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except Exception:
        pass


# #endregion


@dataclass
class OptimizationReport:
    summary: dict[str, Any]
    recommendations: list[dict[str, Any]]
    symbol_scores: pd.DataFrame
    feature_scores: pd.DataFrame
    model_feature_importance: pd.DataFrame
    walkforward_comparison: pd.DataFrame
    trades: pd.DataFrame


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def _load_trade_file(path: str | Path) -> pd.DataFrame:
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Trade file not found: {file_path}")

    if file_path.suffix.lower() == ".json":
        payload = json.loads(file_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("log"), list):
            df = pd.DataFrame(payload.get("log", []))
        elif isinstance(payload, list):
            df = pd.DataFrame(payload)
        else:
            raise ValueError(
                "Unsupported JSON trade file. Expected dashboard state JSON with a 'log' array or a list of trade rows."
            )
    else:
        df = pd.read_csv(file_path)

    if df.empty:
        raise ValueError(f"Trade file is empty: {file_path}")
    return df


def _normalize_backtest_trades(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["trade_source"] = "backtest"
    if "symbol" not in out.columns:
        out["symbol"] = None
    out["entry_ts"] = pd.to_datetime(out["entry_ts"], errors="coerce")
    out["exit_ts"] = pd.to_datetime(out["exit_ts"], errors="coerce")
    out["side"] = out["side"].astype(str).str.lower()
    out["qty"] = pd.to_numeric(out["qty"], errors="coerce").fillna(0)
    out["entry_price"] = pd.to_numeric(out["entry_price"], errors="coerce")
    out["exit_price"] = pd.to_numeric(out["exit_price"], errors="coerce")
    out["net_pnl"] = pd.to_numeric(out["net_pnl"], errors="coerce").fillna(0.0)
    out["commission"] = pd.to_numeric(
        out.get("commission", 0.0), errors="coerce").fillna(0.0)
    if "exit_reason" not in out.columns:
        out["exit_reason"] = "unknown"
    return out[
        [
            "trade_source",
            "symbol",
            "side",
            "entry_ts",
            "exit_ts",
            "qty",
            "entry_price",
            "exit_price",
            "net_pnl",
            "commission",
            "exit_reason",
        ]
    ].copy()


def _normalize_paper_trades(df: pd.DataFrame) -> pd.DataFrame:
    rows = df.copy()
    rename_map = {
        "ts": "timestamp_ist",
        "realized_delta": "realized_pnl",
        "reason": "note",
    }
    rows = rows.rename(
        columns={k: v for k, v in rename_map.items() if k in rows.columns})

    required = {"timestamp_ist", "symbol",
                "side", "qty", "price", "realized_pnl"}
    missing = required - set(rows.columns)
    if missing:
        raise ValueError(
            "Paper trade history is missing required columns: " +
            ", ".join(sorted(missing))
        )

    rows["timestamp_ist"] = pd.to_datetime(
        rows["timestamp_ist"], errors="coerce")
    rows = rows.sort_values("timestamp_ist").reset_index(drop=True)

    open_lots: dict[tuple[str, str], list[dict[str, Any]]] = {}
    closed: list[dict[str, Any]] = []

    for _, row in rows.iterrows():
        side = str(row.get("side", "")).upper()
        if side == "ADJUST":
            continue

        symbol = str(row.get("symbol", "")).strip()
        ts = row.get("timestamp_ist")
        qty = int(_safe_float(row.get("qty", 0.0), 0.0))
        price = _safe_float(row.get("price", 0.0), 0.0)
        charges = _safe_float(row.get("charges", 0.0), 0.0)
        realized_pnl = _safe_float(row.get("realized_pnl", 0.0), 0.0)
        note = str(row.get("note", ""))

        if qty <= 0 or symbol == "" or pd.isna(ts):
            continue

        if side in {"BUY", "SHORT"}:
            norm_side = "long" if side == "BUY" else "short"
            key = (symbol, norm_side)
            open_lots.setdefault(key, []).append(
                {
                    "entry_ts": ts,
                    "entry_price": price,
                    "qty": qty,
                    "charges": charges,
                    "note": note,
                }
            )
            continue

        if side not in {"SELL", "COVER"}:
            continue

        norm_side = "long" if side == "SELL" else "short"
        key = (symbol, norm_side)
        remaining_qty = qty
        lots = open_lots.get(key, [])
        if not lots:
            continue

        pnl_per_share = realized_pnl / qty if qty else 0.0
        charge_per_share = charges / qty if qty else 0.0

        while remaining_qty > 0 and lots:
            lot = lots[0]
            matched_qty = min(int(lot["qty"]), remaining_qty)
            if matched_qty <= 0:
                lots.pop(0)
                continue

            closed.append(
                {
                    "trade_source": "paper",
                    "symbol": symbol,
                    "side": norm_side,
                    "entry_ts": lot["entry_ts"],
                    "exit_ts": ts,
                    "qty": matched_qty,
                    "entry_price": _safe_float(lot["entry_price"], 0.0),
                    "exit_price": price,
                    "net_pnl": pnl_per_share * matched_qty,
                    "commission": charge_per_share * matched_qty + _safe_float(lot.get("charges", 0.0), 0.0),
                    "exit_reason": note or "paper_close",
                }
            )

            lot["qty"] = int(lot["qty"]) - matched_qty
            remaining_qty -= matched_qty
            if int(lot["qty"]) <= 0:
                lots.pop(0)

    out = pd.DataFrame(closed)
    if out.empty:
        return out

    return out[
        [
            "trade_source",
            "symbol",
            "side",
            "entry_ts",
            "exit_ts",
            "qty",
            "entry_price",
            "exit_price",
            "net_pnl",
            "commission",
            "exit_reason",
        ]
    ].copy()


def load_closed_trades(path: str | Path) -> pd.DataFrame:
    raw = _load_trade_file(path)
    if {"entry_ts", "exit_ts", "net_pnl", "entry_price", "exit_price"}.issubset(raw.columns):
        out = _normalize_backtest_trades(raw)
    elif (
        {"timestamp_ist", "side", "realized_pnl"}.issubset(raw.columns)
        or {"ts", "side", "realized_delta"}.issubset(raw.columns)
    ):
        out = _normalize_paper_trades(raw)
    else:
        raise ValueError(
            "Unsupported trade file format. Expected backtest trades with entry/exit timestamps or paper trade history."
        )

    if out.empty:
        raise ValueError(
            "No closed trades could be derived from the provided trade file.")

    out = out.dropna(subset=["entry_ts", "exit_ts"]).sort_values(
        "exit_ts").reset_index(drop=True)
    out["holding_minutes"] = (
        (out["exit_ts"] - out["entry_ts"]).dt.total_seconds() / 60.0
    ).clip(lower=0.0)
    out["return_pct"] = np.where(
        out["entry_price"] > 0,
        out["net_pnl"] / (out["entry_price"] * out["qty"]),
        0.0,
    )
    out["is_win"] = out["net_pnl"] > 0
    return out


def collect_clean_closed_trades(
    trade_file: str | Path,
    lookback_trades: int = 0,
) -> tuple[pd.DataFrame, dict[str, int]]:
    trades = load_closed_trades(trade_file)
    if lookback_trades > 0:
        trades = trades.tail(int(lookback_trades)).reset_index(drop=True)

    summary = {
        "clean_closed_trades": int(len(trades)),
        "trades_to_200_goal": max(0, 200 - int(len(trades))),
        "trades_to_300_goal": max(0, 300 - int(len(trades))),
    }
    return trades, summary


def _nearest_feature_row(feature_df: pd.DataFrame, ts: pd.Timestamp) -> pd.Series | None:
    if feature_df.empty:
        return None
    if pd.isna(ts):
        return None
    index_tz = getattr(feature_df.index, "tz", None)
    if index_tz is not None and getattr(ts, "tzinfo", None) is None:
        ts = ts.tz_localize(index_tz)
    elif index_tz is None and getattr(ts, "tzinfo", None) is not None:
        ts = ts.tz_localize(None)
    elif index_tz is not None and getattr(ts, "tzinfo", None) is not None:
        ts = ts.tz_convert(index_tz)
    subset = feature_df.loc[feature_df.index <= ts]
    if subset.empty:
        subset = feature_df.loc[feature_df.index >= ts]
        if subset.empty:
            return None
        return subset.iloc[0]
    return subset.iloc[-1]


def _prepare_market_features(df: pd.DataFrame, cfg: TradingConfig) -> pd.DataFrame:
    out = add_strategy_columns(df, cfg)

    prev_close = out["close"].shift(1)
    true_range = pd.concat(
        [
            out["high"] - out["low"],
            (out["high"] - prev_close).abs(),
            (out["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["atr_14"] = true_range.rolling(14, min_periods=1).mean()
    out["atr_pct"] = out["atr_14"] / out["close"].replace(0, np.nan)

    candle_range = (out["high"] - out["low"]).replace(0, np.nan)
    candle_body = (out["close"] - out["open"]).abs()
    upper_wick = out["high"] - out[["open", "close"]].max(axis=1)
    lower_wick = out[["open", "close"]].min(axis=1) - out["low"]
    out["candle_body_pct"] = candle_body / candle_range
    out["upper_wick_pct"] = upper_wick / candle_range
    out["lower_wick_pct"] = lower_wick / candle_range
    out["wick_to_body_ratio"] = (
        upper_wick + lower_wick) / candle_body.replace(0, np.nan)
    out["candle_strength"] = np.sign(
        out["close"] - out["open"]) * out["candle_body_pct"].fillna(0.0)

    day_open = out.groupby("date")["open"].transform("first")
    out["session_trend_pct"] = (
        out["close"] - day_open) / day_open.replace(0, np.nan)

    daily_high = out.groupby("date", sort=True)["high"].max()
    daily_low = out.groupby("date", sort=True)["low"].min()
    prev_day_high = out["date"].map(daily_high.shift(1))
    prev_day_low = out["date"].map(daily_low.shift(1))
    out["prev_day_high"] = prev_day_high
    out["prev_day_low"] = prev_day_low
    out["prev_day_high_breakout_pct"] = (
        out["close"] - prev_day_high) / prev_day_high.replace(0, np.nan)
    out["prev_day_low_breakout_pct"] = (
        prev_day_low - out["close"]) / prev_day_low.replace(0, np.nan)

    return out


def _benchmark_symbol_for_cfg(cfg: TradingConfig) -> str:
    tz = str(cfg.market_timezone or "").lower()
    if "new_york" in tz or tz in {"us/eastern", "america/new_york"}:
        return "SPY"
    return "^NSEI"


def enrich_trade_features(trades: pd.DataFrame, cfg: TradingConfig) -> pd.DataFrame:
    symbols = sorted(
        {str(sym) for sym in trades["symbol"].dropna().unique() if str(sym) != "None"})
    feature_map: dict[str, pd.DataFrame] = {}
    index_features: pd.DataFrame | None = None
    index_symbol = _benchmark_symbol_for_cfg(cfg)
    fetch_kwargs = dict(max_retries=1, backoff_base=0.5)

    try:
        index_df = fetch_intraday_data(
            index_symbol,
            cfg.interval,
            cfg.period,
            tz=cfg.market_timezone,
            **fetch_kwargs,
        )
        index_features = _prepare_market_features(index_df, cfg)
    except Exception:
        index_features = None

    for symbol in symbols:
        try:
            market_df = fetch_intraday_data(
                symbol,
                cfg.interval,
                cfg.period,
                tz=cfg.market_timezone,
                **fetch_kwargs,
            )
            feature_map[symbol] = _prepare_market_features(market_df, cfg)
        except Exception:
            continue

    # #region agent log
    _agent_dbg_log(
        "H5",
        "optimizer.py:enrich_trade_features",
        "feature_maps_ready",
        {
            "market_timezone": str(cfg.market_timezone),
            "index_bench": index_symbol,
            "index_loaded": index_features is not None,
            "index_rows": int(len(index_features)) if index_features is not None else 0,
            "symbol_maps": int(len(feature_map)),
            "trade_rows": int(len(trades)),
        },
    )
    # #endregion

    enriched_rows: list[dict[str, Any]] = []
    for _, trade in trades.iterrows():
        row = trade.to_dict()
        symbol = str(row.get("symbol", ""))
        entry_ts = row.get("entry_ts")
        feature_df = feature_map.get(symbol)
        feature_row = _nearest_feature_row(
            feature_df, entry_ts) if feature_df is not None else None
        index_row = _nearest_feature_row(
            index_features, entry_ts) if index_features is not None else None

        row["entry_hour"] = int(entry_ts.hour) if pd.notna(entry_ts) else -1
        row["entry_minute"] = int(
            entry_ts.minute) if pd.notna(entry_ts) else -1
        row["entry_bucket"] = (
            f"{int(entry_ts.hour):02d}:{(int(entry_ts.minute) // 30) * 30:02d}" if pd.notna(
                entry_ts) else "unknown"
        )

        if feature_row is None:
            row["volume_spike_ratio"] = np.nan
            row["vwap_diff_pct"] = np.nan
            row["or_breakout_pct"] = np.nan
            row["bar_range_pct"] = np.nan
            row["close_vs_vwap_side_pct"] = np.nan
            row["candle_body_pct"] = np.nan
            row["wick_to_body_ratio"] = np.nan
            row["candle_strength"] = np.nan
            row["atr_pct"] = np.nan
            row["prev_day_high_breakout_pct"] = np.nan
            row["prev_day_low_breakout_pct"] = np.nan
            row["prev_day_breakout_side_pct"] = np.nan
            row["nifty_trend_pct"] = np.nan
            row["nifty_vwap_diff_pct"] = np.nan
            row["nifty_candle_strength"] = np.nan
            row["signal_side_match"] = False
            enriched_rows.append(row)
            continue

        close_price = _safe_float(feature_row.get("close", row.get(
            "entry_price", 0.0)), row.get("entry_price", 0.0))
        vwap = _safe_float(feature_row.get("vwap", close_price), close_price)
        vol_ma = _safe_float(feature_row.get("vol_ma", 0.0), 0.0)
        volume = _safe_float(feature_row.get("volume", 0.0), 0.0)
        or_high = _safe_float(feature_row.get(
            "or_high", close_price), close_price)
        or_low = _safe_float(feature_row.get(
            "or_low", close_price), close_price)
        high = _safe_float(feature_row.get("high", close_price), close_price)
        low = _safe_float(feature_row.get("low", close_price), close_price)

        side = str(row.get("side", "long"))
        volume_spike_ratio = volume / vol_ma if vol_ma > 0 else np.nan
        vwap_diff_pct = ((close_price - vwap) / vwap) if vwap else np.nan

        if side == "short":
            breakout_base = or_low if or_low else close_price
            breakout_pct = ((or_low - close_price) /
                            breakout_base) if breakout_base else np.nan
            side_vwap_edge = ((vwap - close_price) / vwap) if vwap else np.nan
            signal_match = bool(feature_row.get("short_signal", False))
        else:
            breakout_base = or_high if or_high else close_price
            breakout_pct = ((close_price - or_high) /
                            breakout_base) if breakout_base else np.nan
            side_vwap_edge = ((close_price - vwap) / vwap) if vwap else np.nan
            signal_match = bool(feature_row.get("long_signal", False))

        row["volume_spike_ratio"] = volume_spike_ratio
        row["vwap_diff_pct"] = vwap_diff_pct
        row["or_breakout_pct"] = breakout_pct
        row["bar_range_pct"] = (
            (high - low) / close_price) if close_price else np.nan
        row["close_vs_vwap_side_pct"] = side_vwap_edge
        row["candle_body_pct"] = _safe_float(
            feature_row.get("candle_body_pct", np.nan), np.nan)
        row["wick_to_body_ratio"] = _safe_float(
            feature_row.get("wick_to_body_ratio", np.nan), np.nan)
        row["candle_strength"] = _safe_float(
            feature_row.get("candle_strength", np.nan), np.nan)
        row["atr_pct"] = _safe_float(
            feature_row.get("atr_pct", np.nan), np.nan)
        row["prev_day_high_breakout_pct"] = _safe_float(
            feature_row.get("prev_day_high_breakout_pct", np.nan), np.nan)
        row["prev_day_low_breakout_pct"] = _safe_float(
            feature_row.get("prev_day_low_breakout_pct", np.nan), np.nan)
        if side == "short":
            row["prev_day_breakout_side_pct"] = _safe_float(
                feature_row.get("prev_day_low_breakout_pct", np.nan), np.nan)
        else:
            row["prev_day_breakout_side_pct"] = _safe_float(
                feature_row.get("prev_day_high_breakout_pct", np.nan), np.nan)
        if index_row is not None:
            row["nifty_trend_pct"] = _safe_float(
                index_row.get("session_trend_pct", np.nan), np.nan)
            row["nifty_vwap_diff_pct"] = _safe_float(
                index_row.get("vwap_diff_pct", np.nan), np.nan)
            row["nifty_candle_strength"] = _safe_float(
                index_row.get("candle_strength", np.nan), np.nan)
        else:
            row["nifty_trend_pct"] = np.nan
            row["nifty_vwap_diff_pct"] = np.nan
            row["nifty_candle_strength"] = np.nan
        row["signal_side_match"] = signal_match
        enriched_rows.append(row)

    return pd.DataFrame(enriched_rows)


MODEL_FEATURE_COLUMNS = [
    "entry_hour",
    "entry_minute",
    "volume_spike_ratio",
    "vwap_diff_pct",
    "or_breakout_pct",
    "bar_range_pct",
    "close_vs_vwap_side_pct",
    "candle_body_pct",
    "wick_to_body_ratio",
    "candle_strength",
    "atr_pct",
    "prev_day_high_breakout_pct",
    "prev_day_low_breakout_pct",
    "prev_day_breakout_side_pct",
    "nifty_trend_pct",
    "nifty_vwap_diff_pct",
    "nifty_candle_strength",
]


QUALITY_THRESHOLD_DEFAULT = 55.0
MIN_TRAIN_TRADES_DEFAULT = 50


def _ridge_fit(X: np.ndarray, y: np.ndarray, alpha: float = 1.0) -> np.ndarray:
    reg = np.eye(X.shape[1]) * alpha
    reg[0, 0] = 0.0
    gram = X.T @ X + reg
    # #region agent log
    _agent_dbg_log(
        "H1",
        "optimizer.py:_ridge_fit",
        "pre_pinv",
        {
            "n": int(X.shape[0]),
            "p": int(X.shape[1]),
            "alpha": float(alpha),
            "gram_max": float(np.nanmax(np.abs(gram))),
            "gram_has_nan": bool(np.isnan(gram).any()),
            "gram_has_inf": bool(np.isinf(gram).any()),
            "x_max": float(np.nanmax(np.abs(X))),
            "x_has_nan": bool(np.isnan(X).any()),
            "x_has_inf": bool(np.isinf(X).any()),
        },
    )
    # #endregion
    beta = np.linalg.pinv(gram) @ X.T @ y
    # #region agent log
    _agent_dbg_log(
        "H2",
        "optimizer.py:_ridge_fit",
        "post_pinv",
        {
            "beta_max": float(np.nanmax(np.abs(beta))),
            "beta_has_nan": bool(np.isnan(beta).any()),
            "beta_has_inf": bool(np.isinf(beta).any()),
        },
    )
    # #endregion
    return beta


def _prepare_model_matrix(trades: pd.DataFrame) -> tuple[pd.DataFrame, list[str], pd.Series, pd.Series]:
    out = trades.copy()
    out["signal_side_match_num"] = out["signal_side_match"].astype(float)
    feature_cols = MODEL_FEATURE_COLUMNS + ["signal_side_match_num"]
    X_raw = out[feature_cols].apply(pd.to_numeric, errors="coerce")
    y_win = out["is_win"].astype(float)
    y_return = out["return_pct"].clip(-0.10, 0.10)
    return X_raw, feature_cols, y_win, y_return


def _fit_quality_model_on_train(
    X_train_raw: pd.DataFrame,
    y_win_train: pd.Series,
    y_return_train: pd.Series,
) -> dict[str, Any]:
    medians = X_train_raw.median(numeric_only=True).fillna(0.0)
    X_filled = X_train_raw.fillna(medians)
    means = X_filled.mean().fillna(0.0)
    stds = X_filled.std(ddof=0).replace(0, 1.0).fillna(1.0)
    X_scaled = (X_filled - means) / stds
    X_design = np.column_stack(
        [np.ones(len(X_scaled)), X_scaled.to_numpy(dtype=float)])

    beta_win = _ridge_fit(
        X_design, y_win_train.to_numpy(dtype=float), alpha=1.5)
    beta_return = _ridge_fit(
        X_design, y_return_train.to_numpy(dtype=float), alpha=1.5)
    return {
        "medians": medians,
        "means": means,
        "stds": stds,
        "beta_win": beta_win,
        "beta_return": beta_return,
    }


def _score_quality_model(
    X_raw: pd.DataFrame,
    model: dict[str, Any],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    X_filled = X_raw.fillna(model["medians"])
    X_scaled = (X_filled - model["means"]) / model["stds"]
    X_design = np.column_stack(
        [np.ones(len(X_scaled)), X_scaled.to_numpy(dtype=float)])

    raw_win = X_design @ model["beta_win"]
    win_prob = np.clip(raw_win, 0.0, 1.0)
    expected_return = X_design @ model["beta_return"]
    expected_return = np.clip(expected_return, -0.10, 0.10)
    return_score = 0.5 + 0.5 * np.tanh(expected_return / 0.02)
    quality_score = (0.65 * win_prob + 0.35 * return_score) * 100.0
    return win_prob, expected_return, quality_score


def _fit_trade_quality_model(trades: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    out = trades.copy()
    X_raw, feature_cols, y_win, y_return = _prepare_model_matrix(out)

    if len(out) < 8 or out["is_win"].nunique() < 2:
        out["ml_win_prob"] = np.nan
        out["ml_expected_return_pct"] = np.nan
        out["ml_trade_quality_score"] = np.nan
        return (
            out,
            pd.DataFrame(columns=["feature", "win_coef",
                         "return_coef", "importance"]),
            {
                "model_status": "insufficient_data",
                "model_training_trades": int(len(out)),
                "model_feature_count": int(len(feature_cols)),
            },
        )

    model = _fit_quality_model_on_train(X_raw, y_win, y_return)
    win_prob, expected_return, quality_score = _score_quality_model(
        X_raw, model)

    out["ml_win_prob"] = win_prob
    out["ml_expected_return_pct"] = expected_return
    out["ml_trade_quality_score"] = quality_score

    importance = pd.DataFrame(
        {
            "feature": feature_cols,
            "win_coef": model["beta_win"][1:],
            "return_coef": model["beta_return"][1:],
        }
    )
    importance["importance"] = importance["win_coef"].abs() + \
        importance["return_coef"].abs()
    importance = importance.sort_values(
        "importance", ascending=False).reset_index(drop=True)

    model_summary = {
        "model_status": "trained",
        "model_training_trades": int(len(out)),
        "model_feature_count": int(len(feature_cols)),
        "ml_mean_quality_score": float(out["ml_trade_quality_score"].mean()),
        "ml_top_feature": str(importance.iloc[0]["feature"]) if not importance.empty else None,
    }
    return out, importance, model_summary


def _compare_trade_sets(trades: pd.DataFrame, label: str) -> dict[str, Any]:
    if trades.empty:
        return {
            "segment": label,
            "trades": 0,
            "win_rate": 0.0,
            "net_pnl": 0.0,
            "avg_net_pnl": 0.0,
            "avg_return_pct": 0.0,
        }
    return {
        "segment": label,
        "trades": int(len(trades)),
        "win_rate": float(trades["is_win"].mean()),
        "net_pnl": float(trades["net_pnl"].sum()),
        "avg_net_pnl": float(trades["net_pnl"].mean()),
        "avg_return_pct": float(trades["return_pct"].mean()),
    }


def _run_walkforward_validation(
    trades: pd.DataFrame,
    min_train_trades: int = MIN_TRAIN_TRADES_DEFAULT,
    quality_threshold: float = QUALITY_THRESHOLD_DEFAULT,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    out = trades.sort_values("exit_ts").reset_index(drop=True).copy()
    out["wf_ml_win_prob"] = np.nan
    out["wf_ml_expected_return_pct"] = np.nan
    out["wf_ml_trade_quality_score"] = np.nan
    out["wf_model_trained_on"] = 0

    X_raw, _, y_win, y_return = _prepare_model_matrix(out)

    if len(out) <= min_train_trades or out["is_win"].nunique() < 2:
        comparison = pd.DataFrame(
            [
                _compare_trade_sets(pd.DataFrame(
                    columns=out.columns), "baseline"),
                _compare_trade_sets(pd.DataFrame(
                    columns=out.columns), "filtered"),
            ]
        )
        summary = {
            "walkforward_status": "insufficient_data",
            "walkforward_min_train_trades": int(min_train_trades),
            "walkforward_scored_trades": 0,
            "quality_threshold": float(quality_threshold),
        }
        return out, comparison, summary

    scored = 0
    for idx in range(min_train_trades, len(out)):
        train_slice = out.iloc[:idx]
        if train_slice["is_win"].nunique() < 2:
            continue
        model = _fit_quality_model_on_train(
            X_raw.iloc[:idx],
            y_win.iloc[:idx],
            y_return.iloc[:idx],
        )
        win_prob, expected_return, quality_score = _score_quality_model(
            X_raw.iloc[idx:idx + 1],
            model,
        )
        out.loc[idx, "wf_ml_win_prob"] = float(win_prob[0])
        out.loc[idx, "wf_ml_expected_return_pct"] = float(expected_return[0])
        out.loc[idx, "wf_ml_trade_quality_score"] = float(quality_score[0])
        out.loc[idx, "wf_model_trained_on"] = int(idx)
        scored += 1

    scored_trades = out.dropna(subset=["wf_ml_trade_quality_score"]).copy()
    filtered_trades = scored_trades[
        (scored_trades["wf_ml_trade_quality_score"]
         >= float(quality_threshold))
        & (scored_trades["wf_ml_expected_return_pct"] > 0.0)
    ].copy()

    comparison = pd.DataFrame(
        [
            _compare_trade_sets(scored_trades, "baseline"),
            _compare_trade_sets(filtered_trades, "filtered"),
        ]
    )

    baseline_count = int(
        comparison.loc[comparison["segment"] == "baseline", "trades"].iloc[0])
    filtered_count = int(
        comparison.loc[comparison["segment"] == "filtered", "trades"].iloc[0])
    summary = {
        "walkforward_status": "trained" if scored > 0 else "insufficient_data",
        "walkforward_min_train_trades": int(min_train_trades),
        "walkforward_scored_trades": int(scored),
        "quality_threshold": float(quality_threshold),
        "filtered_trade_count": filtered_count,
        "filtered_trade_share": float(filtered_count / baseline_count) if baseline_count else 0.0,
        "baseline_net_pnl": float(comparison.loc[comparison["segment"] == "baseline", "net_pnl"].iloc[0]),
        "filtered_net_pnl": float(comparison.loc[comparison["segment"] == "filtered", "net_pnl"].iloc[0]),
        "baseline_avg_net_pnl": float(comparison.loc[comparison["segment"] == "baseline", "avg_net_pnl"].iloc[0]),
        "filtered_avg_net_pnl": float(comparison.loc[comparison["segment"] == "filtered", "avg_net_pnl"].iloc[0]),
        "baseline_win_rate": float(comparison.loc[comparison["segment"] == "baseline", "win_rate"].iloc[0]),
        "filtered_win_rate": float(comparison.loc[comparison["segment"] == "filtered", "win_rate"].iloc[0]),
    }
    return out, comparison, summary


def _score_group(group: pd.DataFrame) -> dict[str, Any]:
    wins = group[group["net_pnl"] > 0]["net_pnl"].sum()
    losses = abs(group[group["net_pnl"] < 0]["net_pnl"].sum())
    profit_factor = wins / losses if losses > 0 else np.inf
    return {
        "trades": int(len(group)),
        "win_rate": float(group["is_win"].mean()) if len(group) else 0.0,
        "avg_net_pnl": float(group["net_pnl"].mean()) if len(group) else 0.0,
        "total_net_pnl": float(group["net_pnl"].sum()) if len(group) else 0.0,
        "profit_factor": float(profit_factor),
    }


def _bin_series(values: pd.Series, bins: list[float], labels: list[str]) -> pd.Series:
    return pd.cut(values, bins=bins, labels=labels, include_lowest=True)


def _build_feature_scores(trades: pd.DataFrame) -> pd.DataFrame:
    working = trades.copy()
    working["volume_bucket"] = _bin_series(
        working["volume_spike_ratio"],
        [-np.inf, 1.1, 1.3, 1.6, np.inf],
        ["<=1.10", "1.10-1.30", "1.30-1.60", ">1.60"],
    )
    working["breakout_bucket"] = _bin_series(
        working["or_breakout_pct"],
        [-np.inf, 0.0, 0.002, 0.005, np.inf],
        ["<=0", "0-0.2%", "0.2-0.5%", ">0.5%"],
    )
    working["vwap_bucket"] = _bin_series(
        working["close_vs_vwap_side_pct"],
        [-np.inf, 0.0, 0.0025, 0.005, np.inf],
        ["<=0", "0-0.25%", "0.25-0.5%", ">0.5%"],
    )
    working["volatility_bucket"] = _bin_series(
        working["bar_range_pct"],
        [-np.inf, 0.003, 0.008, np.inf],
        ["low", "medium", "high"],
    )
    working["atr_bucket"] = _bin_series(
        working["atr_pct"],
        [-np.inf, 0.004, 0.008, np.inf],
        ["low", "medium", "high"],
    )
    working["candle_strength_bucket"] = _bin_series(
        working["candle_body_pct"],
        [-np.inf, 0.25, 0.55, np.inf],
        ["weak", "medium", "strong"],
    )
    working["prev_day_breakout_bucket"] = _bin_series(
        working["prev_day_breakout_side_pct"],
        [-np.inf, 0.0, 0.003, np.inf],
        ["<=0", "0-0.3%", ">0.3%"],
    )
    working["nifty_trend_bucket"] = _bin_series(
        working["nifty_trend_pct"],
        [-np.inf, -0.002, 0.002, np.inf],
        ["down", "flat", "up"],
    )

    frames: list[pd.DataFrame] = []
    for feature in [
        "symbol",
        "entry_bucket",
        "volume_bucket",
        "breakout_bucket",
        "vwap_bucket",
        "volatility_bucket",
        "atr_bucket",
        "candle_strength_bucket",
        "prev_day_breakout_bucket",
        "nifty_trend_bucket",
    ]:
        grouped = working.dropna(subset=[feature]).groupby(
            feature, dropna=False, observed=False)
        if grouped.ngroups == 0:
            continue
        feature_df = grouped.apply(_score_group)
        normalized = pd.DataFrame(
            list(feature_df.values), index=feature_df.index).reset_index()
        normalized.columns = ["feature_value", "trades", "win_rate",
                              "avg_net_pnl", "total_net_pnl", "profit_factor"]
        normalized.insert(0, "feature_name", feature)
        frames.append(normalized)

    if not frames:
        return pd.DataFrame(
            columns=["feature_name", "feature_value", "trades",
                     "win_rate", "avg_net_pnl", "total_net_pnl", "profit_factor"]
        )

    return pd.concat(frames, ignore_index=True)


def _pick_recommendations(trades: pd.DataFrame, feature_scores: pd.DataFrame, cfg: TradingConfig) -> list[dict[str, Any]]:
    recommendations: list[dict[str, Any]] = []
    overall_avg = float(trades["net_pnl"].mean()) if len(trades) else 0.0

    def add(kind: str, priority: int, message: str, evidence: dict[str, Any]) -> None:
        recommendations.append(
            {
                "kind": kind,
                "priority": priority,
                "message": message,
                "evidence": evidence,
            }
        )

    min_support = max(5, min(12, len(trades) // 8)) if len(trades) else 5
    for _, row in feature_scores.sort_values(["feature_name", "avg_net_pnl"]).iterrows():
        trades_count = int(row["trades"])
        avg_net = float(row["avg_net_pnl"])
        if trades_count < min_support:
            continue

        feature_name = str(row["feature_name"])
        feature_value = str(row["feature_value"])
        evidence = {
            "feature": feature_name,
            "value": feature_value,
            "trades": trades_count,
            "avg_net_pnl": avg_net,
            "win_rate": float(row["win_rate"]),
            "profit_factor": float(row["profit_factor"]),
        }

        if feature_name == "volume_bucket" and avg_net < min(0.0, overall_avg):
            add(
                "avoid_rule",
                1,
                f"Avoid entries when volume spike ratio falls in {feature_value}; expectancy is weaker than baseline.",
                evidence,
            )
        elif feature_name == "entry_bucket" and avg_net < min(0.0, overall_avg):
            add(
                "timing_rule",
                2,
                f"Reduce or skip entries around {feature_value}; this time bucket underperforms.",
                evidence,
            )
        elif feature_name == "symbol" and avg_net > max(0.0, overall_avg):
            add(
                "stock_preference",
                3,
                f"Favor {feature_value}; it has above-baseline expectancy in recent trades.",
                evidence,
            )

    volatility_rows = feature_scores[feature_scores["feature_name"]
                                     == "volatility_bucket"]
    if not volatility_rows.empty:
        best_vol = volatility_rows.sort_values(
            ["avg_net_pnl", "win_rate"], ascending=False).iloc[0]
        worst_vol = volatility_rows.sort_values(
            ["avg_net_pnl", "win_rate"], ascending=True).iloc[0]
        if int(best_vol["trades"]) >= min_support and float(best_vol["avg_net_pnl"]) > 0:
            add(
                "tp_adjustment",
                2,
                f"Increase take-profit above the base {cfg.take_profit_pct:.2%} during {best_vol['feature_value']} volatility setups.",
                {
                    "feature": "volatility_bucket",
                    "value": str(best_vol["feature_value"]),
                    "avg_net_pnl": float(best_vol["avg_net_pnl"]),
                    "win_rate": float(best_vol["win_rate"]),
                    "trades": int(best_vol["trades"]),
                },
            )

    for feature_name, label in [
        ("nifty_trend_bucket", "market index trend"),
        ("prev_day_breakout_bucket", "previous-day breakout"),
        ("candle_strength_bucket", "candle strength"),
    ]:
        subset = feature_scores[feature_scores["feature_name"] == feature_name]
        if subset.empty:
            continue
        best = subset.sort_values(
            ["avg_net_pnl", "win_rate"], ascending=False).iloc[0]
        if int(best["trades"]) >= min_support and float(best["avg_net_pnl"]) > max(0.0, overall_avg):
            add(
                "feature_edge",
                3,
                f"Model context favors {label} = {best['feature_value']}; recent expectancy is above baseline.",
                {
                    "feature": feature_name,
                    "value": str(best["feature_value"]),
                    "avg_net_pnl": float(best["avg_net_pnl"]),
                    "win_rate": float(best["win_rate"]),
                    "trades": int(best["trades"]),
                },
            )
        if int(worst_vol["trades"]) >= min_support and float(worst_vol["avg_net_pnl"]) < 0:
            add(
                "sl_adjustment",
                2,
                f"Avoid or tighten stop-loss for {worst_vol['feature_value']} volatility setups; recent expectancy is negative.",
                {
                    "feature": "volatility_bucket",
                    "value": str(worst_vol["feature_value"]),
                    "avg_net_pnl": float(worst_vol["avg_net_pnl"]),
                    "win_rate": float(worst_vol["win_rate"]),
                    "trades": int(worst_vol["trades"]),
                },
            )

    recommendations.sort(key=lambda item: (
        item["priority"], -item["evidence"].get("trades", 0)))

    deduped: list[dict[str, Any]] = []
    seen_messages: set[str] = set()
    for item in recommendations:
        if item["message"] in seen_messages:
            continue
        deduped.append(item)
        seen_messages.add(item["message"])
    return deduped[:8]


def run_intelligent_optimization(
    trade_file: str | Path,
    cfg: TradingConfig,
    lookback_trades: int = 200,
    min_train_trades: int = MIN_TRAIN_TRADES_DEFAULT,
    quality_threshold: float = QUALITY_THRESHOLD_DEFAULT,
) -> OptimizationReport:
    # #region agent log
    _agent_dbg_log(
        "H4",
        "optimizer.py:run_intelligent_optimization",
        "entry",
        {
            "market_timezone": str(cfg.market_timezone),
            "lookback_trades": int(lookback_trades),
            "min_train_trades": int(min_train_trades),
        },
    )
    # #endregion
    trades, collection_summary = collect_clean_closed_trades(
        trade_file,
        lookback_trades=lookback_trades,
    )

    enriched = enrich_trade_features(trades, cfg)
    enriched, model_feature_importance, model_summary = _fit_trade_quality_model(
        enriched)
    enriched, walkforward_comparison, walkforward_summary = _run_walkforward_validation(
        enriched,
        min_train_trades=min_train_trades,
        quality_threshold=quality_threshold,
    )
    feature_scores = _build_feature_scores(enriched)
    symbol_scores = (
        feature_scores[feature_scores["feature_name"] == "symbol"]
        .sort_values(["avg_net_pnl", "win_rate", "trades"], ascending=[False, False, False])
        .reset_index(drop=True)
    )
    recommendations = _pick_recommendations(enriched, feature_scores, cfg)

    summary = {
        "trade_count": int(len(enriched)),
        "wins": int(enriched["is_win"].sum()),
        "losses": int((~enriched["is_win"]).sum()),
        "win_rate": float(enriched["is_win"].mean()) if len(enriched) else 0.0,
        "net_pnl": float(enriched["net_pnl"].sum()) if len(enriched) else 0.0,
        "avg_net_pnl": float(enriched["net_pnl"].mean()) if len(enriched) else 0.0,
        "symbols_analyzed": int(enriched["symbol"].nunique()),
        "lookback_trades": int(lookback_trades),
        "base_config": {
            "stop_loss_pct": cfg.stop_loss_pct,
            "take_profit_pct": cfg.take_profit_pct,
            "volume_spike_threshold": cfg.volume_spike_threshold,
            "opening_range_minutes": cfg.opening_range_minutes,
        },
    }
    summary.update(collection_summary)
    summary.update(model_summary)
    summary.update(walkforward_summary)

    return OptimizationReport(
        summary=summary,
        recommendations=recommendations,
        symbol_scores=symbol_scores,
        feature_scores=feature_scores,
        model_feature_importance=model_feature_importance,
        walkforward_comparison=walkforward_comparison,
        trades=enriched,
    )


def export_optimization_report(report: OptimizationReport, out_dir: str | Path, prefix: str) -> dict[str, Path]:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    json_path = out_path / f"{prefix}_recommendations.json"
    symbols_path = out_path / f"{prefix}_symbol_scores.csv"
    features_path = out_path / f"{prefix}_feature_scores.csv"
    model_path = out_path / f"{prefix}_model_feature_importance.csv"
    walkforward_path = out_path / f"{prefix}_walkforward_comparison.csv"
    clean_trades_path = out_path / f"{prefix}_clean_closed_trades.csv"
    trades_path = out_path / f"{prefix}_enriched_trades.csv"

    payload = {
        "summary": report.summary,
        "recommendations": report.recommendations,
    }
    json_path.write_text(pd.Series(payload).to_json(
        indent=2), encoding="utf-8")
    report.symbol_scores.to_csv(symbols_path, index=False)
    report.feature_scores.to_csv(features_path, index=False)
    report.model_feature_importance.to_csv(model_path, index=False)
    report.walkforward_comparison.to_csv(walkforward_path, index=False)
    report.trades[[
        "trade_source",
        "symbol",
        "side",
        "entry_ts",
        "exit_ts",
        "qty",
        "entry_price",
        "exit_price",
        "net_pnl",
        "commission",
        "exit_reason",
        "holding_minutes",
        "return_pct",
        "is_win",
    ]].to_csv(clean_trades_path, index=False)
    report.trades.to_csv(trades_path, index=False)

    return {
        "recommendations_json": json_path,
        "symbol_scores_csv": symbols_path,
        "feature_scores_csv": features_path,
        "model_feature_importance_csv": model_path,
        "walkforward_comparison_csv": walkforward_path,
        "clean_closed_trades_csv": clean_trades_path,
        "enriched_trades_csv": trades_path,
    }
