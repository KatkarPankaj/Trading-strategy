"""JSON-backed repository for the simple dashboard paper state."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

from stockmarket.domain import DailyCounters, PaperState, Position, TradeLogEntry
from stockmarket.state import session_state_to_counters, session_state_to_paper_state


class JsonPaperRepo:
    def __init__(self, path: Path, market: str | None = None):
        self._path = Path(path)
        self._market = (market or "NSE").upper()

    def load(self) -> tuple[PaperState, DailyCounters] | None:
        if not self._path.exists():
            return None

        data = json.loads(self._path.read_text(encoding="utf-8"))
        session = _session_from_payload(data, self._market)
        return session_state_to_paper_state(session), session_state_to_counters(session)

    def save(self, state: PaperState, counters: DailyCounters) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = paper_state_to_json_payload(state, counters, default_market=self._market)
        self._path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def path(self) -> Path:
        return self._path


def paper_state_to_json_payload(
    state: PaperState,
    counters: DailyCounters,
    *,
    default_market: str = "NSE",
) -> dict[str, Any]:
    return {
        "cash": float(state.cash),
        "start": float(state.start_capital),
        "realized": float(state.realized),
        "charges": float(state.charges),
        "holdings": _positions_to_payload(state.holdings),
        "shorts": _positions_to_payload(state.shorts),
        "ui_config": dict(state.ui_config),
        "log": [_trade_to_payload(row) for row in state.log],
        "prices": dict(state.prices),
        "agent_memory": dict(state.agent_memory),
        "peak_open_pnl": float(counters.peak_open_pnl),
        "peak_open_pnl_day": counters.peak_open_pnl_day or counters.day,
        "profit_guard_triggered_day": counters.profit_guard_triggered_day,
        "profit_ladder_day": counters.profit_ladder_day,
        "profit_ladder_armed": bool(counters.profit_ladder_armed),
        "profit_ladder_pullback_started": bool(counters.profit_ladder_pullback_started),
        "profit_ladder_exited_day": counters.profit_ladder_exited_day,
        "market": state.market or default_market,
    }


def _session_from_payload(data: Mapping[str, Any], default_market: str) -> dict[str, Any]:
    return {
        "s_cash": data.get("cash", 0.0),
        "s_start": data.get("start", 0.0),
        "s_realized": data.get("realized", 0.0),
        "s_charges": data.get("charges", 0.0),
        "s_holdings": data.get("holdings", {}),
        "s_shorts": data.get("shorts", {}),
        "s_ui_config": data.get("ui_config", {}),
        "s_log": data.get("log", []),
        "s_prices": data.get("prices", {}),
        "s_agent_memory": data.get("agent_memory", {}),
        "s_peak_open_pnl": data.get("peak_open_pnl", 0.0),
        "s_peak_open_pnl_day": data.get("peak_open_pnl_day", ""),
        "s_profit_guard_triggered_day": data.get("profit_guard_triggered_day", ""),
        "s_profit_ladder_day": data.get("profit_ladder_day", ""),
        "s_profit_ladder_armed": data.get("profit_ladder_armed", False),
        "s_profit_ladder_pullback_started": data.get("profit_ladder_pullback_started", False),
        "s_profit_ladder_exited_day": data.get("profit_ladder_exited_day", ""),
        "selected_market": data.get("market", default_market),
    }


def _positions_to_payload(rows: Mapping[str, Position]) -> dict[str, dict[str, Any]]:
    return {str(symbol): asdict(position) for symbol, position in rows.items()}


def _trade_to_payload(row: TradeLogEntry | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(row, TradeLogEntry):
        return asdict(row)
    return dict(row)
