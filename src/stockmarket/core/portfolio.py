"""Multi-currency portfolio accounting: cash, positions, PnL, exposure and drawdown."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable
from math import isfinite
from zoneinfo import ZoneInfo

from .models import Instrument, OrderSide, PositionSide
from .risk_portfolio import PortfolioRiskState


class PortfolioError(ValueError):
    """Raised for invalid fills, missing prices/FX rates, or insufficient cash."""


def _finite(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and isfinite(value)


def _check_positive(value: object, name: str) -> None:
    if not _finite(value) or value <= 0:  # type: ignore[operator]
        raise PortfolioError(f"{name} must be finite and greater than zero")


def _check_non_negative(value: object, name: str) -> None:
    if not _finite(value) or value < 0:  # type: ignore[operator]
        raise PortfolioError(f"{name} must be finite and non-negative")


def _check_aware(value: object, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PortfolioError(f"{name} must be a timezone-aware datetime")


@dataclass(slots=True)
class _Position:
    instrument_id: str
    symbol: str
    currency: str
    sector: str | None
    quantity: int  # signed: positive long, negative short
    average_entry: float
    last_price: float
    opened_at: datetime


@dataclass(frozen=True, slots=True)
class PositionView:
    instrument_id: str
    symbol: str
    side: PositionSide
    quantity: int
    average_entry_price: float
    last_price: float
    currency: str
    sector: str | None
    market_value: float  # signed, local currency
    unrealized_pnl: float  # local currency
    unrealized_pnl_base: float
    opened_at: datetime


@dataclass(frozen=True, slots=True)
class FillRecord:
    timestamp: datetime
    instrument_id: str
    side: OrderSide
    quantity: int
    price: float
    fee: float
    slippage: float
    currency: str
    realized_pnl: float  # local currency, before fees
    client_order_id: str | None = None
    fill_sequence: int | None = None


class PortfolioManager:
    """Single source of truth for portfolio state. All aggregates are in the base currency."""

    def __init__(
        self,
        base_currency: str,
        initial_cash: float,
        *,
        timezone: str = "UTC",
        allow_negative_cash: bool = False,
    ) -> None:
        if not isinstance(base_currency, str) or not base_currency.strip():
            raise PortfolioError("base_currency must be a non-empty string")
        _check_non_negative(initial_cash, "initial_cash")
        self.base_currency = base_currency.upper()
        self._tz = ZoneInfo(timezone)
        self._allow_negative_cash = allow_negative_cash
        self._cash: dict[str, float] = {
            self.base_currency: float(initial_cash)}
        self._fx: dict[str, float] = {self.base_currency: 1.0}
        self._positions: dict[str, _Position] = {}
        self._fills: list[FillRecord] = []
        # e.g. persist each fill
        self.on_fill: Callable[[FillRecord], None] | None = None
        self._realized_base = 0.0
        self._fees_base = 0.0
        self._slippage_base = 0.0
        self._initial_equity = float(initial_cash)
        self._peak_equity = float(initial_cash)
        self._last_equity = float(initial_cash)
        self._day_key: object = None
        self._month_key: object = None
        self._day_baseline = float(initial_cash)
        self._month_baseline = float(initial_cash)

    # ---- configuration ----
    def set_fx_rate(self, currency: str, rate_to_base: float) -> None:
        """Base-currency value of one unit of `currency`."""
        _check_positive(rate_to_base, "rate_to_base")
        if currency.upper() == self.base_currency and rate_to_base != 1.0:
            raise PortfolioError("base currency rate must be 1")
        self._fx[currency.upper()] = float(rate_to_base)

    def deposit(self, currency: str, amount: float) -> None:
        _check_positive(amount, "amount")
        value = amount * self._rate(currency)
        self._cash[currency.upper()] = self._cash.get(
            currency.upper(), 0.0) + amount
        self._initial_equity += value
        self._day_baseline += value
        self._month_baseline += value
        self._peak_equity += value

    def convert_cash(self, from_currency: str, to_currency: str, amount: float) -> None:
        """Convert cash at the configured FX rates (no spread modeled)."""
        _check_positive(amount, "amount")
        src, dst = from_currency.upper(), to_currency.upper()
        if self._cash.get(src, 0.0) < amount and not self._allow_negative_cash:
            raise PortfolioError(f"INSUFFICIENT_CASH in {src}")
        self._cash[src] = self._cash.get(src, 0.0) - amount
        self._cash[dst] = self._cash.get(
            dst, 0.0) + amount * self._rate(src) / self._rate(dst)

    # ---- mutations ----
    def apply_fill(
        self,
        instrument: Instrument,
        side: OrderSide,
        quantity: int,
        price: float,
        timestamp: datetime,
        *,
        fee: float = 0.0,
        slippage: float = 0.0,
        sector: str | None = None,
        client_order_id: str | None = None,
        fill_sequence: int | None = None,
    ) -> FillRecord:
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise PortfolioError("quantity must be a positive integer")
        _check_positive(price, "price")
        _check_non_negative(fee, "fee")
        _check_non_negative(slippage, "slippage")
        _check_aware(timestamp, "timestamp")
        if client_order_id is not None and (
            not isinstance(client_order_id, str) or not client_order_id.strip()
        ):
            raise PortfolioError("client_order_id must be a non-empty string or None")
        if fill_sequence is not None and (
            isinstance(fill_sequence, bool)
            or not isinstance(fill_sequence, int)
            or fill_sequence <= 0
        ):
            raise PortfolioError("fill_sequence must be a positive integer or None")
        if (client_order_id is None) != (fill_sequence is None):
            raise PortfolioError(
                "client_order_id and fill_sequence must be supplied together")
        ccy = instrument.currency.upper()
        rate = self._rate(ccy)

        signed = quantity if side is OrderSide.BUY else -quantity
        cash_delta = -signed * price - fee
        if (self._cash.get(ccy, 0.0) + cash_delta < 0 and cash_delta < 0
                and not self._allow_negative_cash):
            raise PortfolioError(f"INSUFFICIENT_CASH in {ccy}")

        pos = self._positions.get(instrument.instrument_id)
        realized = 0.0
        if pos is None:
            self._positions[instrument.instrument_id] = _Position(
                instrument.instrument_id, instrument.symbol, ccy, sector,
                signed, price, price, timestamp)
        elif (pos.quantity > 0) == (signed > 0):
            total = abs(pos.quantity) + quantity
            pos.average_entry = (abs(pos.quantity) *
                                 pos.average_entry + quantity * price) / total
            pos.quantity += signed
            pos.last_price = price
            pos.sector = sector or pos.sector
        else:
            closed = min(abs(pos.quantity), quantity)
            direction = 1 if pos.quantity > 0 else -1
            realized = closed * (price - pos.average_entry) * direction
            remainder = quantity - closed
            pos.quantity += signed
            pos.last_price = price
            if pos.quantity == 0:
                del self._positions[instrument.instrument_id]
            elif remainder > 0:  # flipped through zero
                pos.average_entry = price
                pos.opened_at = timestamp
                pos.sector = sector or pos.sector

        self._cash[ccy] = self._cash.get(ccy, 0.0) + cash_delta
        self._realized_base += realized * rate
        self._fees_base += fee * rate
        self._slippage_base += slippage * rate
        record = FillRecord(timestamp, instrument.instrument_id, side, quantity, price,
                            fee, slippage, ccy, realized, client_order_id,
                            fill_sequence)
        self._fills.append(record)
        if self.on_fill is not None:
            self.on_fill(record)
        self._record_equity(timestamp)
        return record

    def mark(self, instrument_id: str, price: float, timestamp: datetime) -> None:
        _check_positive(price, "price")
        _check_aware(timestamp, "timestamp")
        pos = self._positions.get(instrument_id)
        if pos is None:
            raise PortfolioError(f"no open position for {instrument_id}")
        pos.last_price = price
        self._record_equity(timestamp)

    def credit_cash(self, currency: str, amount: float, timestamp: datetime) -> None:
        """Add (or, if negative, remove) cash that is not a trade, e.g. dividends."""
        if not _finite(amount):
            raise PortfolioError("amount must be finite")
        _check_aware(timestamp, "timestamp")
        self._rate(currency)
        self._cash[currency.upper()] = self._cash.get(
            currency.upper(), 0.0) + amount
        self._record_equity(timestamp)

    def apply_split(self, instrument_id: str, ratio: float, timestamp: datetime) -> float:
        """Rescale a position for a split; returns cash paid in lieu of fractional shares."""
        _check_positive(ratio, "ratio")
        _check_aware(timestamp, "timestamp")
        pos = self._positions.get(instrument_id)
        if pos is None:
            return 0.0
        exact = pos.quantity * ratio
        whole = int(exact)  # truncates toward zero
        pos.average_entry /= ratio
        pos.last_price /= ratio
        in_lieu = (exact - whole) * pos.last_price
        pos.quantity = whole
        if whole == 0:
            del self._positions[instrument_id]
        self._cash[pos.currency] = self._cash.get(pos.currency, 0.0) + in_lieu
        self._record_equity(timestamp)
        return in_lieu

    # ---- queries ----
    def positions(self) -> dict[str, PositionView]:
        views = {}
        for key, p in self._positions.items():
            rate = self._rate(p.currency)
            pnl = p.quantity * (p.last_price - p.average_entry)
            views[key] = PositionView(
                p.instrument_id, p.symbol,
                PositionSide.LONG if p.quantity > 0 else PositionSide.SHORT,
                abs(p.quantity), p.average_entry, p.last_price, p.currency, p.sector,
                p.quantity * p.last_price, pnl, pnl * rate, p.opened_at)
        return views

    @property
    def fills(self) -> tuple[FillRecord, ...]:
        return tuple(self._fills)

    @property
    def cash(self) -> dict[str, float]:
        return dict(self._cash)

    @property
    def cash_base(self) -> float:
        return sum(v * self._rate(c) for c, v in self._cash.items() if v != 0)

    @property
    def equity(self) -> float:
        return self.cash_base + sum(
            p.quantity * p.last_price * self._rate(p.currency)
            for p in self._positions.values())

    @property
    def realized_pnl(self) -> float:
        return self._realized_base

    @property
    def unrealized_pnl(self) -> float:
        return sum(v.unrealized_pnl_base for v in self.positions().values())

    @property
    def fees(self) -> float:
        return self._fees_base

    @property
    def slippage(self) -> float:
        return self._slippage_base

    @property
    def gross_exposure(self) -> float:
        return sum(abs(p.quantity) * p.last_price * self._rate(p.currency)
                   for p in self._positions.values())

    @property
    def net_exposure(self) -> float:
        return sum(p.quantity * p.last_price * self._rate(p.currency)
                   for p in self._positions.values())

    def sector_exposure(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for p in self._positions.values():
            key = p.sector or "UNKNOWN"
            out[key] = out.get(key, 0.0) + abs(p.quantity) * \
                p.last_price * self._rate(p.currency)
        return out

    def currency_exposure(self) -> dict[str, float]:
        """Cash plus signed position value per currency, in base currency."""
        out: dict[str, float] = {}
        for c, v in self._cash.items():
            if v != 0:
                out[c] = out.get(c, 0.0) + v * self._rate(c)
        for p in self._positions.values():
            out[p.currency] = out.get(
                p.currency, 0.0) + p.quantity * p.last_price * self._rate(p.currency)
        return out

    @property
    def peak_equity(self) -> float:
        return self._peak_equity

    @property
    def drawdown(self) -> float:
        return max(0.0, (self._peak_equity - self.equity) / self._peak_equity) if self._peak_equity > 0 else 0.0

    @property
    def daily_pnl(self) -> float:
        return self.equity - self._day_baseline

    @property
    def monthly_pnl(self) -> float:
        return self.equity - self._month_baseline

    def risk_state(
        self,
        instrument_id: str | None = None,
        *,
        sector: str | None = None,
        **extra: object,
    ) -> PortfolioRiskState:
        """Build the Phase 10 risk snapshot; extra fields (liquidity, slippage...) come from the caller."""
        pos = self._positions.get(instrument_id) if instrument_id else None
        current = abs(pos.quantity) * pos.last_price * \
            self._rate(pos.currency) if pos else 0.0
        return PortfolioRiskState(
            equity=self.equity, peak_equity=self._peak_equity, daily_pnl=self.daily_pnl,
            gross_notional_exposure=self.gross_exposure, sector=sector,
            sector_exposure=self.sector_exposure(), current_position_notional=current,
            **extra)  # type: ignore[arg-type]

    # ---- internals ----
    def _rate(self, currency: str) -> float:
        try:
            return self._fx[currency.upper()]
        except KeyError:
            raise PortfolioError(
                f"missing FX rate for {currency.upper()}") from None

    def rate_to_base(self, currency: str) -> float:
        """Return the configured conversion rate, failing closed when unavailable."""
        return self._rate(currency)

    def _record_equity(self, timestamp: datetime) -> None:
        local = timestamp.astimezone(self._tz)
        day, month = local.date(), (local.year, local.month)
        if self._day_key is not None and day != self._day_key:
            self._day_baseline = self._last_equity
        if self._month_key is not None and month != self._month_key:
            self._month_baseline = self._last_equity
        self._day_key, self._month_key = day, month
        equity = self.equity
        self._last_equity = equity
        self._peak_equity = max(self._peak_equity, equity)
