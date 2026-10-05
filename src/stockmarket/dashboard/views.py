"""Display shaping only: turns API payloads into frames and labels. No trading decisions here."""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

import pandas as pd


def mode_banner(health: Mapping[str, Any] | None) -> tuple[str, str]:
    """(label, level). If the mode cannot be confirmed it is shown as unknown, never assumed to be paper."""
    mode = (health or {}).get("trading_mode")
    if mode == "LIVE":
        return "LIVE MODE", "live"
    if mode == "PAPER":
        return "PAPER MODE", "paper"
    return "MODE UNKNOWN - API UNREACHABLE", "unknown"


def frame(rows: Iterable[Mapping[str, Any]] | None) -> pd.DataFrame:
    return pd.DataFrame(list(rows or []))


def _payload(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("payload") or row.get("explanation") or "{}"
    try:
        return json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        return {}


def top_signals(signals: list[dict[str, Any]], limit: int = 20) -> pd.DataFrame:
    df = frame(signals)
    if df.empty:
        return df
    df["regime"] = [_payload(s).get("regime") for s in signals]
    return df.sort_values("confidence", ascending=False).head(limit)[
        ["timestamp", "symbol", "strategy", "side", "confidence", "regime"]]


def regime_counts(signals: list[dict[str, Any]]) -> pd.DataFrame:
    regimes = [_payload(s).get("regime") or "UNKNOWN" for s in signals]
    return pd.Series(regimes, name="signals").value_counts().rename_axis("regime").reset_index() if regimes \
        else pd.DataFrame(columns=["regime", "signals"])


def pnl_curves(snapshots: list[dict[str, Any]]) -> pd.DataFrame:
    df = frame(snapshots)
    if df.empty:
        return df
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df.set_index("timestamp")[["equity", "realized_pnl", "unrealized_pnl", "daily_pnl", "drawdown"]]


def strategy_performance(trades: list[dict[str, Any]]) -> pd.DataFrame:
    df = frame(trades)
    if df.empty:
        return df
    grouped = df.groupby("strategy")["net_pnl"]
    out = pd.DataFrame({
        "trades": grouped.count(),
        "net_pnl": grouped.sum(),
        "win_rate": grouped.apply(lambda s: (s > 0).mean()),
        "average_pnl": grouped.mean(),
    })
    return out.reset_index()


def rejection_summary(decisions: list[dict[str, Any]]) -> pd.DataFrame:
    rejected = [d for d in decisions if d.get("status") == "REJECTED"]
    codes = [(d.get("reason") or "UNKNOWN").split(":", 1)[0] for d in rejected]
    return pd.Series(codes, name="count").value_counts().rename_axis("reason_code").reset_index() if codes \
        else pd.DataFrame(columns=["reason_code", "count"])


def health_checks(health: Mapping[str, Any]) -> pd.DataFrame:
    return pd.DataFrame([{"check": name, **detail} for name, detail in (health.get("checks") or {}).items()])
