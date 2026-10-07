"""Deterministic signal aggregation: scored inputs in, reproducible decision out."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from math import isfinite
from typing import Any, Mapping
from uuid import UUID, uuid5

from .models import Signal, SignalSide

EXPLANATION_VERSION = 1
_NAMESPACE = UUID("6f1c2b0e-3d4a-4b8e-9a57-0c1d2e3f4a5b")

# Directional components are scored in [-1, 1]: -1 strongly bearish, +1 strongly bullish.
DIRECTIONAL_COMPONENTS = (
    "technical",
    "volume",
    "momentum",
    "regime",
    "sector",
    "news",
    "fundamental",
    "history",
)


class AggregatedAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    SKIP = "SKIP"


def _require_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


def _require_finite(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError(f"{name} must be a finite number")


def _optional_score(value: float | None, name: str) -> None:
    if value is not None:
        _require_finite(value, name)
        if not -1.0 <= value <= 1.0:
            raise ValueError(f"{name} must be between -1 and 1")


@dataclass(frozen=True, slots=True)
class SignalInputs:
    """Every input to one aggregation. None means the input is unavailable."""

    instrument_id: str
    symbol: str
    timestamp: datetime
    strategy: str
    technical: float | None = None
    volume: float | None = None
    momentum: float | None = None
    regime: float | None = None
    sector: float | None = None
    news: float | None = None
    fundamental: float | None = None
    history: float | None = None
    history_trades: int = 0
    volatility: float | None = None  # fractional, e.g. ATR / price
    liquidity: float | None = None  # average traded value, instrument currency

    def __post_init__(self) -> None:
        for name in ("instrument_id", "symbol", "strategy"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        _require_aware(self.timestamp, "timestamp")
        for name in DIRECTIONAL_COMPONENTS:
            _optional_score(getattr(self, name), name)
        if isinstance(self.history_trades, bool) or not isinstance(self.history_trades, int) or self.history_trades < 0:
            raise ValueError("history_trades must be a non-negative integer")
        for name in ("volatility", "liquidity"):
            value = getattr(self, name)
            if value is not None:
                _require_finite(value, name)
                if value < 0:
                    raise ValueError(f"{name} must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "symbol": self.symbol,
            "timestamp": self.timestamp.isoformat(),
            "strategy": self.strategy,
            **{name: getattr(self, name) for name in DIRECTIONAL_COMPONENTS},
            "history_trades": self.history_trades,
            "volatility": self.volatility,
            "liquidity": self.liquidity,
        }


def _default_weights() -> dict[str, float]:
    return {
        "technical": 0.25,
        "volume": 0.10,
        "momentum": 0.15,
        "regime": 0.15,
        "sector": 0.10,
        "news": 0.10,
        "fundamental": 0.05,
        "history": 0.10,
    }


@dataclass(frozen=True, slots=True)
class AggregationConfig:
    weights: Mapping[str, float] = field(default_factory=_default_weights)
    buy_threshold: float = 0.25
    sell_threshold: float = 0.25
    min_confidence: float = 40.0
    min_components: int = 3
    required_components: tuple[str, ...] = ("technical",)
    min_history_trades: int = 20
    min_liquidity: float = 0.0
    max_volatility: float = 0.10
    max_input_age_seconds: float = 300.0

    def __post_init__(self) -> None:
        if set(self.weights) != set(DIRECTIONAL_COMPONENTS):
            raise ValueError(
                f"weights must define exactly: {', '.join(DIRECTIONAL_COMPONENTS)}")
        for name, weight in self.weights.items():
            _require_finite(weight, f"weight[{name}]")
            if weight < 0:
                raise ValueError(f"weight[{name}] must be non-negative")
        if sum(self.weights.values()) <= 0:
            raise ValueError("weights must have a positive sum")
        for name in ("buy_threshold", "sell_threshold"):
            value = getattr(self, name)
            _require_finite(value, name)
            if not 0 < value <= 1:
                raise ValueError(f"{name} must be in (0, 1]")
        _require_finite(self.min_confidence, "min_confidence")
        if not 0 <= self.min_confidence <= 100:
            raise ValueError("min_confidence must be between 0 and 100")
        if self.min_components < 1 or self.min_history_trades < 0:
            raise ValueError(
                "min_components must be >= 1 and min_history_trades >= 0")
        unknown = set(self.required_components) - set(DIRECTIONAL_COMPONENTS)
        if unknown:
            raise ValueError(f"unknown required components: {sorted(unknown)}")
        _require_finite(self.min_liquidity, "min_liquidity")
        _require_finite(self.max_volatility, "max_volatility")
        _require_finite(self.max_input_age_seconds, "max_input_age_seconds")
        if self.min_liquidity < 0 or self.max_volatility <= 0 or self.max_input_age_seconds <= 0:
            raise ValueError("invalid liquidity, volatility, or age limit")

    def to_dict(self) -> dict[str, Any]:
        return {
            "weights": {k: self.weights[k] for k in sorted(self.weights)},
            "buy_threshold": self.buy_threshold,
            "sell_threshold": self.sell_threshold,
            "min_confidence": self.min_confidence,
            "min_components": self.min_components,
            "required_components": list(self.required_components),
            "min_history_trades": self.min_history_trades,
            "min_liquidity": self.min_liquidity,
            "max_volatility": self.max_volatility,
            "max_input_age_seconds": self.max_input_age_seconds,
        }


@dataclass(frozen=True, slots=True)
class AggregatedDecision:
    action: AggregatedAction
    confidence: float
    reason_codes: tuple[str, ...]
    explanation: Mapping[str, Any]
    input_hash: str
    decision_id: UUID
    instrument_id: str
    symbol: str
    strategy: str
    timestamp: datetime

    def explanation_json(self) -> str:
        return _canonical(self.explanation)

    def to_signal(
        self,
        *,
        entry_price: float | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        regime: str = "UNKNOWN",
    ) -> Signal:
        """Map to a domain Signal; SKIP maps to HOLD and carries no prices."""
        side = {
            AggregatedAction.BUY: SignalSide.BUY,
            AggregatedAction.SELL: SignalSide.SELL,
        }.get(self.action, SignalSide.HOLD)
        priced = side is not SignalSide.HOLD
        return Signal(
            instrument_id=self.instrument_id,
            symbol=self.symbol,
            timestamp=self.timestamp,
            strategy=self.strategy,
            side=side,
            entry_price=entry_price if priced else None,
            stop_loss=stop_loss if priced else None,
            take_profit=take_profit if priced else None,
            confidence=self.confidence,
            regime=regime,
            reasons=self.reason_codes or ("NO_REASON",),
            signal_id=self.decision_id,
        )


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _r(value: float) -> float:
    return round(value, 6)


class SignalAggregator:
    """Aggregate evidence deterministically; an optional strategy signal gates direction."""

    def __init__(self, config: AggregationConfig | None = None) -> None:
        self.config = config or AggregationConfig()

    def aggregate(
        self,
        inputs: SignalInputs,
        *,
        as_of: datetime,
        strategy_signal: Signal | None = None,
    ) -> AggregatedDecision:
        _require_aware(as_of, "as_of")
        cfg = self.config
        input_dict = inputs.to_dict()
        scores: dict[str, float | None] = {
            name: getattr(inputs, name) for name in DIRECTIONAL_COMPONENTS}
        excluded: dict[str, str] = {}
        strategy_side: SignalSide | None = None
        strategy_signal_skip: str | None = None
        strategy_signal_age: float | None = None
        if strategy_signal is not None:
            if not isinstance(strategy_signal, Signal):
                raise TypeError("strategy_signal must be a Signal")
            if (strategy_signal.instrument_id != inputs.instrument_id
                    or strategy_signal.symbol != inputs.symbol
                    or strategy_signal.strategy != inputs.strategy):
                raise ValueError(
                    "strategy_signal instrument, symbol and strategy must match aggregation inputs")
            strategy_side = strategy_signal.side
            signal_payload = {
                "signal_id": str(strategy_signal.signal_id),
                "instrument_id": strategy_signal.instrument_id,
                "symbol": strategy_signal.symbol,
                "timestamp": strategy_signal.timestamp.isoformat(),
                "strategy": strategy_signal.strategy,
                "side": strategy_side.value,
                "entry_price": strategy_signal.entry_price,
                "stop_loss": strategy_signal.stop_loss,
                "take_profit": strategy_signal.take_profit,
                "reasons": list(strategy_signal.reasons),
            }
            input_dict["strategy_signal"] = signal_payload
            signal_age = (as_of - strategy_signal.timestamp).total_seconds()
            strategy_signal_age = _r(signal_age)
            if signal_age < 0:
                strategy_signal_skip = "STRATEGY_SIGNAL_FROM_FUTURE"
            elif signal_age > cfg.max_input_age_seconds:
                strategy_signal_skip = "STALE_STRATEGY_SIGNAL"
            elif strategy_side is not SignalSide.HOLD and strategy_signal.entry_price is None:
                strategy_signal_skip = "UNPRICED_STRATEGY_SIGNAL"
            else:
                strategy_signal_skip = None
            if strategy_signal_skip is not None:
                excluded["technical"] = strategy_signal_skip
                scores["technical"] = None
            else:
                scores["technical"] = {
                    SignalSide.BUY: 1.0,
                    SignalSide.SELL: -1.0,
                    SignalSide.HOLD: 0.0,
                }[strategy_side]
            input_dict["provided_technical_score"] = inputs.technical
            input_dict["technical"] = scores["technical"]
        if scores["history"] is not None and inputs.history_trades < cfg.min_history_trades:
            scores["history"] = None
            excluded["history"] = "INSUFFICIENT_HISTORY_TRADES"

        available = {k: v for k, v in scores.items() if v is not None}
        age = (as_of - inputs.timestamp).total_seconds()

        skip: list[str] = []
        if strategy_signal is not None and strategy_signal_skip is not None:
            skip.append(strategy_signal_skip)
        if age < 0:
            skip.append("INPUT_FROM_FUTURE")
        elif age > cfg.max_input_age_seconds:
            skip.append("STALE_INPUTS")
        for name in cfg.required_components:
            if name not in available:
                skip.append(f"MISSING_REQUIRED_{name.upper()}")
        if len(available) < cfg.min_components:
            skip.append("INSUFFICIENT_COMPONENTS")
        if cfg.min_liquidity > 0 and (inputs.liquidity is None or inputs.liquidity < cfg.min_liquidity):
            skip.append("LOW_OR_UNKNOWN_LIQUIDITY")
        if inputs.volatility is not None and inputs.volatility > cfg.max_volatility:
            skip.append("EXCESSIVE_VOLATILITY")

        total_weight = sum(cfg.weights.values())
        avail_weight = sum(cfg.weights[k] for k in available)
        components: dict[str, Any] = {}
        score = 0.0
        coverage = 0.0
        agreement = 0.0
        confidence = 0.0
        action = AggregatedAction.SKIP
        reasons = list(skip)

        if avail_weight > 0:
            coverage = avail_weight / total_weight
            for name in DIRECTIONAL_COMPONENTS:
                if name in available:
                    eff = cfg.weights[name] / avail_weight
                    contrib = eff * available[name]
                    score += contrib
                    components[name] = {
                        "score": _r(available[name]),
                        "effective_weight": _r(eff),
                        "contribution": _r(contrib),
                    }
                else:
                    components[name] = {
                        "score": None,
                        "excluded_reason": excluded.get(name, "UNAVAILABLE"),
                    }
            if score != 0:
                agreeing = sum(
                    cfg.weights[k] for k, v in available.items() if v * score > 0)
                agreement = agreeing / avail_weight

        if not skip:
            if score >= cfg.buy_threshold:
                action = AggregatedAction.BUY
            elif score <= -cfg.sell_threshold:
                action = AggregatedAction.SELL
            else:
                action = AggregatedAction.HOLD
                reasons.append("SCORE_WITHIN_NEUTRAL_BAND")

            if strategy_signal is not None and action in (
                    AggregatedAction.BUY, AggregatedAction.SELL):
                expected_action = {
                    SignalSide.BUY: AggregatedAction.BUY,
                    SignalSide.SELL: AggregatedAction.SELL,
                }.get(strategy_side)
                if expected_action is None:
                    action = AggregatedAction.HOLD
                    reasons.append("STRATEGY_SIGNAL_HOLD")
                elif action is not expected_action:
                    action = AggregatedAction.HOLD
                    reasons.append("STRATEGY_AGGREGATION_CONFLICT")

            vol_factor = 1.0
            if inputs.volatility is not None:
                vol_factor = 1.0 - 0.5 * \
                    (inputs.volatility / cfg.max_volatility)

            if action is AggregatedAction.HOLD:
                threshold = cfg.buy_threshold if score >= 0 else cfg.sell_threshold
                confidence = 100.0 * coverage * \
                    (1.0 - min(1.0, abs(score) / threshold))
            else:
                confidence = 100.0 * (
                    0.5 * min(1.0, abs(score)) + 0.3 *
                    agreement + 0.2 * coverage
                ) * vol_factor
                if confidence < cfg.min_confidence:
                    action = AggregatedAction.HOLD
                    reasons.append("LOW_CONFIDENCE")
                else:
                    reasons.append("SCORE_ABOVE_BUY_THRESHOLD" if action is AggregatedAction.BUY
                                   else "SCORE_BELOW_SELL_THRESHOLD")
            confidence = max(0.0, min(100.0, confidence))

        explanation: dict[str, Any] = {
            "version": EXPLANATION_VERSION,
            "as_of": as_of.isoformat(),
            "input_age_seconds": _r(age),
            "inputs": input_dict,
            "config": cfg.to_dict(),
            "components": components,
            "gates": {
                "skip_reasons": skip,
                "strategy_signal_side": strategy_side.value if strategy_side else None,
                "strategy_signal_age_seconds": strategy_signal_age,
                "volatility": inputs.volatility,
                "liquidity": inputs.liquidity,
            },
            "aggregate": {
                "weighted_score": _r(score),
                "coverage": _r(coverage),
                "agreement": _r(agreement),
                "confidence": _r(confidence),
            },
            "action": action.value,
            "reason_codes": reasons,
        }
        input_hash = hashlib.sha256(_canonical({
            "inputs": input_dict, "config": cfg.to_dict(), "as_of": as_of.isoformat(),
            "version": EXPLANATION_VERSION,
        }).encode("utf-8")).hexdigest()
        explanation["input_hash"] = input_hash

        return AggregatedDecision(
            action=action,
            confidence=_r(confidence),
            reason_codes=tuple(reasons),
            explanation=explanation,
            input_hash=input_hash,
            decision_id=uuid5(_NAMESPACE, input_hash),
            instrument_id=inputs.instrument_id,
            symbol=inputs.symbol,
            strategy=inputs.strategy,
            timestamp=inputs.timestamp,
        )
