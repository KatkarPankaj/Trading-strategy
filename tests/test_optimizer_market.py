from stockmarket.config import TradingConfig
from stockmarket.optimizer import _benchmark_symbol_for_cfg


def test_benchmark_symbol_us_timezone():
    cfg = TradingConfig(market_timezone="America/New_York")
    assert _benchmark_symbol_for_cfg(cfg) == "SPY"


def test_benchmark_symbol_nse_timezone():
    cfg = TradingConfig(market_timezone="Asia/Kolkata")
    assert _benchmark_symbol_for_cfg(cfg) == "^NSEI"
