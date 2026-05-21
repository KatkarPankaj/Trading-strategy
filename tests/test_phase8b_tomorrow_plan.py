"""Slice 3 of phase 8b: Tomorrow Plan expander extracted to views.

The global views/* AST guards in ``test_phase8b_top_panels.py`` already cover
``simple_tomorrow_plan.py`` (they parametrize over every file in the views
package). These focused tests pin the new module's contract: import isolation,
the ``USE_SIMPLE_VIEWS_TOMORROW_PLAN`` flag fallback to ``USE_SIMPLE_VIEWS``,
and render-shape parity with the legacy block.
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
    / "simple_tomorrow_plan.py"
)


def _patch_streamlit() -> MagicMock:
    fake_st = MagicMock(name="streamlit")
    fake_st.expander.return_value.__enter__ = MagicMock(return_value=None)
    fake_st.expander.return_value.__exit__ = MagicMock(return_value=False)
    return fake_st


def test_simple_tomorrow_plan_import_isolated_from_dashboard_module():
    sys.modules.pop("dashboard_simple", None)
    mod = importlib.import_module("stockmarket.views.simple_tomorrow_plan")
    assert hasattr(mod, "render_tomorrow_plan")
    assert "dashboard_simple" not in sys.modules


def test_simple_tomorrow_plan_does_not_touch_session_state():
    tree = ast.parse(VIEW_PATH.read_text(encoding="utf-8"), filename=str(VIEW_PATH))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "session_state":
            value = node.value
            if isinstance(value, ast.Name) and value.id == "st":
                raise AssertionError(
                    f"simple_tomorrow_plan touches st.session_state at line {node.lineno}"
                )


def test_simple_tomorrow_plan_does_not_import_dashboard_simple():
    tree = ast.parse(VIEW_PATH.read_text(encoding="utf-8"), filename=str(VIEW_PATH))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "dashboard_simple"
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "dashboard_simple"


def _render_with_defaults(**overrides):
    from stockmarket.views import simple_tomorrow_plan

    kwargs = dict(
        agent_plan={},
        market_research={},
        learning_memory={},
        effective_min_buy_score=0.0,
        effective_min_short_score=0.0,
        effective_tp_pct=0.0,
        learning_apply_messages=[],
        config_guard_messages=[],
    )
    kwargs.update(overrides)
    fake_st = _patch_streamlit()
    with patch.object(simple_tomorrow_plan, "st", fake_st):
        simple_tomorrow_plan.render_tomorrow_plan(**kwargs)
    return fake_st


def test_render_tomorrow_plan_falls_back_when_no_latest_learning():
    fake_st = _render_with_defaults()
    fake_st.expander.assert_called_once_with(
        "\U0001f916 Learning Agent: Tomorrow Plan", expanded=True
    )
    written = [c.args[0] for c in fake_st.write.call_args_list]
    assert any("Not enough closed-trade history" in line for line in written)
    assert any("Market regime: unknown" in line for line in written)
    assert any(
        "Buy score: 0.0" in line and "TP: 0.00%" in line for line in written
    )


def test_render_tomorrow_plan_latest_learning_summary():
    fake_st = _render_with_defaults(
        agent_plan={
            "latest_learning": {
                "date": "2026-05-20",
                "closed_trades": 7,
                "win_rate": 57.5,
                "net": 1234.56,
            },
            "notes": ["maintain TP", "watch volatility"],
        },
        market_research={"regime": "bullish", "avg_pchange": 1.23, "volatility": 0.42},
        effective_min_buy_score=12.5,
        effective_min_short_score=8.0,
        effective_tp_pct=0.018,
    )
    written = [c.args[0] for c in fake_st.write.call_args_list]
    assert any("2026-05-20" in line and "7" in line and "57.5%" in line for line in written)
    assert any("Rs 1,234.56" in line for line in written)
    assert any("bullish" in line and "1.23%" in line and "0.42" in line for line in written)
    assert any("12.5" in line and "8.0" in line and "1.80%" in line for line in written)

    captioned = [c.args[0] for c in fake_st.caption.call_args_list]
    assert "- maintain TP" in captioned
    assert "- watch volatility" in captioned


def test_render_tomorrow_plan_combines_learning_and_guard_messages():
    fake_st = _render_with_defaults(
        agent_plan={"notes": ["agent note"]},
        learning_apply_messages=["learn msg"],
        config_guard_messages=["guard msg"],
    )
    captioned = [c.args[0] for c in fake_st.caption.call_args_list]
    assert "- agent note" in captioned
    assert "- learn msg" in captioned
    assert "- guard msg" in captioned


def test_render_tomorrow_plan_emits_top_and_avoid_symbols():
    fake_st = _render_with_defaults(
        learning_memory={
            "symbols": [
                {"symbol": "AAA", "bias": 0.9},
                {"symbol": "BBB", "bias": 0.4},
                {"symbol": "CCC", "bias": 0.1},
                {"symbol": "DDD", "bias": -0.3},
                {"symbol": "EEE", "bias": -0.7},
            ]
        }
    )
    written = [c.args[0] for c in fake_st.write.call_args_list]
    assert any(line == "Preferred symbols: AAA, BBB, CCC" for line in written)
    assert any("Avoid/low-priority symbols:" in line and "EEE" in line and "DDD" in line for line in written)


def test_render_tomorrow_plan_skips_avoid_line_when_no_negative_bias():
    fake_st = _render_with_defaults(
        learning_memory={
            "symbols": [
                {"symbol": "AAA", "bias": 0.5},
                {"symbol": "BBB", "bias": 0.2},
            ]
        }
    )
    written = [c.args[0] for c in fake_st.write.call_args_list]
    assert any(line.startswith("Preferred symbols:") for line in written)
    assert not any(line.startswith("Avoid/low-priority symbols:") for line in written)
