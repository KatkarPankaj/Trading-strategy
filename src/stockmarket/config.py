from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TradingConfig:
    symbol: str = "RELIANCE.NS"
    interval: str = "5m"
    period: str = "30d"
    market_timezone: str = "Asia/Kolkata"

    opening_range_minutes: int = 15
    entry_cutoff_time: str = "13:30"
    square_off_time: str = "15:15"
    time_exit_minutes: int = 60

    stop_loss_pct: float = 0.004
    take_profit_pct: float = 0.008
    risk_per_trade_pct: float = 0.005
    starting_capital: float = 200000.0
    max_trades_per_day: int = 1

    commission_pct: float = 0.0003
    slippage_pct: float = 0.0005

    allow_short: bool = False
    volume_ma_window: int = 20
    volume_spike_threshold: float = 1.2

    @classmethod
    def from_json(cls, file_path: str | Path) -> "TradingConfig":
        path = Path(file_path)
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(**data)
