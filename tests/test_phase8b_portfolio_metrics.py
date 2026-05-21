"""Slice 4 of phase 8b: Portfolio metrics block extracted to views.

The view receives a frozen ``PortfolioSnapshot``; price refresh and the
``_portfolio_view()`` query stay in the caller. The global views/* AST guards
in ``test_phase8b_top_panels.py`` cover ``simple_portfolio_metrics.py``; these
tests pin its render-shape contract, the snapshot construction, the flag
matrix, and the import-isolation invariants.
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


VIEW_PATH = (
    Path(__file__).parent.parent
    / "src"
    / "stockmarket"
    / "views"
    / "simple_portfolio_metrics.py"
)


def _patch_streamlit(table_view: bool = False) -> MagicMock:
    fake_st = MagicMock(name="streamlit")
    col = MagicMock()
    col.__enter__ = MagicMock(return_value=None)
    col.__exit__ = MagicMock(return_value=False)
    fake_st.columns.side_effect = lambda spec: tuple(
        col for _ in range(spec if isinstance(spec, int) else len(spec))
    )
    fake_st.checkbox.return_value = table_view
    return fake_st


def _snapshot(**overrides):
    from stockmarket.domain import PortfolioSnapshot

    base = dict(
        start_capital=100_000.0,
        cash=50_000.0,
        equity=110_000.0,
        equity_delta=10_000.0,
        unrealized=2_000.0,
        realized=8_000.0,
        charges=500.0,
        net_realized=7_500.0,
        today_pnl=1_500.0,
        daily_profit_target=4_000.0,
        currency_symbol="Rs",
    )
    base.update(overrides)
    return PortfolioSnapshot(**base)


def test_simple_portfolio_metrics_import_isolated_from_dashboard_module():
    sys.modules.pop("dashboard_simple", None)
    mod = importlib.import_module("stockmarket.views.simple_portfolio_metrics")
    assert hasattr(mod, "render_portfolio_metrics")
    assert "dashboard_simple" not in sys.modules


def test_simple_portfolio_metrics_does_not_touch_session_state():
    tree = ast.parse(VIEW_PATH.read_text(encoding="utf-8"), filename=str(VIEW_PATH))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "session_state":
            value = node.value
            if isinstance(value, ast.Name) and value.id == "st":
                raise AssertionError(
                    f"simple_portfolio_metrics touches st.session_state at line {node.lineno}"
                )


def test_simple_portfolio_metrics_does_not_import_dashboard_simple():
    tree = ast.parse(VIEW_PATH.read_text(encoding="utf-8"), filename=str(VIEW_PATH))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "dashboard_simple"
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "dashboard_simple"


def test_portfolio_snapshot_is_frozen_dataclass():
    snap = _snapshot()
    try:
        snap.cash = 0.0  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("PortfolioSnapshot must be frozen")


def test_render_portfolio_metrics_metric_view_renders_six_columns_with_positive_delta():
    from stockmarket.views import simple_portfolio_metrics

    fake_st = _patch_streamlit(table_view=False)
    with patch.object(simple_portfolio_metrics, "st", fake_st):
        simple_portfolio_metrics.render_portfolio_metrics(_snapshot())

    # checkbox keyed identically to legacy block
    fake_st.checkbox.assert_called_once()
    assert fake_st.checkbox.call_args.kwargs.get("key") == "portfolio_metrics_tabular"

    # Six metric columns + the [5,1] header columns => columns called twice
    column_specs = [c.args[0] for c in fake_st.columns.call_args_list]
    assert [5, 1] in column_specs
    assert 6 in column_specs

    # Six markdown calls for the six metric tiles
    assert fake_st.markdown.call_count == 6
    html_blocks = "\n".join(c.args[0] for c in fake_st.markdown.call_args_list)
    assert "Start Capital" in html_blocks
    assert "Current Equity" in html_blocks
    assert "Cash" in html_blocks
    assert "Open PnL" in html_blocks
    assert "Realized PnL" in html_blocks
    assert "Total Charges" in html_blocks
    assert "Rs 110,000.00" in html_blocks
    # positive equity_delta => green colour
    assert "#198754" in html_blocks
    # positive net_realized => green net
    assert "Net Rs 7,500.00" in html_blocks

    # Progress: 1500/4000 = 37.5%
    fake_st.progress.assert_called_once()
    progress_val = fake_st.progress.call_args.args[0]
    progress_text = fake_st.progress.call_args.kwargs["text"]
    assert abs(progress_val - 0.375) < 1e-9
    assert "Rs 1,500.00" in progress_text
    assert "Rs 4,000.00" in progress_text
    assert "37.5%" in progress_text


def test_render_portfolio_metrics_zero_pnl_does_not_break():
    from stockmarket.views import simple_portfolio_metrics

    fake_st = _patch_streamlit(table_view=False)
    snap = _snapshot(
        equity=100_000.0,
        equity_delta=0.0,
        unrealized=0.0,
        realized=0.0,
        charges=0.0,
        net_realized=0.0,
        today_pnl=0.0,
    )
    with patch.object(simple_portfolio_metrics, "st", fake_st):
        simple_portfolio_metrics.render_portfolio_metrics(snap)

    progress_val = fake_st.progress.call_args.args[0]
    assert progress_val == 0.0
    progress_text = fake_st.progress.call_args.kwargs["text"]
    assert "Rs 0.00" in progress_text and "0.0%" in progress_text


def test_render_portfolio_metrics_negative_delta_uses_red_subtext():
    from stockmarket.views import simple_portfolio_metrics

    fake_st = _patch_streamlit(table_view=False)
    snap = _snapshot(
        equity=80_000.0,
        equity_delta=-20_000.0,
        charges=15_000.0,
        realized=5_000.0,
        net_realized=-10_000.0,
        today_pnl=-2_500.0,
    )
    with patch.object(simple_portfolio_metrics, "st", fake_st):
        simple_portfolio_metrics.render_portfolio_metrics(snap)

    html_blocks = "\n".join(c.args[0] for c in fake_st.markdown.call_args_list)
    assert "#dc3545" in html_blocks
    assert "Δ Rs -20,000.00" in html_blocks
    assert "Net Rs -10,000.00" in html_blocks

    # progress clamped to >=0 when today_pnl is negative
    progress_val = fake_st.progress.call_args.args[0]
    assert progress_val == 0.0


def test_render_portfolio_metrics_progress_clamped_when_exceeds_target():
    from stockmarket.views import simple_portfolio_metrics

    fake_st = _patch_streamlit(table_view=False)
    snap = _snapshot(today_pnl=12_000.0, daily_profit_target=4_000.0)
    with patch.object(simple_portfolio_metrics, "st", fake_st):
        simple_portfolio_metrics.render_portfolio_metrics(snap)

    progress_val = fake_st.progress.call_args.args[0]
    assert progress_val == 1.0
    assert "300.0%" in fake_st.progress.call_args.kwargs["text"]


def test_render_portfolio_metrics_zero_target_yields_zero_progress():
    from stockmarket.views import simple_portfolio_metrics

    fake_st = _patch_streamlit(table_view=False)
    snap = _snapshot(today_pnl=1_500.0, daily_profit_target=0.0)
    with patch.object(simple_portfolio_metrics, "st", fake_st):
        simple_portfolio_metrics.render_portfolio_metrics(snap)

    progress_val = fake_st.progress.call_args.args[0]
    assert progress_val == 0.0


def test_render_portfolio_metrics_table_view_renders_dataframe_no_metric_tiles():
    from stockmarket.views import simple_portfolio_metrics

    fake_st = _patch_streamlit(table_view=True)
    with patch.object(simple_portfolio_metrics, "st", fake_st):
        simple_portfolio_metrics.render_portfolio_metrics(_snapshot())

    fake_st.dataframe.assert_called_once()
    fake_st.markdown.assert_not_called()
    df_arg = fake_st.dataframe.call_args.args[0]
    metrics = df_arg["Metric"].tolist()
    assert metrics == [
        "Start Capital",
        "Current Equity",
        "Cash",
        "Open PnL",
        "Realized PnL",
        "Total Charges",
    ]
    detail_for_charges = df_arg.loc[df_arg["Metric"] == "Total Charges", "Detail"].iloc[0]
    assert "Net Rs 7,500.00" in detail_for_charges


def test_render_portfolio_metrics_caption_mentions_net_realized():
    from stockmarket.views import simple_portfolio_metrics

    fake_st = _patch_streamlit(table_view=False)
    with patch.object(simple_portfolio_metrics, "st", fake_st):
        simple_portfolio_metrics.render_portfolio_metrics(_snapshot())

    captions = [c.args[0] for c in fake_st.caption.call_args_list]
    assert any("Portfolio summary" in t for t in captions)
    assert any("Net (under Total Charges)" in t and "Rs 7,500.00" in t for t in captions)
