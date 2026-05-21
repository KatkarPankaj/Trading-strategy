"""Idle buy fallback policy."""

from __future__ import annotations

from stockmarket.domain.types import CycleSettings


class DefaultIdleFallbackPolicy:
    def allow_below_min_score(
        self, *, idle_minutes: float, settings: CycleSettings
    ) -> bool:
        return float(idle_minutes) >= float(settings.signals.idle_buy_fallback_minutes)
