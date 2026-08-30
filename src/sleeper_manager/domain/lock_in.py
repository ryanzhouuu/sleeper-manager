"""Production-neutral Lock-In decision and trace contracts.

Decision policies, live workflows, and historical replay share these immutable
records. Backtesting may attach chronological ordering evidence, but it does not
own the decision shape consumed by later application layers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from math import isfinite


class LockInContractError(ValueError):
    """Report an invalid decision or trace before it enters application state."""


class LockInDecisionKind(StrEnum):
    """Identify the actionable outcomes supported by the current policy boundary."""

    LOCK = "lock"
    PASS = "pass"


class LockInEvaluationKind(StrEnum):
    """Identify actionable, deferred, and terminal live evaluation outcomes."""

    LOCK = "lock"
    PASS = "pass"
    WAIT = "wait"
    UNAVAILABLE = "unavailable"
    AUTOMATIC_FINAL = "automatic_final"


@dataclass(frozen=True, slots=True)
class LockInDecision:
    """Capture one policy result with the evidence required to audit it later."""

    decision_time: datetime
    kind: LockInDecisionKind
    player_id: str
    game_id: str
    slot_index: int | None
    expected_terminal_score: float
    counterfactual_value: float
    information_version: str
    reason: str

    def __post_init__(self) -> None:
        """Reject decisions that cannot be applied or reproduced safely."""

        if self.decision_time.tzinfo is None:
            raise LockInContractError("Lock-In decision times must be timezone-aware")
        if not isinstance(self.kind, LockInDecisionKind):
            raise LockInContractError("Lock-In decision kind must be Lock or Pass")
        _require_text(self.player_id, "player ID")
        _require_text(self.game_id, "game ID")
        _require_text(self.information_version, "information version")
        _require_text(self.reason, "decision reason")
        if not isfinite(self.expected_terminal_score) or not isfinite(self.counterfactual_value):
            raise LockInContractError("Lock-In decision values must be finite")
        if self.kind is LockInDecisionKind.LOCK:
            if self.slot_index is None or self.slot_index < 0:
                raise LockInContractError("Lock decisions require a non-negative slot index")
        elif self.slot_index is not None:
            raise LockInContractError("Pass decisions cannot carry a slot index")


@dataclass(frozen=True, slots=True)
class LockInDecisionTrace:
    """Attach stable chronological ordering identity to one exact policy decision."""

    decision: LockInDecision
    event_id: str
    batch_id: str
    candidate_id: str
    evaluation_order: int

    def __post_init__(self) -> None:
        """Reject trace records whose identity cannot be reproduced deterministically."""

        _require_text(self.event_id, "event ID")
        _require_text(self.batch_id, "batch ID")
        _require_text(self.candidate_id, "candidate ID")
        if self.evaluation_order <= 0:
            raise LockInContractError("Lock-In evaluation order must be positive")


@dataclass(frozen=True, slots=True)
class LockInEvaluation:
    """Describe one auditable live result without inventing a Lock/Pass decision."""

    decision_time: datetime
    kind: LockInEvaluationKind
    player_id: str
    game_id: str
    deadline: datetime
    information_version: str
    manager_policy_version: str
    reason_codes: tuple[str, ...]
    trace: tuple[tuple[str, str], ...]
    observed_score: float | None = None
    alternative_expected_score: float | None = None
    alternative_percentiles: tuple[tuple[int, float], ...] = ()
    confidence: float | None = None
    decision: LockInDecision | None = None

    def __post_init__(self) -> None:
        """Reject evaluations whose outcome and supporting evidence disagree."""

        _require_aware(self.decision_time, "evaluation time")
        _require_aware(self.deadline, "evaluation deadline")
        _require_text(self.player_id, "player ID")
        _require_text(self.game_id, "game ID")
        _require_text(self.information_version, "information version")
        _require_text(self.manager_policy_version, "manager policy version")
        if not self.reason_codes or any(not item.strip() for item in self.reason_codes):
            raise LockInContractError("Lock-In evaluations require reason codes")
        trace_keys = tuple(key for key, _ in self.trace)
        if not self.trace or len(set(trace_keys)) != len(trace_keys):
            raise LockInContractError("Lock-In evaluation trace requires unique evidence keys")
        if any(not key.strip() or not value.strip() for key, value in self.trace):
            raise LockInContractError("Lock-In evaluation trace values must be non-empty")
        if self.observed_score is not None and not isfinite(self.observed_score):
            raise LockInContractError("Observed Lock-In score must be finite")
        if self.alternative_expected_score is not None and not isfinite(
            self.alternative_expected_score
        ):
            raise LockInContractError("Alternative Lock-In score must be finite")
        if self.confidence is not None and (
            not isfinite(self.confidence) or not 0 <= self.confidence <= 1
        ):
            raise LockInContractError("Lock-In confidence must be between zero and one")
        _validate_percentiles(self.alternative_percentiles)
        actionable = self.kind in {LockInEvaluationKind.LOCK, LockInEvaluationKind.PASS}
        if actionable != (self.decision is not None):
            raise LockInContractError("Only actionable evaluations carry a Lock-In decision")
        if self.decision is not None:
            if self.decision.player_id != self.player_id or self.decision.game_id != self.game_id:
                raise LockInContractError("Evaluation and decision identities must match")
            if self.decision.kind.value != self.kind.value:
                raise LockInContractError("Evaluation and decision actions must match")
            if self.observed_score is None or self.confidence is None:
                raise LockInContractError("Actionable evaluations require score and confidence")


def _require_text(value: str, label: str) -> None:
    """Require stable nonblank identifiers and human-readable evidence."""

    if not value.strip():
        raise LockInContractError(f"Lock-In {label} must be non-empty")


def _require_aware(value: datetime, label: str) -> None:
    """Require timezone-aware timestamps at live decision boundaries."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise LockInContractError(f"Lock-In {label} must be timezone-aware")


def _validate_percentiles(values: tuple[tuple[int, float], ...]) -> None:
    """Require ordered finite percentile evidence without duplicate ranks."""

    ranks = tuple(rank for rank, _ in values)
    if ranks != tuple(sorted(set(ranks))) or any(not 0 <= rank <= 100 for rank in ranks):
        raise LockInContractError("Lock-In percentiles must have unique ordered ranks")
    if any(not isfinite(value) for _, value in values):
        raise LockInContractError("Lock-In percentile values must be finite")


__all__ = (
    "LockInContractError",
    "LockInDecision",
    "LockInDecisionKind",
    "LockInDecisionTrace",
    "LockInEvaluation",
    "LockInEvaluationKind",
)
