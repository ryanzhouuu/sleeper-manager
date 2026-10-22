"""Immutable roster, eligibility, and source evidence shared by live planning and replay."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from math import isfinite

from sleeper_manager.domain._planning_validation import (
    PlanningStateError,
    _require_aware,
    _require_text,
    _require_unique,
)
from sleeper_manager.domain.projection import ProjectionSnapshot


class PlanningGameStatus(StrEnum):
    SCHEDULED = "scheduled"
    ACTIVE = "active"
    FINAL = "final"
    POSTPONED = "postponed"
    CANCELED = "canceled"
    UNKNOWN = "unknown"


class PlanningQuality(StrEnum):
    EXACT = "exact"
    BEST_KNOWN_CONSTRAINTS_ORACLE = "best_known_constraints_oracle"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class PlanningReasonCode(StrEnum):
    UNSUPPORTED_SCORING = "unsupported_scoring"
    LEAGUE_CONFIGURATION_CHANGED = "league_configuration_changed"
    MISSING_ROSTER_SNAPSHOT = "missing_roster_snapshot"
    ROSTER_STATE_MISMATCH = "roster_state_mismatch"
    MISSING_GAME_SCHEDULE = "missing_game_schedule"
    MISSING_GAME_RESULT = "missing_game_result"
    MISSING_NBA_TEAM_HISTORY = "missing_nba_team_history"
    AMBIGUOUS_GAME_TIME = "ambiguous_game_time"
    UNRESOLVED_PLAYER_IDENTITY = "unresolved_player_identity"
    MISSING_PROJECTION = "missing_projection"
    PROJECTION_AFTER_DECISION = "projection_after_decision"
    MISSING_MEMBERSHIP = "missing_membership"
    AMBIGUOUS_ELIGIBILITY = "ambiguous_eligibility"
    STALE_SLEEPER_STATE = "stale_sleeper_state"
    STALE_NBA_STATE = "stale_nba_state"
    INJURY_DATA_UNAVAILABLE = "injury_data_unavailable"
    ACKNOWLEDGEMENT_CONFLICT = "acknowledgement_conflict"
    AMBIGUOUS_EVENT_ORDER = "ambiguous_event_order"
    DEADLINE_ELAPSED = "deadline_elapsed"
    SEARCH_LIMIT = "search_limit"
    LOW_CONFIDENCE = "low_confidence"
    RECOVERY_WINDOW_CLOSED = "recovery_window_closed"
    MISSING_TIPOFF_EVIDENCE = "missing_tipoff_evidence"


@dataclass(frozen=True, slots=True)
class SourceLineage:
    source: str
    version: str
    available_as_of: datetime
    retrieved_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_text(self.source, "Source name")
        _require_text(self.version, "Source version")
        _require_aware(self.available_as_of, "Source availability")
        if self.retrieved_at is not None:
            _require_aware(self.retrieved_at, "Source retrieval")
            if self.retrieved_at < self.available_as_of:
                raise PlanningStateError("Source retrieval cannot precede source availability")


@dataclass(frozen=True, slots=True)
class FreshnessSummary:
    sources: tuple[SourceLineage, ...] = ()

    def __post_init__(self) -> None:
        _require_unique(
            (source.source for source in self.sources),
            "freshness sources",
        )


@dataclass(frozen=True, slots=True)
class StarterSlot:
    index: int
    position: str

    def __post_init__(self) -> None:
        if self.index < 0:
            raise PlanningStateError("Starter slot indices must be non-negative")
        _require_text(self.position, "Starter slot position")
        object.__setattr__(self, "position", self.position.strip().upper())


@dataclass(frozen=True, slots=True)
class ObservedStarter:
    slot_index: int
    player_id: str
    eligible_positions: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.slot_index < 0:
            raise PlanningStateError("Observed starter slot indices must be non-negative")
        _require_text(self.player_id, "Observed starter player ID")
        positions = tuple(
            sorted({position.strip().upper() for position in self.eligible_positions})
        )
        if any(not position for position in positions):
            raise PlanningStateError("Observed starter positions must be non-empty")
        object.__setattr__(self, "eligible_positions", positions)


@dataclass(frozen=True, slots=True)
class GameOpportunity:
    sleeper_player_id: str
    provider_player_id: str | None
    game_id: str
    scheduled_start: datetime
    status: PlanningGameStatus
    roster_id: int
    membership_segment: str | None
    eligible_slot_indices: tuple[int, ...]
    eligible_positions: tuple[str, ...]
    rostered_at_tipoff: bool | None
    availability_status: str
    availability_evidence_at: datetime | None
    projection: ProjectionSnapshot | None
    missing_projection_reason: PlanningReasonCode | None
    completed_fantasy_score: float | None
    finalized_at: datetime | None
    source_lineage: tuple[SourceLineage, ...] = ()
    data_quality: str = "complete"

    def __post_init__(self) -> None:
        _require_text(self.sleeper_player_id, "Sleeper player ID")
        if self.provider_player_id is not None:
            _require_text(self.provider_player_id, "Provider player ID")
        _require_text(self.game_id, "Game ID")
        _require_aware(self.scheduled_start, "Game scheduled start")
        if self.roster_id <= 0:
            raise PlanningStateError("Opportunity roster IDs must be positive")
        if len(set(self.eligible_slot_indices)) != len(self.eligible_slot_indices):
            raise PlanningStateError("Opportunity slot indices must be unique")
        if any(index < 0 for index in self.eligible_slot_indices):
            raise PlanningStateError("Opportunity slot indices must be non-negative")
        positions = tuple(
            sorted({position.strip().upper() for position in self.eligible_positions})
        )
        if any(not position for position in positions):
            raise PlanningStateError("Opportunity positions must be non-empty")
        object.__setattr__(self, "eligible_positions", positions)
        _require_text(self.availability_status, "Availability status")
        if self.availability_evidence_at is not None:
            _require_aware(self.availability_evidence_at, "Availability evidence")
        if self.projection is None and self.missing_projection_reason is None:
            raise PlanningStateError("Missing projections require an explicit reason")
        if self.projection is not None and self.missing_projection_reason is not None:
            raise PlanningStateError("Projection and missing-projection reason are exclusive")
        if self.projection is not None:
            if self.projection.player_id != self.sleeper_player_id:
                raise PlanningStateError("Projection player does not match opportunity player")
            if self.projection.game_id != self.game_id:
                raise PlanningStateError("Projection game does not match opportunity game")
        if self.completed_fantasy_score is not None:
            if not isfinite(self.completed_fantasy_score):
                raise PlanningStateError("Completed fantasy scores must be finite")
            if self.status is not PlanningGameStatus.FINAL or self.finalized_at is None:
                raise PlanningStateError("Completed scores require a finalized game")
        if self.finalized_at is not None:
            _require_aware(self.finalized_at, "Game finalization")
            if self.status is not PlanningGameStatus.FINAL:
                raise PlanningStateError("Finalization time requires a final game")
        _require_text(self.data_quality, "Opportunity data quality")

    def validate_at(self, decision_time: datetime) -> None:
        _require_aware(decision_time, "Decision time")
        if (
            self.availability_evidence_at is not None
            and self.availability_evidence_at > decision_time
        ):
            raise PlanningStateError("Availability evidence is newer than the decision time")
        if self.projection is not None:
            _require_aware(self.projection.available_as_of, "Projection availability")
            if self.projection.available_as_of > decision_time:
                raise PlanningStateError("Projection is newer than the decision time")
        if self.finalized_at is not None and self.finalized_at > decision_time:
            raise PlanningStateError("Game finalization is newer than the decision time")
        for lineage in self.source_lineage:
            _validate_lineage_at(lineage, decision_time)


@dataclass(frozen=True, slots=True)
class FixedSlot:
    slot_index: int
    slot_position: str
    player_id: str
    game_id: str
    accepted_fantasy_score: float
    decision_time: datetime
    decision_id: str
    provenance: str

    def __post_init__(self) -> None:
        if self.slot_index < 0:
            raise PlanningStateError("Fixed slot indices must be non-negative")
        _require_text(self.slot_position, "Fixed slot position")
        _require_text(self.player_id, "Fixed player ID")
        _require_text(self.game_id, "Fixed game ID")
        if not isfinite(self.accepted_fantasy_score):
            raise PlanningStateError("Fixed scores must be finite")
        _require_aware(self.decision_time, "Fixed decision time")
        _require_text(self.decision_id, "Fixed decision ID")
        _require_text(self.provenance, "Fixed provenance")
        object.__setattr__(self, "slot_position", self.slot_position.strip().upper())


@dataclass(frozen=True, slots=True)
class PassedOpportunity:
    player_id: str
    game_id: str
    decision_time: datetime
    decision_id: str
    provenance: str

    def __post_init__(self) -> None:
        _require_text(self.player_id, "Passed player ID")
        _require_text(self.game_id, "Passed game ID")
        _require_aware(self.decision_time, "Passed decision time")
        _require_text(self.decision_id, "Passed decision ID")
        _require_text(self.provenance, "Passed provenance")


class AcknowledgedAction(StrEnum):
    LOCK = "lock"
    PASS = "pass"


@dataclass(frozen=True, slots=True)
class AcknowledgedDecisionEvidence:
    """Provider-neutral manager decision that later planning must treat as fixed."""

    decision_id: str
    player_id: str
    game_id: str
    action: AcknowledgedAction
    decided_at: datetime
    provenance: str
    slot_index: int | None = None
    slot_position: str | None = None
    accepted_fantasy_score: float | None = None
    reconciled: bool = True

    def __post_init__(self) -> None:
        _require_text(self.decision_id, "Acknowledged decision ID")
        _require_text(self.player_id, "Acknowledged player ID")
        _require_text(self.game_id, "Acknowledged game ID")
        _require_aware(self.decided_at, "Acknowledged decision time")
        _require_text(self.provenance, "Acknowledged provenance")
        if self.action is AcknowledgedAction.LOCK:
            if self.slot_index is None or self.slot_position is None:
                if self.reconciled:
                    raise PlanningStateError("Acknowledged locks require slot evidence")
            elif self.slot_index < 0:
                raise PlanningStateError("Acknowledged lock slot indices must be non-negative")
            if self.accepted_fantasy_score is None or not isfinite(self.accepted_fantasy_score):
                if self.reconciled:
                    raise PlanningStateError("Acknowledged locks require a finite accepted score")
            return
        if (
            self.slot_index is not None
            or self.slot_position is not None
            or self.accepted_fantasy_score is not None
        ):
            raise PlanningStateError("Acknowledged passes cannot carry slot evidence")


class PlanStatus(StrEnum):
    NO_ACTION = "no_action"
    ACTION_REQUIRED = "action_required"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class PlanConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True, slots=True)
class PlannedAssignment:
    slot_index: int
    slot_position: str
    player_id: str | None

    def __post_init__(self) -> None:
        if self.slot_index < 0:
            raise PlanningStateError("Planned assignment slot indices must be non-negative")
        _require_text(self.slot_position, "Planned assignment slot position")
        object.__setattr__(self, "slot_position", self.slot_position.strip().upper())
        if self.player_id is not None:
            _require_text(self.player_id, "Planned assignment player ID")


@dataclass(frozen=True, slots=True)
class PlanDistributionSummary:
    scenario_count: int
    seed: int
    approximation: str
    perfect_information_bound: float
    postgame_scenario_count: int | None = None
    minimum_lock_in_confidence: float | None = None

    def __post_init__(self) -> None:
        if self.scenario_count <= 0:
            raise PlanningStateError("Plan scenario counts must be positive")
        _require_text(self.approximation, "Plan approximation")
        if not isfinite(self.perfect_information_bound):
            raise PlanningStateError("Plan bounds must be finite")
        if self.postgame_scenario_count is not None and self.postgame_scenario_count <= 0:
            raise PlanningStateError("Postgame scenario counts must be positive")
        confidence = self.minimum_lock_in_confidence
        if confidence is not None and (not isfinite(confidence) or not 0 <= confidence <= 1):
            raise PlanningStateError("Postgame confidence must be between zero and one")


def _validate_lineage_at(lineage: SourceLineage, decision_time: datetime) -> None:
    if lineage.available_as_of > decision_time:
        raise PlanningStateError("Source evidence is newer than the decision time")
    if lineage.retrieved_at is not None and lineage.retrieved_at > decision_time:
        raise PlanningStateError("Source retrieval is newer than the decision time")
