"""Apply effective scores to ranked signal frames via Services.scorer."""

from __future__ import annotations

from ..context import CycleContext
from ..scoring import apply_scorer_to_ranked_signals
from ..services import Services


def apply_signal_scores(ctx: CycleContext, svc: Services) -> CycleContext:
    ctx.signals = apply_scorer_to_ranked_signals(
        ctx.signals,
        svc.scorer,
        ml_enabled=svc.ml_enabled,
        batch_ml_scores=svc.batch_ml_scores,
        state_mtime=svc.state_mtime,
        model_mtime=svc.model_mtime,
    )
    return ctx
