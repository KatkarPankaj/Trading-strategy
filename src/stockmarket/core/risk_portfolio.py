"""Portfolio-level hard risk controls; every control is enforced unless explicitly disabled with a reason."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from types import MappingProxyType
from typing import Mapping

from .models import Order

CONTROLS = (
    "max_risk_per_trade_pct",
    "max_daily_loss_pct",
    "max_drawdown_pct",
    "max_position_notional_pct",
    "max_total_notional_pct",
    "max_sector_exposure_pct",
    "max_correlated_exposure_pct",
    "max_leverage",
    "max_orders_per_minute",
    "min_liquidity",
    "max_slippage_bps",
    "duplicate_order_prevention",
)
_INTEGER_CONTROLS = {"max_orders_per_minute"}
_FLAG_CONTROLS = {"duplicate_order_prevention"}


def _finite(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and isfinite(value)


@dataclass(frozen=True, slots=True)
class PortfolioRiskLimits:
    """Fractions are of equity. A control left as None must be named in `disabled` with a reason."""

    max_risk_per_trade_pct: float | None = None
    max_daily_loss_pct: float | None = None
    max_drawdown_pct: float | None = None
    max_position_notional_pct: float | None = None
    max_total_notional_pct: float | None = None
    max_sector_exposure_pct: float | None = None
    max_correlated_exposure_pct: float | None = None
    max_leverage: float | None = None
    max_orders_per_minute: int | None = None
    min_liquidity: float | None = None
    max_slippage_bps: float | None = None
    duplicate_order_prevention: bool | None = None
    disabled: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        unknown = set(self.disabled) - set(CONTROLS)
        if unknown:
            raise ValueError(
                f"unknown controls in disabled: {sorted(unknown)}")
        for name, reason in self.disabled.items():
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError(
                    f"disabling {name} requires a non-empty reason")
        for name in CONTROLS:
            value = getattr(self, name)
            if name in self.disabled:
                if value is not None:
                    raise ValueError(f"{name} is both configured and disabled")
                continue
            if value is None:
                raise ValueError(
                    f"{name} must be configured or explicitly disabled with a reason")
            if name in _FLAG_CONTROLS:
                if value is not True:
                    raise ValueError(
                        f"{name} can only be turned off via `disabled`")
            elif name in _INTEGER_CONTROLS:
                if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                    raise ValueError(f"{name} must be a positive integer")
            elif not _finite(value) or value <= 0:
                raise ValueError(
                    f"{name} must be a finite number greater than zero")
        for name in ("max_risk_per_trade_pct", "max_daily_loss_pct", "max_drawdown_pct"):
            value = getattr(self, name)
            if value is not None and value > 1:
                raise ValueError(f"{name} must not exceed 1 (100% of equity)")
        object.__setattr__(
            self, "disabled", MappingProxyType(dict(self.disabled)))


@dataclass(frozen=True, slots=True)
class PortfolioRiskState:
    """Caller-supplied portfolio snapshot; all money values share one currency."""

    equity: float
    peak_equity: float
    daily_pnl: float
    gross_notional_exposure: float
    sector: str | None = None
    sector_exposure: Mapping[str, float] = field(default_factory=dict)
    correlated_exposure: float | None = None
    current_position_notional: float = 0.0
    orders_last_minute: int | None = None
    recent_order_keys: frozenset[str] = frozenset()
    average_daily_traded_value: float | None = None
    expected_slippage_bps: float | None = None


def order_key_for(instrument_id: str, side: str, signal_id: object, quantity: int) -> str:
    suffix = str(signal_id) if signal_id is not None else f"qty{quantity}"
    return f"{instrument_id}|{side}|{suffix}"


def order_key(order: Order) -> str:
    """Stable duplicate-detection key: the signal when known, else side and size."""
    return order_key_for(order.instrument_id, order.side.value, order.signal_id, order.quantity)


def _bad(code: str, message: str) -> tuple[str, str]:
    return code, message


def check_portfolio_limits(
    limits: PortfolioRiskLimits,
    state: object,
    order: Order,
    *,
    reference_price: float,
    notional: float,
    stop_loss: float | None,
) -> tuple[str, str] | None:
    """Return (code, explanation) for the first breached control, else None."""
    if not isinstance(state, PortfolioRiskState):
        return _bad("PORTFOLIO_STATE_MISSING",
                    "A PortfolioRiskState is required while portfolio limits are configured.")
    numbers = (state.equity, state.peak_equity, state.daily_pnl,
               state.gross_notional_exposure, state.current_position_notional)
    if not all(_finite(v) for v in numbers) or state.equity <= 0 or state.peak_equity <= 0:
        return _bad("PORTFOLIO_STATE_INVALID",
                    "Equity and peak equity must be positive and all exposures finite.")
    if state.gross_notional_exposure < 0 or state.current_position_notional < 0:
        return _bad("PORTFOLIO_STATE_INVALID", "Exposures must be non-negative.")

    def active(name): return name not in limits.disabled  # noqa: E731
    equity = state.equity

    if active("max_drawdown_pct"):
        drawdown = max(0.0, (state.peak_equity - equity) / state.peak_equity)
        if drawdown >= limits.max_drawdown_pct:
            return _bad("MAX_DRAWDOWN_BREACHED",
                        f"Drawdown {drawdown:.4f} has reached the limit {limits.max_drawdown_pct:.4f}.")

    if active("max_daily_loss_pct"):
        start_equity = equity - state.daily_pnl
        if start_equity <= 0:
            return _bad("PORTFOLIO_STATE_INVALID", "Start-of-day equity must be positive.")
        loss = max(0.0, -state.daily_pnl) / start_equity
        if loss >= limits.max_daily_loss_pct:
            return _bad("MAX_DAILY_LOSS_BREACHED",
                        f"Daily loss {loss:.4f} has reached the limit {limits.max_daily_loss_pct:.4f}.")

    if active("max_risk_per_trade_pct"):
        if stop_loss is None:
            return _bad("RISK_PER_TRADE_UNKNOWN",
                        "A stop-loss is required to measure risk per trade.")
        risk = abs(reference_price - stop_loss) * order.quantity
        if risk > limits.max_risk_per_trade_pct * equity:
            return _bad("MAX_RISK_PER_TRADE_EXCEEDED",
                        f"Trade risk {risk:.2f} exceeds {limits.max_risk_per_trade_pct:.4f} of equity ({equity:.2f}).")

    if active("max_position_notional_pct"):
        resulting = state.current_position_notional + notional
        if resulting > limits.max_position_notional_pct * equity:
            return _bad("MAX_POSITION_SIZE_EXCEEDED",
                        f"Position notional {resulting:.2f} exceeds {limits.max_position_notional_pct:.4f} of equity.")

    resulting_gross = state.gross_notional_exposure + notional
    if active("max_total_notional_pct") and resulting_gross > limits.max_total_notional_pct * equity:
        return _bad("MAX_NOTIONAL_EXPOSURE_EXCEEDED",
                    f"Gross exposure {resulting_gross:.2f} exceeds {limits.max_total_notional_pct:.4f} of equity.")
    if active("max_leverage") and resulting_gross / equity > limits.max_leverage:
        return _bad("MAX_LEVERAGE_EXCEEDED",
                    f"Leverage {resulting_gross / equity:.3f} exceeds the limit {limits.max_leverage:.3f}.")

    if active("max_sector_exposure_pct"):
        if not state.sector:
            return _bad("SECTOR_UNKNOWN", "Instrument sector is required for the sector exposure limit.")
        sector_total = float(state.sector_exposure.get(
            state.sector, 0.0)) + notional
        if sector_total > limits.max_sector_exposure_pct * equity:
            return _bad("MAX_SECTOR_EXPOSURE_EXCEEDED",
                        f"Sector '{state.sector}' exposure {sector_total:.2f} exceeds {limits.max_sector_exposure_pct:.4f} of equity.")

    if active("max_correlated_exposure_pct"):
        if state.correlated_exposure is None or not _finite(state.correlated_exposure) or state.correlated_exposure < 0:
            return _bad("CORRELATED_EXPOSURE_UNKNOWN",
                        "A valid correlated-exposure figure is required.")
        correlated = state.correlated_exposure + notional
        if correlated > limits.max_correlated_exposure_pct * equity:
            return _bad("MAX_CORRELATED_EXPOSURE_EXCEEDED",
                        f"Correlated exposure {correlated:.2f} exceeds {limits.max_correlated_exposure_pct:.4f} of equity.")

    if active("max_orders_per_minute"):
        count = state.orders_last_minute
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            return _bad("ORDER_RATE_UNKNOWN", "A valid orders-in-last-minute count is required.")
        if count >= limits.max_orders_per_minute:
            return _bad("MAX_ORDERS_PER_MINUTE_EXCEEDED",
                        f"{count} orders in the last minute reached the limit {limits.max_orders_per_minute}.")

    if active("min_liquidity"):
        liquidity = state.average_daily_traded_value
        if liquidity is None or not _finite(liquidity):
            return _bad("LIQUIDITY_UNKNOWN", "Average daily traded value is required.")
        if liquidity < limits.min_liquidity:
            return _bad("MIN_LIQUIDITY_NOT_MET",
                        f"Liquidity {liquidity:.2f} is below the minimum {limits.min_liquidity:.2f}.")

    if active("max_slippage_bps"):
        slip = state.expected_slippage_bps
        if slip is None or not _finite(slip) or slip < 0:
            return _bad("SLIPPAGE_UNKNOWN", "A valid expected slippage estimate is required.")
        if slip > limits.max_slippage_bps:
            return _bad("MAX_SLIPPAGE_EXCEEDED",
                        f"Expected slippage {slip:.2f} bps exceeds the limit {limits.max_slippage_bps:.2f} bps.")

    if active("duplicate_order_prevention") and order_key(order) in state.recent_order_keys:
        return _bad("DUPLICATE_ORDER", "An equivalent order is already open or was just submitted.")

    return None
