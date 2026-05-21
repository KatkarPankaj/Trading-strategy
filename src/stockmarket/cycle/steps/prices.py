"""Price refresh step."""

from __future__ import annotations

from ..context import CycleContext
from ..services import Services


def refresh_holding_prices(ctx: CycleContext, svc: Services) -> CycleContext:
    svc.prices.refresh(ctx.state)
    return ctx
