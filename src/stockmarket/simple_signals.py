"""Build scan rows from normalized quote fields (simple dashboard)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ScoreConfig:
    score_change_weight: float = 6.0
    score_vwap_weight: float = 20.0
    score_range_weight: float = 2.0
    ready_pchange_threshold: float = 0.25
    ready_range_threshold: float = 0.5


def scan_row_from_quote(
    symbol: str,
    price: float,
    vwap: float,
    pchange: float,
    range_pct: float,
    updated_ts: str,
    score_cfg: dict[str, float] | ScoreConfig,
) -> dict[str, Any]:
    if isinstance(score_cfg, ScoreConfig):
        cfg = score_cfg
    else:
        cfg = ScoreConfig(
            score_change_weight=float(score_cfg.get("score_change_weight", 6.0)),
            score_vwap_weight=float(score_cfg.get("score_vwap_weight", 20.0)),
            score_range_weight=float(score_cfg.get("score_range_weight", 2.0)),
            ready_pchange_threshold=float(
                score_cfg.get("ready_pchange_threshold", 0.25)
            ),
            ready_range_threshold=float(score_cfg.get("ready_range_threshold", 0.5)),
        )

    buy_score = 0.0
    sell_score = 0.0
    buy_ready = False
    sell_ready = False

    if vwap > 0:
        above = max(0.0, (price - vwap) / vwap * 100.0)
        below = max(0.0, (vwap - price) / vwap * 100.0)
    else:
        above = 0.0
        below = 0.0

    buy_score += min(45.0, max(0.0, pchange) * cfg.score_change_weight)
    buy_score += min(35.0, above * cfg.score_vwap_weight)
    buy_score += min(20.0, range_pct * cfg.score_range_weight)
    buy_ready = (
        price > vwap
        and pchange > cfg.ready_pchange_threshold
        and range_pct > cfg.ready_range_threshold
    )

    sell_score += min(45.0, max(0.0, -pchange) * cfg.score_change_weight)
    sell_score += min(35.0, below * cfg.score_vwap_weight)
    sell_score += min(20.0, range_pct * cfg.score_range_weight)
    sell_ready = (
        price < vwap
        and pchange < -cfg.ready_pchange_threshold
        and range_pct > cfg.ready_range_threshold
    )

    return {
        "symbol": symbol,
        "price": round(price, 2),
        "buy_score": round(buy_score, 2),
        "buy_signal": "READY" if buy_ready else "WAIT",
        "sell_score": round(sell_score, 2),
        "sell_signal": "READY" if sell_ready else "WAIT",
        "pchange": round(pchange, 2),
        "range_pct": round(range_pct, 2),
        "vwap_gap_pct": round((price - vwap) / vwap * 100.0, 2) if vwap > 0 else 0.0,
        "updated": updated_ts,
    }
