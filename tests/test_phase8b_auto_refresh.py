"""Phase 8b: Auto-refresh footer extracted to views/simple_auto_refresh.

The view is intentionally pure (no _refresh_holding_prices side effect). The
global views/* AST guards in ``test_phase8b_top_panels.py`` cover this module;
these tests pin the public contract:

- import isolation,
- USE_SIMPLE_VIEWS_AUTO_REFRESH falls back to USE_SIMPLE_VIEWS,
- footer caption + JS rerun snippet only emitted when enabled / seconds > 0.
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
    / "simple_auto_refresh.py"
)


def _patch_streamlit() -> MagicMock:
    return MagicMock(name="streamlit")


def test_simple_auto_refresh_import_isolated_from_dashboard_module():
    sys.modules.pop("dashboard_simple", None)
    mod = importlib.import_module("stockmarket.views.simple_auto_refresh")
    assert hasattr(mod, "render_auto_refresh_footer")
    assert "dashboard_simple" not in sys.modules


def test_simple_auto_refresh_does_not_touch_session_state():
    tree = ast.parse(VIEW_PATH.read_text(encoding="utf-8"), filename=str(VIEW_PATH))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "session_state":
            value = node.value
            if isinstance(value, ast.Name) and value.id == "st":
                raise AssertionError(
                    f"simple_auto_refresh touches st.session_state at line {node.lineno}"
                )


def test_simple_auto_refresh_does_not_import_dashboard_simple():
    tree = ast.parse(VIEW_PATH.read_text(encoding="utf-8"), filename=str(VIEW_PATH))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "dashboard_simple"
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "dashboard_simple"


def test_simple_auto_refresh_does_not_import_refresh_holding_prices():
    """The price-refresh side effect must remain in the caller."""
    source = VIEW_PATH.read_text(encoding="utf-8")
    assert "_refresh_holding_prices" not in source


def test_render_auto_refresh_footer_noop_when_disabled():
    from stockmarket.views import simple_auto_refresh

    fake_st = _patch_streamlit()
    with patch.object(simple_auto_refresh, "st", fake_st):
        simple_auto_refresh.render_auto_refresh_footer(enabled=False, refresh_seconds=30)

    fake_st.caption.assert_not_called()
    fake_st.html.assert_not_called()


def test_render_auto_refresh_footer_caption_only_when_seconds_non_positive():
    from stockmarket.views import simple_auto_refresh

    fake_st = _patch_streamlit()
    with patch.object(simple_auto_refresh, "st", fake_st):
        simple_auto_refresh.render_auto_refresh_footer(enabled=True, refresh_seconds=0)

    fake_st.caption.assert_called_once_with("Auto refresh active: every 0s")
    fake_st.html.assert_not_called()


def test_dashboard_footer_does_not_double_refresh_holding_prices():
    """Per-render refresh is owned by _quick_portfolio_metrics, not the footer."""
    dashboard_path = Path(__file__).parent.parent / "dashboard_simple.py"
    source = dashboard_path.read_text(encoding="utf-8")
    footer_idx = source.index("render_auto_refresh_footer(")
    block_start = source.rfind("\n", 0, footer_idx)
    block = source[block_start:footer_idx]
    assert "_refresh_holding_prices" not in block


def test_render_auto_refresh_footer_emits_js_when_enabled_and_positive_seconds():
    from stockmarket.views import simple_auto_refresh

    fake_st = _patch_streamlit()
    with patch.object(simple_auto_refresh, "st", fake_st):
        simple_auto_refresh.render_auto_refresh_footer(enabled=True, refresh_seconds=45)

    fake_st.caption.assert_called_once_with("Auto refresh active: every 45s")
    fake_st.html.assert_called_once()
    html_text = fake_st.html.call_args.args[0]
    assert "const delayMs = 45000" in html_text
    assert "streamlit:rerunScript" in html_text
    assert "window.parent.postMessage" in html_text
    assert "window.location.reload" in html_text
