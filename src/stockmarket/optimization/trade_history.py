from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


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

