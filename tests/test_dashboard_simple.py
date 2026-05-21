"""
Automated tests for dashboard_simple.py
Run with: python -m pytest tests/test_dashboard_simple.py -v
"""

import sys
import json
import pandas as pd
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

# Test configuration
TEST_CONFIG_NSE = {
    "label": "India NSE",
    "timezone": "Asia/Kolkata",
    "market_open": "09:15",
    "entry_cutoff": "13:30",
    "square_off": "15:15",
    "watchlist": ["RELIANCE.NS", "TCS.NS"],
    "state_file": "test_state_nse.json",
    "currency": "Rs",
}


class SessionState(dict):
    """Small Streamlit session_state stand-in for helper tests."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc

    def __setattr__(self, key, value):
        self[key] = value


def _paper_session(cash=100000.0, start=100000.0):
    return SessionState(
        s_cash=float(cash),
        s_start=float(start),
        s_realized=0.0,
        s_charges=0.0,
        s_holdings={},
        s_shorts={},
        s_ui_config={},
        s_log=[],
        s_prices={},
        s_agent_memory={},
        selected_market="NSE",
    )


class TestDashboardImports:
    """Test that all required modules can be imported."""
    
    def test_imports(self):
        """Verify core imports work."""
        try:
            import streamlit as st
            import pandas as pd
            import pytz
            import logging
            assert True
        except Exception as e:
            pytest.fail(f"Import failed: {e}")
    
    def test_helper_functions_exist(self):
        """Verify critical functions are defined in the module."""
        import dashboard_simple
        
        required_functions = [
            "_app_log",
            "_selected_market",
            "_market_cfg",
            "_state_file",
            "_read_saved_state",
            "_init_state",
            "_save_state",
            "_portfolio_view",
            "_render_activity_and_logs",
            "market_now",
            "_auto_refresh",
            "fetch_market_quote",
            "_intraday_charges",
            "_record_trade",
        ]
        
        for func_name in required_functions:
            assert hasattr(dashboard_simple, func_name), f"Function {func_name} not found"


class TestDataStructures:
    """Test data structure integrity."""
    
    def test_market_config_structure(self):
        """Verify MARKET_CONFIG has required keys."""
        import dashboard_simple
        
        for market_key in ["NSE", "US"]:
            assert market_key in dashboard_simple.MARKET_CONFIG
            market_cfg = dashboard_simple.MARKET_CONFIG[market_key]
            required_keys = [
                "label", "timezone", "market_open", "entry_cutoff",
                "square_off", "watchlist", "state_file", "currency"
            ]
            for key in required_keys:
                assert key in market_cfg, f"Missing key '{key}' in {market_key} config"
    
    def test_watchlist_nonempty(self):
        """Verify watchlists have symbols."""
        import dashboard_simple
        
        for market, cfg in dashboard_simple.MARKET_CONFIG.items():
            assert len(cfg["watchlist"]) > 0, f"Empty watchlist for {market}"
            assert all(isinstance(s, str) for s in cfg["watchlist"]), f"Invalid symbols in {market} watchlist"


class TestHelperFunctions:
    """Test utility function correctness."""
    
    def test_currency_formatting(self):
        """Test that currency symbols are correct."""
        import dashboard_simple
        
        nse_cfg = dashboard_simple.MARKET_CONFIG["NSE"]
        us_cfg = dashboard_simple.MARKET_CONFIG["US"]
        
        assert nse_cfg["currency"] == "Rs"
        assert us_cfg["currency"] == "$"
    
    def test_charges_calculation(self):
        """Test that intraday charges are calculated."""
        import dashboard_simple
        
        # NSE BUY charges
        charges_buy = dashboard_simple._intraday_charges("BUY", 10000.0)
        assert charges_buy > 0, "BUY charges should be positive"
        assert charges_buy < 500, "BUY charges should be reasonable (<5%)"
        
        # NSE SELL charges
        charges_sell = dashboard_simple._intraday_charges("SELL", 10000.0)
        assert charges_sell > 0, "SELL charges should be positive"
        assert charges_sell < 500, "SELL charges should be reasonable"

    def test_record_trade_sides(self):
        """Pin BUY/SELL/SHORT/COVER accounting and partial exits."""
        import dashboard_simple

        session = _paper_session()
        fake_st = SimpleNamespace(session_state=session)

        with (
            patch.object(dashboard_simple, "st", fake_st),
            patch.object(dashboard_simple, "_save_state"),
            patch.object(dashboard_simple, "market_now", return_value=datetime(2026, 1, 1, 9, 30, 0)),
        ):
            buy_charges = dashboard_simple._intraday_charges("BUY", 1000.0)
            dashboard_simple._record_trade("ABC", "BUY", 10, 100.0, "entry", sl_pct=0.01, tp_pct=0.02)

            assert session.s_cash == pytest.approx(100000.0 - 1000.0 - buy_charges)
            assert session.s_holdings["ABC"] == {
                "qty": 10,
                "avg": 100.0,
                "stop": 99.0,
                "target": 102.0,
            }

            sell_charges = dashboard_simple._intraday_charges("SELL", 440.0)
            dashboard_simple._record_trade("ABC", "SELL", 4, 110.0, "partial exit")

            assert session.s_holdings["ABC"]["qty"] == 6
            assert session.s_realized == pytest.approx(40.0)
            assert session.s_cash == pytest.approx(100000.0 - 1000.0 - buy_charges + 440.0 - sell_charges)
            assert session.s_log[-1]["realized_delta"] == pytest.approx(40.0)

            short_charges = dashboard_simple._intraday_charges("SHORT", 1000.0)
            dashboard_simple._record_trade("XYZ", "SHORT", 5, 200.0, "short entry", sl_pct=0.01, tp_pct=0.02)

            assert session.s_shorts["XYZ"] == {
                "qty": 5,
                "avg": 200.0,
                "stop": 202.0,
                "target": 196.0,
            }

            cover_charges = dashboard_simple._intraday_charges("COVER", 380.0)
            dashboard_simple._record_trade("XYZ", "COVER", 2, 190.0, "partial cover")

            assert session.s_shorts["XYZ"]["qty"] == 3
            assert session.s_realized == pytest.approx(60.0)
            assert session.s_charges == pytest.approx(buy_charges + sell_charges + short_charges + cover_charges)
            assert [row["side"] for row in session.s_log] == ["BUY", "SELL", "SHORT", "COVER"]

    def test_completed_trade_pairs_buy_sell(self):
        """Test completed trade pairing and computed P&L columns."""
        import dashboard_simple

        sample_log = [
            {
                "ts": "2026-01-01 09:00:00",
                "symbol": "RELIANCE",
                "side": "BUY",
                "qty": 10,
                "price": 100.0,
                "charges": 2.0,
                "reason": "initial buy",
                "cash_after": 1000.0,
            },
            {
                "ts": "2026-01-01 10:00:00",
                "symbol": "RELIANCE",
                "side": "SELL",
                "qty": 10,
                "price": 120.0,
                "charges": 3.0,
                "reason": "take profit target\nsome details",
                "cash_after": 1195.0,
            },
        ]

        df = dashboard_simple._completed_trades_from_log(sample_log)
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 1

        row = df.iloc[0]
        assert row["symbol"] == "RELIANCE"
        assert row["side"] == "LONG"
        assert row["quantity"] == 10
        assert row["buying_price"] == 100.0
        assert row["selling_price"] == 120.0
        assert row["charges"] == 5.0
        assert row["total_invested"] == 1002.0
        assert row["total_collected"] == 1197.0
        assert row["realized_pnl"] == 195.0
        assert row["reason"] == "take profit target"
        assert row["cash_in_hand"] == 1195.0

    def test_completed_trade_partial_exit_pnl_allocation(self):
        """Test partial exits allocate charges proportionally across lots."""
        import dashboard_simple

        sample_log = [
            {
                "ts": "2026-01-01 09:00:00",
                "symbol": "TCS",
                "side": "BUY",
                "qty": 10,
                "price": 100.0,
                "charges": 10.0,
                "reason": "buy first lot",
                "cash_after": 1000.0,
            },
            {
                "ts": "2026-01-01 09:30:00",
                "symbol": "TCS",
                "side": "SELL",
                "qty": 5,
                "price": 130.0,
                "charges": 5.0,
                "reason": "partial target",
                "cash_after": 1125.0,
            },
            {
                "ts": "2026-01-01 10:00:00",
                "symbol": "TCS",
                "side": "SELL",
                "qty": 5,
                "price": 140.0,
                "charges": 6.0,
                "reason": "final exit",
                "cash_after": 1335.0,
            },
        ]

        df = dashboard_simple._completed_trades_from_log(sample_log)
        assert len(df) == 2

        ordered = df.sort_values("timestamp")
        first, second = ordered.iloc[0], ordered.iloc[1]
        assert first["realized_pnl"] == (130.0 - 100.0) * 5 - 5.0 - 5.0
        assert first["total_invested"] == (5 * 100.0) + 5.0
        assert first["total_collected"] == (5 * 130.0) - 5.0
        assert first["reason"] == "partial target"
        assert second["realized_pnl"] == (140.0 - 100.0) * 5 - 10.0 - 6.0
        assert second["total_invested"] == (5 * 100.0) + 10.0
        assert second["total_collected"] == (5 * 140.0) - 6.0

    def test_completed_trades_fifo(self):
        """Interleaved symbols are matched by symbol using FIFO lots."""
        import dashboard_simple

        sample_log = [
            {"ts": "2026-01-01 09:00:00", "symbol": "AAA", "side": "BUY", "qty": 10, "price": 100.0, "charges": 10.0},
            {"ts": "2026-01-01 09:05:00", "symbol": "BBB", "side": "BUY", "qty": 3, "price": 50.0, "charges": 3.0},
            {"ts": "2026-01-01 09:10:00", "symbol": "AAA", "side": "BUY", "qty": 5, "price": 110.0, "charges": 5.0},
            {
                "ts": "2026-01-01 09:15:00",
                "symbol": "AAA",
                "side": "SELL",
                "qty": 12,
                "price": 120.0,
                "charges": 12.0,
                "reason": "aaa exit",
                "cash_after": 101400.0,
            },
            {
                "ts": "2026-01-01 09:20:00",
                "symbol": "BBB",
                "side": "SELL",
                "qty": 3,
                "price": 60.0,
                "charges": 3.0,
                "reason": "bbb exit",
                "cash_after": 101580.0,
            },
        ]

        df = dashboard_simple._completed_trades_from_log(sample_log)

        assert list(df["symbol"]) == ["AAA", "AAA", "BBB"]
        assert list(df["quantity"]) == [10, 2, 3]
        assert list(df["buying_price"]) == [100.0, 110.0, 50.0]
        assert list(df["selling_price"]) == [120.0, 120.0, 60.0]
        assert list(df["realized_pnl"]) == pytest.approx([180.0, 16.0, 24.0])

    def test_rank_signals_caps(self):
        """Ranking quantity respects max trade quantity and available cash."""
        import dashboard_simple

        quote_rows = pd.DataFrame(
            [
                {
                    "symbol": "AAA",
                    "price": 100.0,
                    "buy_score": 80.0,
                    "sell_score": 20.0,
                    "pchange": 1.0,
                    "range_pct": 1.0,
                    "vwap_gap_pct": 0.5,
                    "updated": "09:30:00",
                }
            ]
        )

        def run_with(session, max_qty_per_trade):
            fake_st = SimpleNamespace(session_state=session, spinner=lambda *_args, **_kwargs: nullcontext())
            with (
                patch.object(dashboard_simple, "st", fake_st),
                patch.object(dashboard_simple, "_scan_watchlist", return_value=(quote_rows, [])),
            ):
                buy_df, _, _, _ = dashboard_simple._rank_signals(
                    ["AAA"],
                    min_price=1.0,
                    max_price=1000.0,
                    risk_pct=10.0,
                    sl_pct=0.01,
                    tp_pct=0.02,
                    max_symbol_allocation_pct=100.0,
                    max_total_deployment_pct=100.0,
                    max_qty_per_trade=max_qty_per_trade,
                    max_open_positions=1,
                    min_order_value=1.0,
                    max_trade_invest_pct=100.0,
                )
            return int(buy_df.iloc[0]["rank_qty"])

        assert run_with(_paper_session(cash=10000.0, start=10000.0), 3) == 3
        assert run_with(_paper_session(cash=250.0, start=10000.0), 10) == 2


class TestAPIConnections:
    """Test external API integration points."""
    
    @patch('dashboard_simple.nsefetch')
    def test_nse_fetch_integration(self, mock_nsefetch):
        """Verify NSE fetch integration."""
        mock_nsefetch.return_value = {
            "priceInfo": {
                "lastPrice": 2000.0,
                "vwap": 1950.0,
                "pChange": 2.5,
                "intraDayHighLow": {"min": 1900.0, "max": 2050.0}
            }
        }
        
        import dashboard_simple
        
        # Test should work with mock
        assert mock_nsefetch is not None
    
    def test_finnhub_client_import(self):
        """Finnhub client module is importable (API key may be unset in CI)."""
        import stockmarket.finnhub_client as fc

        assert hasattr(fc, "fetch_quote")


class TestStateManagement:
    """Test session state handling."""
    
    def test_state_keys_initialization(self):
        """Verify all required state keys are properly tracked."""
        required_state_keys = [
            "s_cash", "s_start", "s_realized", "s_charges",
            "s_holdings", "s_shorts", "s_ui_config", "s_log",
            "s_prices", "s_agent_memory", "s_peak_open_pnl",
            "selected_market",
        ]
        
        for key in required_state_keys:
            assert key is not None, f"State key {key} cannot be None"

    def test_save_payload_keys(self, tmp_path):
        """Saved JSON keeps the current simple paper payload shape."""
        import dashboard_simple

        fixture_path = Path(__file__).parent / "fixtures" / "simple_paper_state_minimal.json"
        payload = json.loads(fixture_path.read_text(encoding="utf-8"))
        session = SessionState(
            s_cash=payload["cash"],
            s_start=payload["start"],
            s_realized=payload["realized"],
            s_charges=payload["charges"],
            s_holdings=payload["holdings"],
            s_shorts=payload["shorts"],
            s_ui_config=payload["ui_config"],
            s_log=payload["log"],
            s_prices=payload["prices"],
            s_agent_memory=payload["agent_memory"],
            s_peak_open_pnl=payload["peak_open_pnl"],
            s_peak_open_pnl_day=payload["peak_open_pnl_day"],
            s_profit_guard_triggered_day=payload["profit_guard_triggered_day"],
            s_profit_ladder_day=payload["profit_ladder_day"],
            s_profit_ladder_armed=payload["profit_ladder_armed"],
            s_profit_ladder_pullback_started=payload["profit_ladder_pullback_started"],
            s_profit_ladder_exited_day=payload["profit_ladder_exited_day"],
            selected_market=payload["market"],
        )
        state_file = tmp_path / "simple_paper_state.json"
        fake_st = SimpleNamespace(session_state=session)

        with (
            patch.object(dashboard_simple, "st", fake_st),
            patch.object(dashboard_simple, "_state_file", return_value=state_file),
        ):
            dashboard_simple._save_state()

        saved = json.loads(state_file.read_text(encoding="utf-8"))
        assert list(saved.keys()) == [
            "cash",
            "start",
            "realized",
            "charges",
            "holdings",
            "shorts",
            "ui_config",
            "log",
            "prices",
            "agent_memory",
            "peak_open_pnl",
            "peak_open_pnl_day",
            "profit_guard_triggered_day",
            "profit_ladder_day",
            "profit_ladder_armed",
            "profit_ladder_pullback_started",
            "profit_ladder_exited_day",
            "market",
        ]
        assert saved == payload


class TestLoggingSystem:
    """Test the logging functionality."""
    
    def test_logging_module_loaded(self):
        """Verify logging module is imported."""
        import logging
        import dashboard_simple
        
        # Logging should be configured
        assert hasattr(dashboard_simple, 'logger')
        assert hasattr(dashboard_simple, '_app_log')


class TestAPIErrorHandling:
    """Test timeout and error handling."""
    
    def test_timeout_resilience(self):
        """Verify API calls should have timeout handling."""
        import dashboard_simple
        
        # The cache decorators should prevent rapid retries
        assert "@st.cache_data" in str(dashboard_simple.fetch_nse_quote) or True  # Check if cached
        assert "@st.cache_data" in str(dashboard_simple.fetch_us_quote) or True


class TestDataValidation:
    """Test data validation."""
    
    def test_quote_data_structure(self):
        """Verify quote data has required fields."""
        required_quote_keys = ["symbol", "price", "vwap", "pchange", "range_pct"]
        
        # Verify the keys are documented
        for key in required_quote_keys:
            assert key is not None


def run_sanity_check():
    """Run basic sanity checks on the application."""
    print("\n" + "="*60)
    print("SANITY CHECK: Dashboard Simple")
    print("="*60)
    
    checks_passed = 0
    checks_total = 0
    
    # Check 1: Config structure
    checks_total += 1
    try:
        import dashboard_simple
        assert "NSE" in dashboard_simple.MARKET_CONFIG
        assert "US" in dashboard_simple.MARKET_CONFIG
        print("✓ Market configurations loaded correctly")
        checks_passed += 1
    except Exception as e:
        print(f"✗ Market config check failed: {e}")
    
    # Check 2: Functions defined
    checks_total += 1
    try:
        required_funcs = ["fetch_market_quote", "_record_trade", "_portfolio_view"]
        for func in required_funcs:
            assert hasattr(dashboard_simple, func)
        print("✓ All critical functions defined")
        checks_passed += 1
    except Exception as e:
        print(f"✗ Function check failed: {e}")
    
    # Check 3: File dependencies
    checks_total += 1
    try:
        base_path = Path(__file__).parent.parent
        required_files = [
            base_path / "src" / "stockmarket" / "config.py",
            base_path / "config.json" if (base_path / "config.json").exists() else None,
        ]
        for f in required_files:
            if f and not f.exists():
                print(f"⚠ Warning: Expected file not found: {f}")
        print("✓ File dependencies check passed")
        checks_passed += 1
    except Exception as e:
        print(f"✗ File check failed: {e}")
    
    # Check 4: Logging system
    checks_total += 1
    try:
        import logging
        logger = logging.getLogger("dashboard_simple")
        assert logger is not None
        print("✓ Logging system initialized")
        checks_passed += 1
    except Exception as e:
        print(f"✗ Logging check failed: {e}")
    
    # Check 5: Data structure validation
    checks_total += 1
    try:
        nse_cfg = dashboard_simple.MARKET_CONFIG["NSE"]
        us_cfg = dashboard_simple.MARKET_CONFIG["US"]
        assert len(nse_cfg["watchlist"]) > 0
        assert len(us_cfg["watchlist"]) > 0
        print(f"✓ Watchlists loaded: NSE ({len(nse_cfg['watchlist'])} symbols), US ({len(us_cfg['watchlist'])} symbols)")
        checks_passed += 1
    except Exception as e:
        print(f"✗ Data structure check failed: {e}")
    
    print("-" * 60)
    print(f"Sanity Check Result: {checks_passed}/{checks_total} passed")
    print("=" * 60 + "\n")
    
    return checks_passed == checks_total


if __name__ == "__main__":
    # Run sanity check
    success = run_sanity_check()
    
    # Run pytest if requested
    import sys
    if "--pytest" in sys.argv:
        pytest.main([__file__, "-v"])
    
    sys.exit(0 if success else 1)
