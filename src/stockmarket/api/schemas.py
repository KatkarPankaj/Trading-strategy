"""Request schemas: strict validation at the API boundary."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..core.models import OrderSide, OrderType
from ..core.research import ResearchEvidence
from ..core.risk import OrderIntent

_ID = r"^[A-Za-z0-9:._\-^=&]+$"


def _price() -> Any:
    return Field(default=None, gt=0, allow_inf_nan=False)


class AuditBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    technical_signals: dict[str, Any] = Field(
        default_factory=dict, max_length=50)
    news_signals: dict[str, Any] = Field(default_factory=dict, max_length=50)
    ai_analysis_ids: list[str] = Field(default_factory=list, max_length=20)
    strategy_decision_id: str | None = Field(default=None, max_length=64)
    sizing: dict[str, Any] = Field(default_factory=dict, max_length=50)
    data_reference: str | None = Field(default=None, max_length=200)
    exit_reason: str | None = Field(default=None, max_length=64)


class OrderBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_order_id: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")
    instrument_id: str = Field(min_length=1, max_length=64, pattern=_ID)
    side: OrderSide
    quantity: int = Field(gt=0, le=1_000_000_000)
    order_type: OrderType
    strategy: str = Field(min_length=1, max_length=64)
    limit_price: float | None = _price()
    stop_price: float | None = _price()
    stop_loss: float | None = _price()
    take_profit: float | None = _price()
    signal_id: UUID | None = None
    intent: OrderIntent = OrderIntent.ENTRY
    market_regime: str | None = Field(
        default=None, max_length=32, pattern=r"^[A-Z_]+$")
    parameters_hash: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$")
    audit: AuditBody | None = None


class ResearchEvidenceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    component: Literal["volume", "momentum", "sector", "news", "fundamental", "history"]
    score: float = Field(ge=-1, le=1, allow_inf_nan=False)
    observed_at: datetime
    source: str = Field(min_length=1, max_length=256)
    history_trades: int = Field(default=0, ge=0)
    max_age_seconds: int | None = Field(default=None, gt=0, le=31_536_000)

    @field_validator("observed_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return value

    def to_domain(self, instrument_id: str) -> ResearchEvidence:
        return ResearchEvidence(
            instrument_id=instrument_id,
            component=self.component,
            score=self.score,
            observed_at=self.observed_at,
            source=self.source,
            history_trades=self.history_trades,
            max_age=timedelta(seconds=self.max_age_seconds)
            if self.max_age_seconds is not None else None,
        )


class ResearchRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instrument_id: str = Field(min_length=1, max_length=64, pattern=_ID)
    as_of: datetime | None = None
    evidence: list[ResearchEvidenceBody] = Field(default_factory=list, max_length=8)

    @field_validator("as_of")
    @classmethod
    def require_aware_as_of(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("as_of must be timezone-aware")
        return value


class ResumeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operator: str = Field(min_length=1, max_length=64)
    note: str = Field(min_length=1, max_length=500)
    acknowledged: list[str] = Field(default_factory=list, max_length=100)


class KillTriggerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operator: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=200)
    cancel_open_orders: bool = False


class KillResetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operator: str = Field(min_length=1, max_length=64)
    note: str = Field(min_length=1, max_length=500)
    override: bool = False
