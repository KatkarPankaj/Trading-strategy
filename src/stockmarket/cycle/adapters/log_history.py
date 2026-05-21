"""History queries from paper trade log."""

from __future__ import annotations

from datetime import datetime, time

from stockmarket.domain.types import PaperState


class LogHistoryQuery:
    def __init__(self, market_open_time: time):
        self._market_open = market_open_time

    def latest_stop_loss_by_symbol(
        self, state: PaperState
    ) -> dict[str, tuple[datetime, str]]:
        latest: dict[str, tuple[datetime, str]] = {}
        for row in reversed(state.log):
            side = str(row.side).upper()
            if side not in {"SELL", "COVER"}:
                continue
            reason = str(row.reason).strip()
            if "sl" not in reason.lower():
                continue
            sym = str(row.symbol).strip()
            if not sym or sym in latest:
                continue
            try:
                ts_dt = datetime.strptime(str(row.ts), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            latest[sym] = (ts_dt, reason)
        return latest

    def latest_exit_by_symbol(
        self, state: PaperState, day: str
    ) -> dict[str, tuple[datetime, float]]:
        latest: dict[str, tuple[datetime, float]] = {}
        for row in reversed(state.log):
            ts = str(row.ts)
            if not ts.startswith(day):
                continue
            side = str(row.side).upper()
            if side not in {"SELL", "COVER"}:
                continue
            sym = str(row.symbol).strip()
            if not sym or sym in latest:
                continue
            try:
                ts_dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            latest[sym] = (ts_dt, float(row.price))
        return latest

    def minutes_since_last_entry(
        self, state: PaperState, now: datetime, day: str
    ) -> float:
        now_naive = now.replace(tzinfo=None)
        for row in reversed(state.log):
            ts = str(row.ts)
            if not ts.startswith(day):
                continue
            side = str(row.side).upper()
            if side not in {"BUY", "SHORT"}:
                continue
            try:
                ts_dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            return max(0.0, (now_naive - ts_dt).total_seconds() / 60.0)

        market_open_dt = datetime.combine(now_naive.date(), self._market_open)
        return max(0.0, (now_naive - market_open_dt).total_seconds() / 60.0)

    def today_entry_count(self, state: PaperState, day: str) -> int:
        return sum(
            1
            for row in state.log
            if str(row.ts).startswith(day)
            and str(row.side).upper() in {"BUY", "SHORT"}
        )

    def current_open_pnl(self, state: PaperState) -> float:
        pnl = 0.0
        for sym, pos in state.holdings.items():
            qty, avg = int(pos.qty), float(pos.avg)
            ltp = float(state.prices.get(sym, avg))
            if qty > 0 and avg > 0 and ltp > 0:
                pnl += (ltp - avg) * qty
        for sym, pos in state.shorts.items():
            qty, avg = int(pos.qty), float(pos.avg)
            ltp = float(state.prices.get(sym, avg))
            if qty > 0 and avg > 0 and ltp > 0:
                pnl += (avg - ltp) * qty
        return float(pnl)
