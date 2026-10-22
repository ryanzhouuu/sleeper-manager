"""Validate one coherent team-week snapshot before any decision policy runs."""

from dataclasses import dataclass, field
from datetime import datetime

from sleeper_manager.domain._planning_validation import (
    PlanningStateError,
    _require_aware,
    _require_text,
    _require_unique,
)
from sleeper_manager.domain.eligibility import eligible_for_slot
from sleeper_manager.domain.planning_actions import RecoveryObligation
from sleeper_manager.domain.planning_evidence import (
    FixedSlot,
    FreshnessSummary,
    GameOpportunity,
    ObservedStarter,
    PassedOpportunity,
    PlanningGameStatus,
    PlanningQuality,
    PlanningReasonCode,
    StarterSlot,
    _validate_lineage_at,
)


@dataclass(frozen=True, slots=True)
class TeamWeekState:
    """Observed roster evidence; unknown bench capacity never implies a vacancy."""

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
    bench_capacity: int | None = None
    reserve_player_ids: tuple[str, ...] = ()
    week_ends_at: datetime | None = None
    recovery_obligations: tuple[RecoveryObligation, ...] = ()
    recovery_failures: tuple[str, ...] = ()
    recovery_forfeitures: tuple[tuple[str, str], ...] = ()
    roster_eligibility: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def __post_init__(self) -> None:
        authorized = {
            key for recovery in self.recovery_obligations for key in recovery.added_player_games
        }
        if not set(self.recovery_forfeitures) <= authorized:
            raise PlanningStateError("Forfeitures require a named recovery obligation")
        if {player for player, _ in self.recovery_forfeitures} & {
            row.player_id for row in self.fixed_slots
        }:
            raise PlanningStateError("Recovery cannot forfeit accepted locks")
        if self.week_ends_at is not None:
            _require_aware(self.week_ends_at, "Fantasy-week end")
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
        if self.bench_capacity is not None and self.bench_capacity < 0:
            raise PlanningStateError("Bench capacity must be non-negative")
        _require_unique(self.reserve_player_ids, "reserve players")
        if not set(self.reserve_player_ids) <= roster_players:
            raise PlanningStateError("A reserve player is not on the current roster")

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
            if observed.player_id in self.reserve_player_ids:
                raise PlanningStateError("A reserve player cannot occupy a starting slot")
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
