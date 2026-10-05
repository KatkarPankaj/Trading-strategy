"""Request schemas: strict validation at the API boundary."""

from __future__ import annotations

from uuid import UUID

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..core.models import OrderSide, OrderType
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
