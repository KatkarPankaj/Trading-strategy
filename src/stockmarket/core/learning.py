"""Learning governance: research proposes, evidence gates, a human approves, live runs only approved configs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from enum import Enum
from math import sqrt
from statistics import mean, stdev
from typing import Any, Callable, Iterable, Mapping, Sequence


class ConfigStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    APPROVED = "APPROVED"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"
    REJECTED = "REJECTED"


class ApprovalError(Exception):
    pass


class ParametersMismatch(Exception):
    pass


def parameters_hash(parameters: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(parameters, sort_keys=True, separators=(",", ":"),
                                     default=str).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class LearningPolicy:
    min_trades: int = 100
    confidence_z: float = 1.96  # two-sided 95% under a normal approximation
    min_validation_days: int = 30
    blocking_warnings: tuple[str, ...] = ("SUSPICIOUSLY_HIGH_PERFORMANCE", "IN_SAMPLE_OUT_OF_SAMPLE_DEGRADATION",
                                          "UNSTABLE_ACROSS_FOLDS", "PARAMETER_SPIKE")

    def __post_init__(self) -> None:
        if self.min_trades < 30:
            raise ValueError(
                "min_trades below 30 makes the normal approximation unreliable")
        if self.confidence_z <= 0 or self.min_validation_days < 1:
            raise ValueError("invalid policy values")


@dataclass(frozen=True, slots=True)
class EvidenceAssessment:
    passed: bool
    reasons: tuple[str, ...]
    trades: int
    mean_pnl: float | None
    ci_low: float | None
    ci_high: float | None


def _interval(pnls: Sequence[float], z: float) -> tuple[float, float, float]:
    m = mean(pnls)
    half = z * stdev(pnls) / sqrt(len(pnls))
    return m, m - half, m + half


def assess_evidence(
    out_of_sample_pnls: Sequence[float],
    training_period: tuple[date, date],
    validation_period: tuple[date, date],
    warnings: Iterable[str] = (),
    policy: LearningPolicy = LearningPolicy(),
) -> EvidenceAssessment:
    """A configuration needs enough out-of-sample trades, a positive confidence bound, and clean separation from training."""
    reasons: list[str] = []
    n = len(out_of_sample_pnls)
    if validation_period[0] <= training_period[1]:
        reasons.append("VALIDATION_OVERLAPS_TRAINING")
    if (validation_period[1] - validation_period[0]).days + 1 < policy.min_validation_days:
        reasons.append("VALIDATION_TOO_SHORT")
    m = lo = hi = None
    if n < policy.min_trades:
        reasons.append(f"INSUFFICIENT_SAMPLE: {n} < {policy.min_trades}")
    else:
        m, lo, hi = _interval(out_of_sample_pnls, policy.confidence_z)
        if lo <= 0:
            reasons.append("CI_LOWER_BOUND_NOT_POSITIVE")
    for w in warnings:
        if any(w.startswith(b) for b in policy.blocking_warnings):
            reasons.append(f"BLOCKING_WARNING: {w.split(':', 1)[0]}")
    return EvidenceAssessment(not reasons, tuple(reasons), n, m, lo, hi)


@dataclass(frozen=True, slots=True)
class StrategyVersionInfo:
    strategy_version: str
    parameter_version: int
    parameters_hash: str


@dataclass(frozen=True, slots=True)
class LiveStrategyConfig:
    strategy_name: str
    strategy_version: str
    parameter_version: int
    parameters: Mapping[str, Any]
    parameters_hash: str
    validation_period: tuple[date, date]
    validation_summary: Mapping[str, Any]
    proposed_by: str
    created_at: datetime
    status: ConfigStatus = ConfigStatus.CANDIDATE
    approved_at: datetime | None = None
    approved_by: str | None = None


class StrategyConfigRegistry:
    """The only way a live strategy gets parameters. Nothing here changes parameters automatically."""

    def __init__(self, repository: Any = None, clock: Callable[[], datetime] | None = None) -> None:
        self._repo = repository
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._configs: dict[tuple[str, int], LiveStrategyConfig] = {}
        if repository is not None:
            for config in repository.load_all():
                self._configs[(config.strategy_name,
                               config.parameter_version)] = config

    def propose(
        self,
        strategy_name: str,
        strategy_version: str,
        parameters: Mapping[str, Any],
        *,
        evidence: EvidenceAssessment,
        validation_period: tuple[date, date],
        proposed_by: str,
        validation_summary: Mapping[str, Any] | None = None,
    ) -> LiveStrategyConfig:
        """Failed evidence is recorded as REJECTED (with reasons) rather than silently dropped."""
        if not proposed_by.strip():
            raise ApprovalError("proposed_by is required")
        version = 1 + \
            max((v for (n, v) in self._configs if n == strategy_name), default=0)
        summary = dict(validation_summary or {})
        summary["evidence"] = {"passed": evidence.passed, "reasons": list(evidence.reasons), "trades": evidence.trades,
                               "mean_pnl": evidence.mean_pnl, "ci_low": evidence.ci_low, "ci_high": evidence.ci_high}
        config = LiveStrategyConfig(
            strategy_name, strategy_version, version, dict(
                parameters), parameters_hash(parameters),
            validation_period, summary, proposed_by, self._clock(),
            ConfigStatus.CANDIDATE if evidence.passed else ConfigStatus.REJECTED)
        return self._store(config)

    def approve(self, strategy_name: str, parameter_version: int, approver: str) -> LiveStrategyConfig:
        config = self._get(strategy_name, parameter_version)
        if config.status is not ConfigStatus.CANDIDATE:
            raise ApprovalError(
                f"only a CANDIDATE can be approved (status is {config.status.value})")
        if not approver.strip():
            raise ApprovalError("approver is required")
        if approver == config.proposed_by:
            raise ApprovalError("the approver must differ from the proposer")
        return self._store(replace(config, status=ConfigStatus.APPROVED, approved_at=self._clock(),
                                   approved_by=approver))

    def activate(self, strategy_name: str, parameter_version: int) -> LiveStrategyConfig:
        config = self._get(strategy_name, parameter_version)
        if config.status is not ConfigStatus.APPROVED or config.approved_by is None or config.approved_at is None:
            raise ApprovalError(
                "only an APPROVED configuration can be activated")
        for (name, _), other in list(self._configs.items()):
            if name == strategy_name and other.status is ConfigStatus.ACTIVE:
                self._store(replace(other, status=ConfigStatus.RETIRED))
        return self._store(replace(config, status=ConfigStatus.ACTIVE))

    def active(self, strategy_name: str) -> LiveStrategyConfig | None:
        return next((c for (n, _), c in self._configs.items()
                     if n == strategy_name and c.status is ConfigStatus.ACTIVE), None)

    def version_info(self, strategy_name: str) -> StrategyVersionInfo | None:
        config = self.active(strategy_name)
        if config is None:
            return None
        return StrategyVersionInfo(config.strategy_version, config.parameter_version, config.parameters_hash)

    def check_live(self, strategy_name: str) -> str | None:
        """Reason a strategy may not trade live, or None when it has an active, approved configuration."""
        config = self.active(strategy_name)
        if config is None:
            return f"no active approved configuration for strategy {strategy_name!r}"
        if config.approved_by is None or config.approved_at is None:
            return f"configuration for {strategy_name!r} lacks approval details"
        return None

    def verify_parameters(self, strategy_name: str, running: Mapping[str, Any]) -> LiveStrategyConfig:
        """Raise unless the parameters in use are exactly the approved ones."""
        config = self.active(strategy_name)
        if config is None:
            raise ParametersMismatch(
                f"no active configuration for {strategy_name!r}")
        if parameters_hash(running) != config.parameters_hash:
            raise ParametersMismatch(f"running parameters for {strategy_name!r} differ from the approved version "
                                     f"{config.parameter_version}")
        return config

    def all(self) -> tuple[LiveStrategyConfig, ...]:
        return tuple(self._configs.values())

    def _get(self, name: str, version: int) -> LiveStrategyConfig:
        try:
            return self._configs[(name, version)]
        except KeyError:
            raise ApprovalError(
                f"unknown configuration {name!r} v{version}") from None

    def _store(self, config: LiveStrategyConfig) -> LiveStrategyConfig:
        self._configs[(config.strategy_name,
                       config.parameter_version)] = config
        if self._repo is not None:
            self._repo.save(config)
        return config


@dataclass(frozen=True, slots=True)
class LiveReview:
    outcome: str
    trades: int
    mean_pnl: float | None
    ci_high: float | None
    note: str = field(default="")


def review_live_performance(recent_pnls: Sequence[float], policy: LearningPolicy = LearningPolicy()) -> LiveReview:
    """Flags for human research review only; it never edits parameters, and small samples are ignored."""
    n = len(recent_pnls)
    if n < policy.min_trades:
        return LiveReview("INSUFFICIENT_SAMPLE", n, None, None,
                          f"{n} trades; need {policy.min_trades} before any conclusion")
    m, _, hi = _interval(recent_pnls, policy.confidence_z)
    if hi < 0:
        return LiveReview("RESEARCH_REVIEW_RECOMMENDED", n, m, hi,
                          "average trade is negative with statistical confidence; parameters were NOT changed")
    return LiveReview("NO_ACTION", n, m, hi)
