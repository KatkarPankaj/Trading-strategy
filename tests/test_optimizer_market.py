from stockmarket.config import TradingConfig
from stockmarket.optimization import benchmark_symbol_for_cfg


def test_benchmark_symbol_us_timezone():
    cfg = TradingConfig(market_timezone="America/New_York")
    assert benchmark_symbol_for_cfg(cfg) == "SPY"


def test_benchmark_symbol_nse_timezone():
    cfg = TradingConfig(market_timezone="Asia/Kolkata")
    assert benchmark_symbol_for_cfg(cfg) == "^NSEI"
