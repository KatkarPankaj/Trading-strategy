"""Paper-trading persistence port."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from stockmarket.domain.types import DailyCounters, PaperState

_DEFAULT_STATE_FILES = {
    "NSE": Path("outputs") / "simple_paper_state.json",
    "US": Path("outputs") / "simple_paper_state_us.json",
}


class PaperRepo(Protocol):
    def load(self) -> tuple[PaperState, DailyCounters] | None: ...

    def save(self, state: PaperState, counters: DailyCounters) -> None: ...

    def path(self) -> Path: ...


def get_paper_repo(path: Path | None = None, market: str = "NSE") -> PaperRepo:
    from stockmarket.persistence.json_paper_repo import JsonPaperRepo

    normalized_market = market.upper()
    return JsonPaperRepo(
        Path(path) if path is not None else _DEFAULT_STATE_FILES.get(normalized_market, _DEFAULT_STATE_FILES["NSE"]),
        market=normalized_market,
    )
