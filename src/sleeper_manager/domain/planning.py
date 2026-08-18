from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from math import isfinite

from sleeper_manager.domain.eligibility import eligible_for_slot
from sleeper_manager.domain.projection import ProjectionSnapshot


class PlanningStateError(ValueError):
    pass


class PlanningGameStatus(StrEnum):
    SCHEDULED = "scheduled"
    ACTIVE = "active"
    FINAL = "final"
    POSTPONED = "postponed"
    CANCELED = "canceled"


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
    AMBIGUOUS_GAME_TIME = "ambiguous_game_time"
    UNRESOLVED_PLAYER_IDENTITY = "unresolved_player_identity"
    MISSING_PROJECTION = "missing_projection"
    PROJECTION_AFTER_DECISION = "projection_after_decision"
    MISSING_MEMBERSHIP = "missing_membership"
    AMBIGUOUS_ELIGIBILITY = "ambiguous_eligibility"
    STALE_SLEEPER_STATE = "stale_sleeper_state"
    STALE_NBA_STATE = "stale_nba_state"
    ACKNOWLEDGEMENT_CONFLICT = "acknowledgement_conflict"
    AMBIGUOUS_EVENT_ORDER = "ambiguous_event_order"
    DEADLINE_ELAPSED = "deadline_elapsed"


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


@dataclass(frozen=True, slots=True)
class TeamWeekState:
    league_id: str
    season: str
    week: int
    roster_id: int
    decision_time: datetime
    starter_slots: tuple[StarterSlot, ...]
    roster_player_ids: tuple[str, ...]
    observed_starters: tuple[ObservedStarter, ...]
    opportunities: tuple[GameOpportunity, ...]
    fixed_slots: tuple[FixedSlot, ...] = ()
    passed_opportunities: tuple[PassedOpportunity, ...] = ()
    scoring_policy_version: str = "unknown"
    league_configuration_version: str = "unknown"
    manager_policy_version: str = "unknown"
    projection_model_version: str = "unknown"
    input_version: str = "unknown"
    freshness: FreshnessSummary = field(default_factory=FreshnessSummary)
    eligibility_quality: PlanningQuality = PlanningQuality.UNKNOWN
    exclusions: tuple[PlanningReasonCode, ...] = ()
    warnings: tuple[PlanningReasonCode, ...] = ()
    blocking_reasons: tuple[PlanningReasonCode, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.league_id, "League ID")
        _require_text(self.season, "Season")
        if self.week <= 0:
            raise PlanningStateError("Fantasy weeks must be positive")
        if self.roster_id <= 0:
            raise PlanningStateError("Roster IDs must be positive")
        _require_aware(self.decision_time, "Decision time")
        for name, value in (
            ("scoring policy version", self.scoring_policy_version),
            ("league configuration version", self.league_configuration_version),
            ("manager policy version", self.manager_policy_version),
            ("projection model version", self.projection_model_version),
            ("input version", self.input_version),
        ):
            _require_text(value, name)

        _require_unique((slot.index for slot in self.starter_slots), "starter slots")
        _require_unique(self.roster_player_ids, "roster players")
        slot_by_index = {slot.index: slot for slot in self.starter_slots}
        roster_players = set(self.roster_player_ids)

        observed_slots: set[int] = set()
        observed_players: set[str] = set()
        for observed in self.observed_starters:
            if observed.slot_index in observed_slots:
                raise PlanningStateError("Observed assignments cannot reuse a slot")
            if observed.player_id in observed_players:
                raise PlanningStateError("Observed assignments cannot reuse a player")
            slot = slot_by_index.get(observed.slot_index)
            if slot is None:
                raise PlanningStateError("Observed assignment references an unknown slot")
            if observed.player_id not in roster_players:
                raise PlanningStateError("Observed starter is not on the current roster")
            if not observed.eligible_positions:
                if PlanningReasonCode.AMBIGUOUS_ELIGIBILITY not in self.blocking_reasons:
                    raise PlanningStateError("Unknown observed eligibility must block planning")
            elif not eligible_for_slot(observed.eligible_positions, slot.position):
                raise PlanningStateError("Observed starter is not eligible for its slot")
            observed_slots.add(observed.slot_index)
            observed_players.add(observed.player_id)

        opportunity_by_key: dict[tuple[str, str], GameOpportunity] = {}
        game_facts: dict[str, tuple[datetime, PlanningGameStatus]] = {}
        for opportunity in self.opportunities:
            if opportunity.roster_id != self.roster_id:
                raise PlanningStateError("Opportunity belongs to a different roster")
            opportunity.validate_at(self.decision_time)
            key = opportunity.sleeper_player_id, opportunity.game_id
            if key in opportunity_by_key:
                raise PlanningStateError("Opportunities cannot duplicate a player-game")
            opportunity_by_key[key] = opportunity
            facts = opportunity.scheduled_start, opportunity.status
            previous = game_facts.get(opportunity.game_id)
            if previous is not None and previous != facts:
                raise PlanningStateError("A game ID cannot describe conflicting game facts")
            game_facts[opportunity.game_id] = facts

        for source in self.freshness.sources:
            _validate_lineage_at(source, self.decision_time)

        fixed_slots: set[int] = set()
        fixed_players: set[str] = set()
        for fixed in self.fixed_slots:
            if fixed.slot_index in fixed_slots:
                raise PlanningStateError("Fixed records cannot reuse a slot")
            if fixed.player_id in fixed_players:
                raise PlanningStateError("Fixed records cannot reuse a player")
            if fixed.decision_time > self.decision_time:
                raise PlanningStateError("Fixed decision is newer than the state")
            slot = slot_by_index.get(fixed.slot_index)
            if slot is None or fixed.slot_position != slot.position:
                raise PlanningStateError("Fixed record references an invalid slot")
            fixed_opportunity = opportunity_by_key.get((fixed.player_id, fixed.game_id))
            if fixed_opportunity is None:
                raise PlanningStateError("Fixed record references an unknown opportunity")
            if fixed.slot_index not in fixed_opportunity.eligible_slot_indices:
                raise PlanningStateError("Fixed player was not eligible for its slot")
            if not eligible_for_slot(fixed_opportunity.eligible_positions, slot.position):
                raise PlanningStateError("Fixed player positions do not fit its slot")
            if (
                fixed_opportunity.completed_fantasy_score is not None
                and abs(fixed_opportunity.completed_fantasy_score - fixed.accepted_fantasy_score)
                > 1e-6
            ):
                raise PlanningStateError("Fixed score differs from the completed opportunity")
            fixed_slots.add(fixed.slot_index)
            fixed_players.add(fixed.player_id)

        passed_keys: set[tuple[str, str]] = set()
        for passed in self.passed_opportunities:
            key = passed.player_id, passed.game_id
            if key in passed_keys:
                raise PlanningStateError("Passed records cannot duplicate an opportunity")
            if passed.decision_time > self.decision_time:
                raise PlanningStateError("Passed decision is newer than the state")
            if key not in opportunity_by_key:
                raise PlanningStateError("Passed record references an unknown opportunity")
            if key in {(fixed.player_id, fixed.game_id) for fixed in self.fixed_slots}:
                raise PlanningStateError("An opportunity cannot be both fixed and passed")
            passed_keys.add(key)

    @property
    def open_slot_indices(self) -> tuple[int, ...]:
        fixed = {record.slot_index for record in self.fixed_slots}
        return tuple(slot.index for slot in self.starter_slots if slot.index not in fixed)

    @property
    def is_blocked(self) -> bool:
        return bool(self.blocking_reasons)

    @property
    def completed_opportunities(self) -> tuple[GameOpportunity, ...]:
        return tuple(
            opportunity
            for opportunity in self.opportunities
            if opportunity.status is PlanningGameStatus.FINAL
            and opportunity.completed_fantasy_score is not None
            and opportunity.finalized_at is not None
            and opportunity.finalized_at <= self.decision_time
        )

    @property
    def active_opportunities(self) -> tuple[GameOpportunity, ...]:
        return tuple(
            opportunity
            for opportunity in self.opportunities
            if opportunity.status is PlanningGameStatus.ACTIVE
        )

    @property
    def remaining_opportunities(self) -> tuple[GameOpportunity, ...]:
        return tuple(
            opportunity
            for opportunity in self.opportunities
            if opportunity.status is PlanningGameStatus.SCHEDULED
            or opportunity.status is PlanningGameStatus.ACTIVE
        )

    @property
    def unpassed_opportunities(self) -> tuple[GameOpportunity, ...]:
        passed = {(record.player_id, record.game_id) for record in self.passed_opportunities}
        return tuple(
            opportunity
            for opportunity in self.opportunities
            if (opportunity.sleeper_player_id, opportunity.game_id) not in passed
        )


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None:
        raise PlanningStateError(f"{label} must be timezone-aware")


def _require_text(value: str, label: str) -> None:
    if not value.strip():
        raise PlanningStateError(f"{label} must be non-empty")


def _require_unique(values: Iterable[str] | Iterable[int], label: str) -> None:
    materialized: tuple[str | int, ...] = tuple(values)
    if len(set(materialized)) != len(materialized):
        raise PlanningStateError(f"{label} must be unique")
    for value in materialized:
        if isinstance(value, str):
            _require_text(value, label)


def _validate_lineage_at(lineage: SourceLineage, decision_time: datetime) -> None:
    if lineage.available_as_of > decision_time:
        raise PlanningStateError("Source evidence is newer than the decision time")
    if lineage.retrieved_at is not None and lineage.retrieved_at > decision_time:
        raise PlanningStateError("Source retrieval is newer than the decision time")


__all__ = (
    "FixedSlot",
    "FreshnessSummary",
    "GameOpportunity",
    "ObservedStarter",
    "PassedOpportunity",
    "PlanningGameStatus",
    "PlanningQuality",
    "PlanningReasonCode",
    "PlanningStateError",
    "SourceLineage",
    "StarterSlot",
    "TeamWeekState",
)
