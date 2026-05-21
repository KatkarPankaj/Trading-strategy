"""Service factory for dashboard cycle composition."""

from __future__ import annotations

from datetime import datetime, time
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from stockmarket.domain.scorer import SymbolScorer
from stockmarket.persistence.paper_repo import get_paper_repo

from .adapters.dashboard_signals import DashboardSignalSource
from .adapters.live_clock import LiveClock
from .adapters.log_history import LogHistoryQuery
from .adapters.session_prices import SessionPriceRefresh
from .adapters.session_repo import SessionPaperRepo
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
    use_paper_repo: bool,
    state_file: Path,
    market: str,
    scorer: SymbolScorer,
) -> Services:
    """Build concrete Services for the dashboard cycle pipeline."""
    if use_paper_repo:
        repo = get_paper_repo(state_file, market)
    else:
        repo = SessionPaperRepo(session, persist_fn=persist_state_fn)

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
    )
