"""Price refresh via dashboard session."""

from __future__ import annotations

from typing import Any, Callable

from stockmarket.domain.types import PaperState
from stockmarket.state.session_bridge import (
    paper_state_to_session_state,
    session_state_to_paper_state,
)


class SessionPriceRefresh:
    def __init__(self, session: Any, refresh_fn: Callable[[], None]):
        self._session = session
        self._refresh_fn = refresh_fn

    def refresh(self, state: PaperState) -> None:
        paper_state_to_session_state(self._session, state)
        self._refresh_fn()
        refreshed = session_state_to_paper_state(self._session)
        state.prices = dict(refreshed.prices)


class NoOpPriceRefresh:
    def refresh(self, state: PaperState) -> None:
        return
