from __future__ import annotations

import importlib
import sys
from datetime import datetime, time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def test_simple_signals_view_import_isolated_from_dashboard_module():
    sys.modules.pop("dashboard_simple", None)
    mod = importlib.import_module("stockmarket.views.simple_signals_tables")
    assert hasattr(mod, "render_live_tables_and_errors_fragment")
    assert "dashboard_simple" not in sys.modules


def test_use_simple_views_env_flag(monkeypatch):
    import dashboard_simple

    monkeypatch.delenv("USE_SIMPLE_VIEWS", raising=False)
    assert dashboard_simple._use_simple_views() is False

    monkeypatch.setenv("USE_SIMPLE_VIEWS", "1")
    assert dashboard_simple._use_simple_views() is True


def test_cycle_factory_builds_services_with_session_repo():
    from stockmarket.cycle.factory import build_services
    from stockmarket.ml.null_scorer import NullSymbolScorer

    services = build_services(
        session={},
        market_now_fn=lambda: datetime(2026, 1, 1, 9, 30, 0),
        market_open=time(9, 15),
        entry_cutoff=time(13, 30),
        square_off=time(15, 15),
        charges_fn=lambda _side, _value: 0.0,
        rank_signals_fn=lambda **_kwargs: (
            pd.DataFrame(),
            pd.DataFrame(),
            pd.DataFrame(),
            [],
        ),
        refresh_prices_fn=lambda: None,
        persist_state_fn=lambda: None,
        use_paper_repo=False,
        state_file=Path("outputs") / "simple_paper_state.json",
        market="NSE",
        scorer=NullSymbolScorer(),
    )

    assert services.repo.__class__.__name__ == "SessionPaperRepo"
    assert services.signals.__class__.__name__ == "DashboardSignalSource"


def test_dashboard_legacy_warning_present():
    src = Path(__file__).parent.parent / "dashboard.py"
    content = src.read_text(encoding="utf-8")
    assert "Legacy scanner — use `streamlit run app.py`" in content


def test_webapp_legacy_warning_present():
    src = Path(__file__).parent.parent / "src" / "stockmarket" / "webapp.py"
    content = src.read_text(encoding="utf-8")
    assert "Legacy research UI." in content
