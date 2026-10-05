"""Event-driven, causal bar backtester built on the platform's sizing, portfolio and execution models."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from math import isfinite
from typing import Any, Callable, Iterable, Mapping

import numpy as np
import pandas as pd

from ..models import Instrument, OrderSide
from ..portfolio import FillRecord, PortfolioError, PortfolioManager
from ..sizing import SizingLimits, size_position
from ..trading_calendar import TradingCalendar
from .execution_model import ActionKind, CorporateAction, ExecutionModel, PriceBasis
from .metrics import compute_metrics

REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")
LIQUIDITY_LOOKBACK_BARS = 20


@dataclass(frozen=True, slots=True)
class BacktestSignal:
    """Decision taken at a bar's close, using only data up to and including that bar."""

    side: OrderSide
    stop_price: float
    take_profit: float | None = None
    reason: str = ""


SignalFn = Callable[[str, pd.DataFrame], "BacktestSignal | None"]


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    starting_cash: float
    sizing: SizingLimits
    base_currency: str = "USD"
    execution: ExecutionModel = field(default_factory=ExecutionModel)
    max_positions: int = 5
    max_holding_bars: int | None = None
    allow_short: bool = False
    price_basis: PriceBasis = PriceBasis.RAW
    periods_per_year: int = 252
    annual_risk_free_rate: float = 0.0

    def __post_init__(self) -> None:
        if not isfinite(self.starting_cash) or self.starting_cash <= 0:
            raise ValueError("starting_cash must be positive")
        if self.max_positions < 1:
            raise ValueError("max_positions must be at least 1")
        if self.max_holding_bars is not None and self.max_holding_bars < 1:
            raise ValueError("max_holding_bars must be at least 1")


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    instrument_id: str
    side: OrderSide
    quantity: int
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    gross_pnl: float
    fees: float
    dividends: float
    net_pnl: float  # local currency
    net_pnl_base: float
    exit_reason: str
    signal_reason: str


@dataclass(frozen=True, slots=True)
class BacktestResult:
    trades: tuple[BacktestTrade, ...]
    equity_curve: pd.Series
    metrics: dict[str, Any]
    skipped: dict[str, int]
    fills: tuple[FillRecord, ...]


@dataclass(slots=True)
class _Order:
    intent: str  # ENTRY or EXIT
    side: OrderSide
    remaining: int
    activate_i: int
    stop: float = 0.0
    target: float | None = None
    reason: str = ""
    waited: int = 0


@dataclass(slots=True)
class _Pos:
    side: OrderSide  # BUY = long
    qty: int
    avg_entry: float
    stop: float
    target: float | None
    opened_at: datetime
    signal_reason: str
    bars_held: int = 0
    entry_fees: float = 0.0
    exit_fees: float = 0.0
    exit_qty: int = 0
    exit_value: float = 0.0
    dividends: float = 0.0
    exit_reason: str = ""


@dataclass(slots=True)
class _Symbol:
    iid: str
    inst: Instrument
    df: pd.DataFrame
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    local_dates: list[date]
    eligible: dict[pd.Timestamp, int]
    last: int
    rate: float
    actions: list[CorporateAction]
    pos: _Pos | None = None
    pending: _Order | None = None


def _validate_bars(df: pd.DataFrame, iid: str) -> None:
    if not isinstance(df, pd.DataFrame) or df.empty:
        raise ValueError(f"{iid}: bars must be a non-empty DataFrame")
    if not isinstance(df.index, pd.DatetimeIndex) or df.index.tz is None:
        raise ValueError(
            f"{iid}: index must be a timezone-aware DatetimeIndex")
    if not df.index.is_monotonic_increasing or df.index.has_duplicates:
        raise ValueError(f"{iid}: timestamps must be sorted and unique")
    missing = set(REQUIRED_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"{iid}: missing columns {sorted(missing)}")
    values = df[list(REQUIRED_COLUMNS)].to_numpy(dtype=float)
    if not np.isfinite(values).all() or (df[["open", "high", "low", "close"]] <= 0).any().any() \
            or (df["volume"] < 0).any():
        raise ValueError(
            f"{iid}: bars must be finite with positive prices and non-negative volume")
    if (df["high"] < df[["open", "low", "close"]].max(axis=1)).any() \
            or (df["low"] > df[["open", "high", "close"]].min(axis=1)).any():
        raise ValueError(f"{iid}: inconsistent OHLC values")


class BacktestEngine:
    def __init__(self, config: BacktestConfig) -> None:
        self.config = config

    def run(
        self,
        instruments: Mapping[str, Instrument],
        bars: Mapping[str, pd.DataFrame],
        signal_fn: SignalFn,
        *,
        calendars: Mapping[str, TradingCalendar] | None = None,
        corporate_actions: Iterable[CorporateAction] = (),
        fx_rates: Mapping[str, float] | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> BacktestResult:
        """Bars before `start` are history only; positions are closed on each symbol's last bar (end, delisting or data end)."""
        sim = _Simulation(self.config, instruments, bars, signal_fn, calendars or {},
                          list(corporate_actions), fx_rates or {}, start, end)
        return sim.run()


class _Simulation:
    def __init__(self, cfg, instruments, bars, signal_fn, calendars, actions, fx_rates, start, end):
        self.cfg, self.ex, self.signal_fn = cfg, cfg.execution, signal_fn
        self.fx = {cfg.base_currency.upper(): 1.0, **{k.upper(): float(v)
                                                      for k, v in fx_rates.items()}}
        self.pf = PortfolioManager(cfg.base_currency, cfg.starting_cash)
        for ccy, rate in self.fx.items():
            self.pf.set_fx_rate(ccy, rate)
        self.skipped: Counter[str] = Counter()
        self.trades: list[BacktestTrade] = []
        self.traded_notional = 0.0
        self.symbols: dict[str, _Symbol] = {}
        for iid, df in bars.items():
            inst = instruments.get(iid)
            if inst is None:
                raise ValueError(f"no instrument definition for {iid}")
            if inst.currency.upper() not in self.fx:
                raise ValueError(f"no FX rate for {inst.currency} ({iid})")
            _validate_bars(df, iid)
            cal = calendars.get(iid)
            if cal is not None:
                df = df.loc[[cal.is_open(ts) for ts in df.index]]
            if df.empty:
                continue
            local = [ts.date() for ts in df.index.tz_convert(inst.timezone)]
            keep = [k for k, d in enumerate(local) if end is None or d <= end]
            if not keep:
                continue
            df, local = df.iloc[: keep[-1] + 1], local[: keep[-1] + 1]
            first = next((k for k, d in enumerate(local)
                         if start is None or d >= start), None)
            if first is None:
                continue
            self.symbols[iid] = _Symbol(
                iid, inst, df, *(df[c].to_numpy(dtype=float)
                                 for c in REQUIRED_COLUMNS), local,
                {df.index[k]: k for k in range(first, len(df))}, len(df) - 1,
                self.fx[inst.currency.upper()],
                sorted((a for a in actions if a.instrument_id == iid), key=lambda a: a.ex_date))
        if not self.symbols:
            raise ValueError(
                "no bars fall inside the requested window and trading hours")

    def run(self) -> BacktestResult:
        timeline = sorted({ts for s in self.symbols.values()
                          for ts in s.eligible})
        rows: list[tuple[pd.Timestamp, float, bool]] = []
        for t in timeline:
            for s in self.symbols.values():
                i = s.eligible.get(t)
                if i is not None:
                    self._bar(s, i, t)
            rows.append((t, self.pf.equity, len(self.pf.positions()) > 0))
        equity = pd.Series([r[1] for r in rows],
                           index=pd.DatetimeIndex([r[0] for r in rows]))
        exposure = pd.Series([r[2] for r in rows],
                             index=equity.index, dtype=float)
        pnls = [tr.net_pnl_base for tr in self.trades]
        if (equity <= 0).any():
            metrics: dict[str, Any] = {"ruined": True, "trades": len(pnls)}
        else:
            metrics = compute_metrics(
                pnls, equity, exposure, self.traded_notional,
                periods_per_year=self.cfg.periods_per_year,
                annual_risk_free_rate=self.cfg.annual_risk_free_rate)
        return BacktestResult(tuple(self.trades), equity, metrics, dict(self.skipped), self.pf.fills)

    # ---- per-bar pipeline ----
    def _bar(self, s: _Symbol, i: int, t: pd.Timestamp) -> None:
        self._apply_actions(s, i, t)
        self._execute_pending(s, i, t)
        self._check_stops(s, i, t)
        if s.pos is not None:
            s.pos.bars_held += 1
            if (self.cfg.max_holding_bars and s.pos.bars_held >= self.cfg.max_holding_bars
                    and s.pending is None and i < s.last):
                s.pending = _Order("EXIT", _opposite(s.pos.side), s.pos.qty,
                                   i + self.ex.latency_bars, reason="TIME")
        if i == s.last:
            if s.pos is not None:
                self._close(s, s.pos.qty, s.c[i], t, "END_OF_DATA")
            s.pending = None
        if s.pos is not None:
            self.pf.mark(s.iid, float(s.c[i]), t)
        elif i < s.last:
            self._maybe_signal(s, i, t)

    def _apply_actions(self, s: _Symbol, i: int, t: pd.Timestamp) -> None:
        while s.actions and s.actions[0].ex_date <= s.local_dates[i]:
            action = s.actions.pop(0)
            if self.cfg.price_basis is not PriceBasis.RAW or s.pos is None:
                continue
            p = s.pos
            if action.kind is ActionKind.SPLIT:
                self.pf.apply_split(s.iid, action.value, t)
                held = self.pf.positions().get(s.iid)
                p.qty = held.quantity if held else 0
                p.avg_entry /= action.value
                p.stop /= action.value
                p.target = p.target / action.value if p.target else None
                if p.qty == 0:
                    s.pos = s.pending = None
            else:
                sign = 1 if p.side is OrderSide.BUY else -1
                cash = sign * p.qty * action.value
                self.pf.credit_cash(s.inst.currency, cash, t)
                p.dividends += cash

    def _execute_pending(self, s: _Symbol, i: int, t: pd.Timestamp) -> None:
        od = s.pending
        if od is None or i < od.activate_i:
            return
        ex = self.ex
        if od.intent == "ENTRY":
            est = ex.fill_price(s.o[i], od.side)
            if (od.side is OrderSide.BUY and est <= od.stop) or (od.side is OrderSide.SELL and est >= od.stop):
                self.skipped["GAP_THROUGH_STOP"] += 1
                s.pending = None
                return
            qty = ex.fillable_quantity(od.remaining, s.v[i])
            if qty > 0:
                try:
                    price, fee = self._fill(s, od.side, qty, s.o[i], t)
                except PortfolioError:
                    self.skipped["INSUFFICIENT_CASH"] += 1
                    s.pending = None
                    return
                p = s.pos
                if p is None:
                    s.pos = _Pos(od.side, qty, price, od.stop, od.target, t.to_pydatetime(),
                                 od.reason, entry_fees=fee)
                else:
                    p.avg_entry = (p.avg_entry * p.qty +
                                   price * qty) / (p.qty + qty)
                    p.qty += qty
                    p.entry_fees += fee
                od.remaining -= qty
        else:
            if s.pos is None:
                s.pending = None
                return
            qty = ex.fillable_quantity(min(od.remaining, s.pos.qty), s.v[i])
            if qty > 0:
                self._close(s, qty, s.o[i], t, od.reason)
                od.remaining -= qty
        od.waited += 1
        if od.remaining <= 0 or s.pos is None and od.intent == "EXIT":
            s.pending = None
        elif od.waited >= ex.max_order_bars:
            self.skipped[f"{od.intent}_REMAINDER_CANCELLED"] += 1
            s.pending = None

    def _check_stops(self, s: _Symbol, i: int, t: pd.Timestamp) -> None:
        p = s.pos
        if p is None:
            return
        o, h, l = s.o[i], s.h[i], s.l[i]
        raw, reason = None, ""
        if p.side is OrderSide.BUY:
            if o <= p.stop:
                raw, reason = o, "STOP"
            elif p.target and o >= p.target:
                raw, reason = o, "TARGET"
            elif l <= p.stop:
                raw, reason = p.stop, "STOP"
            elif p.target and h >= p.target:
                raw, reason = p.target, "TARGET"
        else:
            if o >= p.stop:
                raw, reason = o, "STOP"
            elif p.target and o <= p.target:
                raw, reason = o, "TARGET"
            elif h >= p.stop:
                raw, reason = p.stop, "STOP"
            elif p.target and l <= p.target:
                raw, reason = p.target, "TARGET"
        if raw is not None:
            self._close(s, p.qty, raw, t, reason)
            s.pending = None

    def _maybe_signal(self, s: _Symbol, i: int, t: pd.Timestamp) -> None:
        if s.pending is not None:
            return
        active = sum(1 for x in self.symbols.values() if x.pos or (
            x.pending and x.pending.intent == "ENTRY"))
        if active >= self.cfg.max_positions:
            self.skipped["MAX_POSITIONS"] += 1
            return
        sig = self.signal_fn(s.iid, s.df.iloc[: i + 1])
        if sig is None:
            return
        close = float(s.c[i])
        buy = sig.side is OrderSide.BUY
        if (buy and not sig.stop_price < close) or (not buy and not sig.stop_price > close):
            self.skipped["INVALID_STOP"] += 1
            return
        if not buy and not self.cfg.allow_short:
            self.skipped["SHORT_NOT_ALLOWED"] += 1
            return
        ccy = s.inst.currency.upper()
        sized = size_position(
            s.inst, sig.side, entry_price=close, stop_price=sig.stop_price,
            equity=self.pf.equity / s.rate,
            available_cash=max(0.0, self.pf.cash.get(ccy, 0.0)),
            limits=self.cfg.sizing, gross_exposure=self.pf.gross_exposure / s.rate,
            average_daily_volume=float(s.v[max(0, i + 1 - LIQUIDITY_LOOKBACK_BARS): i + 1].mean()))
        if not sized.approved:
            self.skipped[f"SIZING_{sized.reason}"] += 1
            return
        s.pending = _Order("ENTRY", sig.side, sized.quantity, i + self.ex.latency_bars,
                           stop=sig.stop_price, target=sig.take_profit, reason=sig.reason)

    # ---- fills ----
    def _fill(self, s: _Symbol, side: OrderSide, qty: int, raw_price: float, t: pd.Timestamp) -> tuple[float, float]:
        price = self.ex.fill_price(float(raw_price), side)
        fee = self.ex.commission(price * qty)
        self.pf.apply_fill(s.inst, side, qty, price, t, fee=fee,
                           slippage=abs(price - float(raw_price)) * qty)
        self.traded_notional += price * qty * s.rate
        return price, fee

    def _close(self, s: _Symbol, qty: int, raw_price: float, t: pd.Timestamp, reason: str) -> None:
        p = s.pos
        assert p is not None
        price, fee = self._fill(s, _opposite(p.side), qty, raw_price, t)
        p.exit_fees += fee
        p.exit_qty += qty
        p.exit_value += price * qty
        p.qty -= qty
        p.exit_reason = p.exit_reason or reason
        if p.qty > 0:
            return
        sign = 1 if p.side is OrderSide.BUY else -1
        gross = sign * (p.exit_value - p.avg_entry * p.exit_qty)
        fees = p.entry_fees + p.exit_fees
        net = gross + p.dividends - fees
        self.trades.append(BacktestTrade(
            s.iid, p.side, p.exit_qty, p.opened_at, t.to_pydatetime(), p.avg_entry,
            p.exit_value / p.exit_qty, gross, fees, p.dividends, net, net * s.rate,
            p.exit_reason, p.signal_reason))
        s.pos = None


def _opposite(side: OrderSide) -> OrderSide:
    return OrderSide.SELL if side is OrderSide.BUY else OrderSide.BUY
