"""Service factory for dashboard cycle composition."""

from __future__ import annotations

from datetime import datetime, time
from typing import Any, Callable

import pandas as pd

from stockmarket.domain.scorer import SymbolScorer
from stockmarket.persistence.paper_repo import get_paper_repo

from .adapters.dashboard_signals import DashboardSignalSource
from .adapters.live_clock import LiveClock
from .adapters.log_history import LogHistoryQuery
from .adapters.session_prices import SessionPriceRefresh
from .adapters.streamlit_broker import StreamlitBroker
from .services import Services


def build_services(
    *,
    session: Any,
    market_now_fn: Callable[[], datetime],
    market_open: time,
    entry_cutoff: time,
    square_off: time,
    charges_fn: Callable[[str, float], float],
    rank_signals_fn: Callable[
        ..., tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, list]
    ],
    refresh_prices_fn: Callable[[], None],
    persist_state_fn: Callable[[], None],
    market: str,
    scorer: SymbolScorer,
    ml_enabled: bool = False,
    batch_ml_scores: Callable[[tuple[str, ...], float | str, float], dict[str, float]]
    | None = None,
    state_mtime: float | str = 0.0,
    model_mtime: float = 0.0,
) -> Services:
    """Build concrete Services for the dashboard cycle pipeline."""
    repo = get_paper_repo(market=market)

    return Services(
        clock=LiveClock(
            market_now_fn,
            market_open=market_open,
            entry_cutoff=entry_cutoff,
            square_off=square_off,
        ),
        broker=StreamlitBroker(charges_fn),
        signals=DashboardSignalSource(rank_signals_fn),
        history=LogHistoryQuery(market_open),
        repo=repo,
        prices=SessionPriceRefresh(session, refresh_prices_fn),
        charges_fn=charges_fn,
        scorer=scorer,
        ml_enabled=ml_enabled,
        batch_ml_scores=batch_ml_scores,
        state_mtime=state_mtime,
        model_mtime=model_mtime,
    )
