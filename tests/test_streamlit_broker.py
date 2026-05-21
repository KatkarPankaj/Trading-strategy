from __future__ import annotations

from datetime import datetime

import pytest

from stockmarket.cycle.adapters.streamlit_broker import StreamlitBroker
from stockmarket.domain import PaperState, Position


def _charges(side: str, turnover: float) -> float:
    import dashboard_simple

    return dashboard_simple._intraday_charges(side, turnover)


class SessionState(dict):
    def __getattr__(self, key):
        return self[key]

    def __setattr__(self, key, value):
        self[key] = value


def _session_dict():
    return SessionState(
        s_cash=100000.0,
        s_start=100000.0,
        s_realized=0.0,
        s_charges=0.0,
        s_holdings={},
        s_shorts={},
        s_ui_config={},
        s_log=[],
        s_prices={},
    )


def test_streamlit_broker_matches_record_trade_accounting():
    import dashboard_simple
    from types import SimpleNamespace
    from unittest.mock import patch

    when = datetime(2026, 1, 1, 9, 30, 0)
    broker = StreamlitBroker(_charges)

    legacy = _session_dict()
    fake_st = SimpleNamespace(session_state=legacy)
    with (
        patch.object(dashboard_simple, "st", fake_st),
        patch.object(dashboard_simple, "_save_state"),
        patch.object(dashboard_simple, "market_now", return_value=when),
    ):
        dashboard_simple._record_trade("ABC", "BUY", 10, 100.0, "entry", sl_pct=0.01, tp_pct=0.02)
        dashboard_simple._record_trade("ABC", "SELL", 4, 110.0, "partial exit")
        dashboard_simple._record_trade("XYZ", "SHORT", 5, 200.0, "short entry", sl_pct=0.01, tp_pct=0.02)
        dashboard_simple._record_trade("XYZ", "COVER", 2, 190.0, "partial cover")

    pipeline = PaperState(
        start_capital=100000.0,
        cash=100000.0,
        realized=0.0,
        charges=0.0,
        holdings={},
        shorts={},
        prices={},
        log=[],
    )
    broker.execute(pipeline, symbol="ABC", side="BUY", qty=10, price=100.0, reason="entry", when=when, sl_pct=0.01, tp_pct=0.02)
    broker.execute(pipeline, symbol="ABC", side="SELL", qty=4, price=110.0, reason="partial exit", when=when)
    broker.execute(pipeline, symbol="XYZ", side="SHORT", qty=5, price=200.0, reason="short entry", when=when, sl_pct=0.01, tp_pct=0.02)
    broker.execute(pipeline, symbol="XYZ", side="COVER", qty=2, price=190.0, reason="partial cover", when=when)

    assert pipeline.cash == pytest.approx(legacy["s_cash"], abs=1e-6)
    assert pipeline.realized == pytest.approx(legacy["s_realized"], abs=1e-6)
    assert pipeline.charges == pytest.approx(legacy["s_charges"], abs=1e-6)
    assert pipeline.holdings["ABC"].qty == legacy["s_holdings"]["ABC"]["qty"]
    assert pipeline.shorts["XYZ"].qty == legacy["s_shorts"]["XYZ"]["qty"]
    assert [row.side for row in pipeline.log] == ["BUY", "SELL", "SHORT", "COVER"]
