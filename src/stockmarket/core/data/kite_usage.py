"""Fail-closed application budget for future Kite REST integration.

The ledger counts tracked REST request attempts, not provider billing credits. No API calls are
made by this module; callers must reserve before each request and complete the ticket afterwards.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import floor
from typing import Callable, Mapping
from uuid import uuid4

from ..persistence.repositories import KiteUsageRepository

_CATEGORIES = frozenset({
    "authentication", "instrument_master", "quote", "historical", "recovery",
})
_ESSENTIAL_CATEGORIES = frozenset({"authentication", "recovery"})
_HISTORICAL_PREFIX = "/instruments/historical/"
_FAILURE_OUTCOMES = frozenset({
    "TIMEOUT", "CONNECTION_ERROR", "AUTHENTICATION_ERROR", "PERMISSION_ERROR",
    "INVALID_REQUEST", "PROVIDER_ERROR",
})


class KiteUsageError(RuntimeError):
    """A Kite request cannot be safely admitted or its usage cannot be recorded."""


class KiteBudgetExceeded(KiteUsageError):
    """The configured rolling request budget or nonessential hard stop was reached."""


@dataclass(frozen=True, slots=True)
class KiteUsagePolicy:
    tracked_request_budget: int = 300
    warning_thresholds: tuple[int, ...] = (50, 70, 85)
    hard_stop_percent: int = 90
    reserve_percent: int = 10
    tracking_enabled: bool = True
    rolling_days: int = 30

    def __post_init__(self) -> None:
        if isinstance(self.tracked_request_budget, bool) or not isinstance(
            self.tracked_request_budget, int
        ) or self.tracked_request_budget < 1:
            raise ValueError("KITE_MONTHLY_CREDIT_BUDGET must be a positive integer")
        if not isinstance(self.warning_thresholds, tuple) or not self.warning_thresholds \
                or any(isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 99
                       for v in self.warning_thresholds) \
                or tuple(sorted(set(self.warning_thresholds))) != self.warning_thresholds:
            raise ValueError("KITE_WARNING_THRESHOLDS must be ascending unique percentages in [1, 99]")
        if isinstance(self.hard_stop_percent, bool) or not isinstance(
            self.hard_stop_percent, int
        ) or not 1 <= self.hard_stop_percent <= 100:
            raise ValueError("KITE_HARD_STOP_PERCENT must be an integer in [1, 100]")
        if isinstance(self.reserve_percent, bool) or not isinstance(
            self.reserve_percent, int
        ) or not 0 <= self.reserve_percent < 100:
            raise ValueError("KITE_RESERVE_PERCENT must be an integer in [0, 99]")
        if not isinstance(self.tracking_enabled, bool):
            raise TypeError("KITE_USAGE_TRACKING_ENABLED must be a bool")
        if self.rolling_days != 30:
            raise ValueError("Kite application request accounting uses a rolling 30-day window")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "KiteUsagePolicy":
        env = os.environ if env is None else env
        try:
            budget = int(env.get("KITE_MONTHLY_CREDIT_BUDGET", "300"))
            warnings = tuple(int(value.strip()) for value in
                             env.get("KITE_WARNING_THRESHOLDS", "50,70,85").split(","))
            hard_stop = int(env.get("KITE_HARD_STOP_PERCENT", "90"))
            reserve = int(env.get("KITE_RESERVE_PERCENT", "10"))
        except ValueError:
            raise ValueError("Kite request budget settings must be valid integers") from None
        tracking = env.get("KITE_USAGE_TRACKING_ENABLED", "true").strip().lower()
        if tracking not in {"true", "false"}:
            raise ValueError("KITE_USAGE_TRACKING_ENABLED must be true or false")
        return cls(
            tracked_request_budget=budget,
            warning_thresholds=warnings,
            hard_stop_percent=hard_stop,
            reserve_percent=reserve,
            tracking_enabled=tracking == "true",
        )

    @property
    def nonessential_limit(self) -> int:
        hard_stop_count = floor(self.tracked_request_budget * self.hard_stop_percent / 100)
        reserve_limit = floor(self.tracked_request_budget * (100 - self.reserve_percent) / 100)
        return min(hard_stop_count, reserve_limit)


@dataclass(frozen=True, slots=True)
class KiteRequestTicket:
    request_id: str


class KiteUsageController:
    """Budget gate and persistent tracker for each future Kite REST request attempt."""

    def __init__(
        self,
        repository: KiteUsageRepository,
        policy: KiteUsagePolicy | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._repository = repository
        self.policy = policy or KiteUsagePolicy()
        self._clock = clock

    def begin_request(
        self, *, category: str, endpoint: str, purpose: str,
        method: str = "GET", essential: bool = False,
    ) -> KiteRequestTicket:
        self._validate_request(category, method, endpoint, purpose, essential)
        if not self.policy.tracking_enabled:
            raise KiteUsageError("KITE_USAGE_TRACKING_DISABLED: request blocked")
        now = self._now()
        request_id = str(uuid4())
        used, admitted = self._repository.reserve(
            request_id=request_id,
            requested_at=now,
            category=category,
            method=method,
            endpoint=endpoint,
            purpose=purpose,
            essential=essential,
            rolling_since=now - timedelta(days=self.policy.rolling_days),
            request_budget=self.policy.tracked_request_budget,
            nonessential_limit=self.policy.nonessential_limit,
        )
        if not admitted:
            reason = (
                "KITE_REQUEST_BUDGET_EXHAUSTED"
                if used >= self.policy.tracked_request_budget
                else "KITE_NONESSENTIAL_HARD_STOP"
            )
            raise KiteBudgetExceeded(f"{reason}: REST request blocked")
        return KiteRequestTicket(request_id)

    def record_response(self, ticket: KiteRequestTicket, status_code: int) -> None:
        if not isinstance(ticket, KiteRequestTicket):
            raise TypeError("ticket must be a KiteRequestTicket")
        if isinstance(status_code, bool) or not isinstance(status_code, int) or not 100 <= status_code <= 599:
            raise ValueError("HTTP response status must be an integer in [100, 599]")
        outcome = "HTTP_RESPONSE" if 200 <= status_code < 400 else "HTTP_ERROR"
        self._repository.complete(
            ticket.request_id, response_status=status_code,
            outcome=outcome, completed_at=self._now())

    def record_failure(self, ticket: KiteRequestTicket, outcome: str) -> None:
        if not isinstance(ticket, KiteRequestTicket):
            raise TypeError("ticket must be a KiteRequestTicket")
        if not isinstance(outcome, str):
            raise TypeError("failure outcome must be a string")
        if outcome not in _FAILURE_OUTCOMES:
            raise ValueError(f"failure outcome must be one of {sorted(_FAILURE_OUTCOMES)}")
        self._repository.complete(
            ticket.request_id, response_status=None,
            outcome=outcome, completed_at=self._now())

    def snapshot(self) -> dict[str, object]:
        now = self._now()
        usage = self._repository.snapshot(
            rolling_since=now - timedelta(days=self.policy.rolling_days))
        used = int(usage["tracked_rest_requests"])
        budget = self.policy.tracked_request_budget
        percentage = used * 100 / budget
        if not self.policy.tracking_enabled:
            status = "DISABLED"
        elif used >= budget:
            status = "BUDGET_EXHAUSTED"
        elif used >= self.policy.nonessential_limit:
            status = "HARD_STOP"
        elif percentage >= self.policy.warning_thresholds[0]:
            status = "WARNING"
        else:
            status = "NORMAL"
        return {
            **usage,
            "metric": "tracked_rest_requests",
            "billing_unit": "UNVERIFIED_NOT_CREDITS",
            "window": "rolling_30_days",
            "window_start": (now - timedelta(days=self.policy.rolling_days)).isoformat(),
            "budget": budget,
            "used_percent": round(percentage, 2),
            "warning_thresholds_percent": list(self.policy.warning_thresholds),
            "warnings_reached_percent": [
                threshold for threshold in self.policy.warning_thresholds
                if percentage >= threshold
            ],
            "hard_stop_percent": self.policy.hard_stop_percent,
            "reserve_percent": self.policy.reserve_percent,
            "nonessential_limit": self.policy.nonessential_limit,
            "remaining_application_budget": max(0, budget - used),
            "remaining_nonessential_budget": max(0, self.policy.nonessential_limit - used),
            "tracking_enabled": self.policy.tracking_enabled,
            "status": status,
        }

    @staticmethod
    def _validate_request(
        category: str, method: str, endpoint: str, purpose: str, essential: bool,
    ) -> None:
        if not isinstance(essential, bool):
            raise TypeError("essential must be a bool")
        if not isinstance(category, str) or not isinstance(method, str) \
                or not isinstance(endpoint, str) or not isinstance(purpose, str):
            raise TypeError("category, method, endpoint and purpose must be strings")
        if category not in _CATEGORIES:
            raise ValueError(f"category must be one of {sorted(_CATEGORIES)}")
        if method not in {"GET", "POST"}:
            raise ValueError("Kite data usage tracker permits GET and authentication POST only")
        if category == "authentication":
            if method != "POST" or endpoint != "/session/token":
                raise ValueError("authentication may only use POST /session/token")
        elif category == "recovery":
            if method != "GET" or endpoint != "/user/profile":
                raise ValueError("recovery may only use GET /user/profile")
        elif category == "instrument_master":
            if method != "GET" or endpoint != "/instruments":
                raise ValueError("instrument master may only use GET /instruments")
        elif category == "quote":
            if method != "GET" or endpoint not in {"/quote", "/quote/ohlc", "/quote/ltp"}:
                raise ValueError("quote category may only use documented GET quote endpoints")
        elif category == "historical":
            path = endpoint[len(_HISTORICAL_PREFIX):] if endpoint.startswith(
                _HISTORICAL_PREFIX
            ) else ""
            if method != "GET" or len(path.split("/")) != 2 or not all(path.split("/")):
                raise ValueError("historical category may only use GET historical-candle endpoints")
        if category in _ESSENTIAL_CATEGORIES and not essential:
            raise ValueError("authentication and recovery requests must be marked essential")
        if essential and category not in _ESSENTIAL_CATEGORIES:
            raise ValueError("only authentication and recovery requests may use the reserve")
        if method != method.upper() or not endpoint.startswith("/") \
                or len(endpoint) > 160 or any(char in endpoint for char in "?#") \
                or "://" in endpoint or "\n" in endpoint or "\r" in endpoint:
            raise ValueError("endpoint must be a relative path without query or fragment")
        if not purpose.strip() or len(purpose) > 160 \
                or "\n" in purpose or "\r" in purpose:
            raise ValueError("purpose must be a non-empty single-line description")

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Kite usage timestamps must be timezone-aware")
        return now.astimezone(timezone.utc)
