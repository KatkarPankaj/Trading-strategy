"""In-memory session repository for cycle runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from stockmarket.domain.types import DailyCounters, PaperState
from stockmarket.state.session_bridge import (
    counters_to_session_state,
    paper_state_to_session_state,
    session_state_to_counters,
    session_state_to_paper_state,
)


class SessionPaperRepo:
    def __init__(self, session: Any, *, persist_fn=None):
        self._session = session
        self._persist = persist_fn

    def load(self) -> tuple[PaperState, DailyCounters] | None:
        if self._session is None:
            return None
        return session_state_to_paper_state(self._session), session_state_to_counters(
            self._session
        )

    def save(self, state: PaperState, counters: DailyCounters) -> None:
        paper_state_to_session_state(self._session, state)
        counters_to_session_state(self._session, counters)
        if self._persist is not None:
            self._persist()

    def path(self) -> Path:
        return Path("session")
