"""Shared types for historical Lock-In diagnostic execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sleeper_manager.backtesting.replay.inputs.models import HistoricalTeamWeekInput
from sleeper_manager.backtesting.replay.models import TeamWeekComparison, TeamWeekReplayResult
from sleeper_manager.decisions.lock_in import LockInPolicyConfig
from sleeper_manager.domain.lock_in import LockInDecision
from sleeper_manager.domain.planning import PlanningReasonCode

DIAGNOSTIC_ADAPTER_VERSION = "lock-in-diagnostic-adapter-v2"
DECISION_CRITICAL_EXCLUSIONS = frozenset(
    {
        PlanningReasonCode.UNSUPPORTED_SCORING,
        PlanningReasonCode.MISSING_GAME_SCHEDULE,
        PlanningReasonCode.UNRESOLVED_PLAYER_IDENTITY,
        PlanningReasonCode.MISSING_PROJECTION,
        PlanningReasonCode.MISSING_MEMBERSHIP,
        PlanningReasonCode.ROSTER_STATE_MISMATCH,
        PlanningReasonCode.MISSING_ROSTER_SNAPSHOT,
    }
)


class LockInDiagnosticError(ValueError):
    """Raised when diagnostic admission or execution fails closed."""


@dataclass(frozen=True, slots=True)
class LockInDiagnosticRequest:
    """Configure one deterministic diagnostic over a persisted team-week."""

    team_week: HistoricalTeamWeekInput
    policy_config: LockInPolicyConfig = field(default_factory=LockInPolicyConfig)
    planning_lead_time: timedelta = timedelta(minutes=1)
    planning_cutoffs: tuple[datetime, ...] | None = None
    policy_name: str = "score_maximizing_lock_in"

    def __post_init__(self) -> None:
        """Reject ambiguous timing or policy configuration before replay."""

        if self.planning_lead_time < timedelta(0):
            raise LockInDiagnosticError("Planning lead time cannot be negative")
        if not self.policy_name.strip():
            raise LockInDiagnosticError("Diagnostic policy name must be non-empty")
        if self.planning_cutoffs is not None:
            for cutoff in self.planning_cutoffs:
                if cutoff.tzinfo is None:
                    raise LockInDiagnosticError("Planning cutoffs must be timezone-aware")


@dataclass(frozen=True, slots=True)
class AdmissionCheck:
    """Record one named diagnostic-admission invariant and its evidence."""

    code: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    """Summarize whether every diagnostic-admission invariant passed."""

    admitted: bool
    checks: tuple[AdmissionCheck, ...]


@dataclass(frozen=True, slots=True)
class DiagnosticDeferral:
    """Explain why a visible candidate could not yet be evaluated."""

    candidate_id: str
    sleeper_id: str
    game_id: str
    cutoff: datetime
    event_id: str
    batch_id: str
    missing_opportunity_keys: tuple[str, ...]
    reason: str
    terminal: bool = False


@dataclass(frozen=True, slots=True)
class DiagnosticPolicyTrace:
    """Attach stable event and ordering identity to one policy decision."""

    decision: LockInDecision
    decision_time: datetime
    event_id: str
    batch_id: str
    candidate_id: str
    evaluation_order: int


@dataclass(frozen=True, slots=True)
class CandidateBatch:
    """Group candidates sharing the same outcome-finalization timestamp."""

    batch_id: str
    finalized_at: datetime
    candidate_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AutomaticSlotAssignment:
    """Record the legal final-game assignment for one unlocked starter."""

    slot_index: int
    slot_position: str
    sleeper_id: str
    game_id: str
    score: float


@dataclass(frozen=True, slots=True)
class OracleFeasibilityCheck:
    """Describe whether an oracle selection was temporally lockable."""

    sleeper_id: str
    game_id: str
    slot_index: int
    kind: str
    feasible: bool
    detail: str


@dataclass(frozen=True, slots=True)
class LockInDiagnosticExecution:
    """Collect admission, replay, comparison, and trace evidence for one run."""

    status: str
    admission: AdmissionResult
    model_result: TeamWeekReplayResult | None
    oracle_result: TeamWeekReplayResult | None
    comparison: TeamWeekComparison | None
    policy_traces: tuple[DiagnosticPolicyTrace, ...]
    deferrals: tuple[DiagnosticDeferral, ...]
    batches: tuple[CandidateBatch, ...]
    evaluation_order: tuple[str, ...]
    automatic_assignments: tuple[AutomaticSlotAssignment, ...]
    oracle_feasibility: tuple[OracleFeasibilityCheck, ...]


__all__ = (
    "AdmissionCheck",
    "AdmissionResult",
    "AutomaticSlotAssignment",
    "CandidateBatch",
    "DECISION_CRITICAL_EXCLUSIONS",
    "DIAGNOSTIC_ADAPTER_VERSION",
    "DiagnosticDeferral",
    "DiagnosticPolicyTrace",
    "LockInDiagnosticError",
    "LockInDiagnosticExecution",
    "LockInDiagnosticRequest",
    "OracleFeasibilityCheck",
)
