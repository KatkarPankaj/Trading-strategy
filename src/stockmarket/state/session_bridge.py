"""Bridge between Streamlit session state and paper-trading domain objects."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Mapping, cast

from stockmarket.domain import DailyCounters, PaperState, Position, Side, TradeLogEntry

_DEFAULT_SL_PCT = 0.008
_DEFAULT_TP_PCT = 0.016


def session_state_to_paper_state(session) -> PaperState:
    ui_config = dict(_get(session, "s_ui_config", {}) or {})
    sl_pct = _ui_pct(ui_config, "sl_pct", "sl_pct_display", _DEFAULT_SL_PCT)
    tp_pct = _ui_pct(ui_config, "tp_pct", "tp_pct_display", _DEFAULT_TP_PCT)

    return PaperState(
        start_capital=float(_get(session, "s_start", 0.0) or 0.0),
        cash=float(_get(session, "s_cash", 0.0) or 0.0),
        realized=float(_get(session, "s_realized", 0.0) or 0.0),
        charges=float(_get(session, "s_charges", 0.0) or 0.0),
        holdings=_positions_from_session(
            _get(session, "s_holdings", {}) or {},
            sl_pct,
            tp_pct,
            short=False,
        ),
        shorts=_positions_from_session(
            _get(session, "s_shorts", {}) or {},
            sl_pct,
            tp_pct,
            short=True,
        ),
        prices={str(k): float(v) for k, v in dict(_get(session, "s_prices", {}) or {}).items()},
        log=[_trade_log_entry(row) for row in _get(session, "s_log", []) or []],
        ui_config=ui_config,
        agent_memory=dict(_get(session, "s_agent_memory", {}) or {}),
        market=str(
            _get(session, "selected_market", _get(session, "market", "NSE")) or "NSE"
        ),
    )


def paper_state_to_session_state(session, state: PaperState) -> None:
    _set(session, "s_cash", float(state.cash))
    _set(session, "s_start", float(state.start_capital))
    _set(session, "s_realized", float(state.realized))
    _set(session, "s_charges", float(state.charges))
    _set(session, "s_holdings", _positions_to_session(state.holdings))
    _set(session, "s_shorts", _positions_to_session(state.shorts))
    _set(session, "s_ui_config", dict(state.ui_config))
    _set(session, "s_log", [_trade_to_session(row) for row in state.log])
    _set(session, "s_prices", dict(state.prices))
    _set(session, "s_agent_memory", dict(state.agent_memory))
    _set(session, "selected_market", state.market)


def session_state_to_counters(session) -> DailyCounters:
    day = str(_get(session, "s_peak_open_pnl_day", "") or "")
    return DailyCounters(
        day=day,
        peak_open_pnl=float(_get(session, "s_peak_open_pnl", 0.0) or 0.0),
        peak_open_pnl_day=day,
        profit_guard_triggered_day=str(_get(session, "s_profit_guard_triggered_day", "") or ""),
        profit_ladder_day=str(_get(session, "s_profit_ladder_day", "") or ""),
        profit_ladder_armed=bool(_get(session, "s_profit_ladder_armed", False)),
        profit_ladder_pullback_started=bool(_get(session, "s_profit_ladder_pullback_started", False)),
        profit_ladder_exited_day=str(_get(session, "s_profit_ladder_exited_day", "") or ""),
    )


def counters_to_session_state(session, counters: DailyCounters) -> None:
    _set(session, "s_peak_open_pnl", float(counters.peak_open_pnl))
    _set(session, "s_peak_open_pnl_day", counters.peak_open_pnl_day or counters.day)
    _set(session, "s_profit_guard_triggered_day", counters.profit_guard_triggered_day)
    _set(session, "s_profit_ladder_day", counters.profit_ladder_day)
    _set(session, "s_profit_ladder_armed", bool(counters.profit_ladder_armed))
    _set(session, "s_profit_ladder_pullback_started", bool(counters.profit_ladder_pullback_started))
    _set(session, "s_profit_ladder_exited_day", counters.profit_ladder_exited_day)


def _get(session, key: str, default: Any = None) -> Any:
    if hasattr(session, "get"):
        return session.get(key, default)
    try:
        return session[key]
    except (KeyError, TypeError):
        return getattr(session, key, default)


def _set(session, key: str, value: Any) -> None:
    try:
        session[key] = value
    except TypeError:
        setattr(session, key, value)


def _ui_pct(ui_config: Mapping[str, Any], decimal_key: str, display_key: str, default: float) -> float:
    if decimal_key in ui_config:
        return float(ui_config[decimal_key])
    if display_key in ui_config:
        return float(ui_config[display_key]) / 100.0
    return default


def _positions_from_session(
    rows: Mapping[str, Any],
    sl_pct: float,
    tp_pct: float,
    *,
    short: bool,
) -> dict[str, Position]:
    return {
        str(symbol): _position_from_session(value, sl_pct, tp_pct, short=short)
        for symbol, value in dict(rows).items()
    }


def _position_from_session(value: Any, sl_pct: float, tp_pct: float, *, short: bool) -> Position:
    if isinstance(value, Position):
        return value

    data = dict(value or {})
    avg = float(data.get("avg", 0.0) or 0.0)
    if short:
        default_stop = avg * (1.0 + sl_pct)
        default_target = avg * (1.0 - tp_pct)
    else:
        default_stop = avg * (1.0 - sl_pct)
        default_target = avg * (1.0 + tp_pct)

    return Position(
        qty=int(data.get("qty", 0) or 0),
        avg=avg,
        stop=float(data.get("stop", default_stop) or 0.0),
        target=float(data.get("target", default_target) or 0.0),
    )


def _positions_to_session(rows: Mapping[str, Position]) -> dict[str, dict[str, float | int]]:
    return {str(symbol): asdict(position) for symbol, position in rows.items()}


def _trade_log_entry(row: Any) -> TradeLogEntry:
    if isinstance(row, TradeLogEntry):
        return row

    data = dict(row or {})
    return TradeLogEntry(
        ts=str(data.get("ts", "")),
        symbol=str(data.get("symbol", "")),
        side=cast(Side, str(data.get("side", "BUY")).upper()),
        qty=int(data.get("qty", 0) or 0),
        price=float(data.get("price", 0.0) or 0.0),
        charges=float(data.get("charges", 0.0) or 0.0),
        realized_delta=float(data.get("realized_delta", 0.0) or 0.0),
        reason=str(data.get("reason", "")),
        cash_after=float(data.get("cash_after", 0.0) or 0.0),
    )


def _trade_to_session(row: TradeLogEntry | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(row, TradeLogEntry):
        return asdict(row)
    return dict(row)

