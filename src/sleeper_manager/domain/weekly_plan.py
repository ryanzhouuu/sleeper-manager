"""Complete executable lineup targets and stable recommendation identities."""

from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from math import isfinite

from sleeper_manager.domain._planning_validation import (
    PlanningStateError,
    _require_aware,
    _require_text,
)
from sleeper_manager.domain.lineup_moves import LineupMove, apply_lineup_moves
from sleeper_manager.domain.lock_in import LockInDecisionKind
from sleeper_manager.domain.planning_actions import PlannedScoreAction, RecoveryObligation
from sleeper_manager.domain.planning_evidence import (
    FixedSlot,
    FreshnessSummary,
    PassedOpportunity,
    PlanConfidence,
    PlanDistributionSummary,
    PlannedAssignment,
    PlanningReasonCode,
    PlanStatus,
)


@dataclass(frozen=True, slots=True)
class WeeklyPlan:
    league_id: str
    season: str
    week: int
    roster_id: int
    decision_time: datetime
    status: PlanStatus
    observed_assignments: tuple[PlannedAssignment, ...]
    desired_assignments: tuple[PlannedAssignment, ...]
    moves: tuple[LineupMove, ...]
    confidence: PlanConfidence
    planner_version: str
    manager_policy_version: str
    scoring_policy_version: str = "unknown"
    league_configuration_version: str = "unknown"
    projection_model_version: str = "unknown"
    input_version: str = "unknown"
    expected_terminal_score: float | None = None
    best_alternative_score: float | None = None
    observed_terminal_score: float | None = None
    decision_margin: float | None = None
    distribution_summary: PlanDistributionSummary | None = None
    fixed_slots: tuple[FixedSlot, ...] = ()
    passed_opportunities: tuple[PassedOpportunity, ...] = ()
    schedule_assumptions: tuple[str, ...] = ()
    explanation_reasons: tuple[PlanningReasonCode, ...] = ()
    freshness: FreshnessSummary = field(default_factory=FreshnessSummary)
    warnings: tuple[PlanningReasonCode, ...] = ()
    blocking_reasons: tuple[PlanningReasonCode, ...] = ()
    score_actions: tuple[PlannedScoreAction, ...] = ()
    pre_lock_move_count: int = 0
    recovery_obligations: tuple[RecoveryObligation, ...] = ()
    forfeited_opportunities: tuple[tuple[str, str], ...] = ()
    bench_player_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.league_id, "Plan league ID")
        _require_text(self.season, "Plan season")
        if self.week <= 0:
            raise PlanningStateError("Plan fantasy weeks must be positive")
        if self.roster_id <= 0:
            raise PlanningStateError("Plan roster IDs must be positive")
        _require_aware(self.decision_time, "Plan decision time")
        for name, value in (
            ("Plan planner version", self.planner_version),
            ("Plan manager policy version", self.manager_policy_version),
            ("Plan scoring policy version", self.scoring_policy_version),
            ("Plan league configuration version", self.league_configuration_version),
            ("Plan projection model version", self.projection_model_version),
            ("Plan input version", self.input_version),
        ):
            _require_text(value, name)
        for label, score in (
            ("Expected terminal score", self.expected_terminal_score),
            ("Best alternative score", self.best_alternative_score),
            ("Observed terminal score", self.observed_terminal_score),
            ("Decision margin", self.decision_margin),
        ):
            if score is not None and not isfinite(score):
                raise PlanningStateError(f"{label} must be finite when present")
        if any(not assumption.strip() for assumption in self.schedule_assumptions):
            raise PlanningStateError("Schedule assumptions must be non-empty")

        observed_players = _assignment_player_map(self.observed_assignments)
        desired_players = _assignment_player_map(self.desired_assignments)
        observed_positions = {
            assignment.slot_index: assignment.slot_position
            for assignment in self.observed_assignments
        }
        desired_positions = {
            assignment.slot_index: assignment.slot_position
            for assignment in self.desired_assignments
        }
        if set(observed_players) != set(desired_players):
            raise PlanningStateError("Observed and desired assignments must cover the same slots")
        for index in observed_players:
            if observed_positions[index] != desired_positions[index]:
                raise PlanningStateError(
                    "Observed and desired assignments disagree on a slot position"
                )

        if not 0 <= self.pre_lock_move_count <= len(self.moves):
            raise PlanningStateError("Pre-lock moves must be a prefix of the action sequence")
        pre_lock = apply_lineup_moves(self.moves[: self.pre_lock_move_count], observed_players)
        for action in self.score_actions:
            if action.kind is LockInDecisionKind.LOCK:
                assert action.slot_index is not None
                if pre_lock.get(action.slot_index) != action.player_id:
                    raise PlanningStateError("A proposed lock requires its exact pre-lock position")
                if any(
                    action.slot_index in (move.source_slot_index, move.target_slot_index)
                    for move in self.moves[self.pre_lock_move_count :]
                ):
                    raise PlanningStateError("Later moves cannot relocate a proposed accepted lock")
        simulated = apply_lineup_moves(self.moves, observed_players)
        if simulated != desired_players:
            raise PlanningStateError("Lineup moves do not produce the desired assignment")

        for fixed in self.fixed_slots:
            if (
                self.status is not PlanStatus.BLOCKED
                and desired_players.get(fixed.slot_index) != fixed.player_id
            ):
                raise PlanningStateError("Desired assignment does not preserve a fixed slot")

        locked_indices = {fixed.slot_index for fixed in self.fixed_slots}
        for move in self.moves:
            if move.source_slot_index in locked_indices or move.target_slot_index in locked_indices:
                raise PlanningStateError("Locked slots cannot appear as move sources or targets")

        if (self.status is PlanStatus.BLOCKED) != bool(self.blocking_reasons):
            raise PlanningStateError("Blocked plans require blocking reasons and vice versa")
        if self.status is PlanStatus.NO_ACTION and (self.moves or self.score_actions):
            raise PlanningStateError("No-action plans cannot contain moves")
        if self.status is PlanStatus.ACTION_REQUIRED and not (self.moves or self.score_actions):
            raise PlanningStateError("Actionable plans require moves")
        if self.status is not PlanStatus.BLOCKED:
            for move in self.moves:
                if move.deadline <= self.decision_time:
                    raise PlanningStateError(
                        "Actionable move deadlines must follow the decision time"
                    )

    @property
    def material_hash(self) -> str:
        return sha256(repr(self._material_components()).encode()).hexdigest()

    @property
    def plan_id(self) -> str:
        payload = (
            "weekly-plan-identity-v1",
            *self._material_components(),
            self._lineage_components(),
        )
        return sha256(repr(payload).encode()).hexdigest()

    def _material_components(self) -> tuple[object, ...]:
        return (
            "weekly-plan-material-v3",
            self.desired_assignments,
            tuple(
                (
                    row.player_id,
                    row.game_id,
                    row.kind.value,
                    row.slot_index,
                    row.slot_position,
                    row.score,
                    row.deadline,
                )
                for row in self.score_actions
            ),
            tuple(row.recovery_id for row in self.recovery_obligations),
            self.forfeited_opportunities,
            self.bench_player_ids,
            tuple((row.player_id, row.game_id) for row in self.passed_opportunities),
            self.league_id,
            self.season,
            self.week,
            self.roster_id,
            self.status.value,
            tuple(
                (
                    move.player_id,
                    move.source_slot_index,
                    move.target_slot_index,
                    move.swap_player_id,
                    move.deadline.isoformat(),
                )
                for move in self.moves
            ),
            tuple(
                sorted(
                    (fixed.slot_index, fixed.player_id, fixed.game_id, fixed.accepted_fantasy_score)
                    for fixed in self.fixed_slots
                )
            ),
            self.planner_version,
            self.manager_policy_version,
            tuple(reason.value for reason in self.blocking_reasons),
        )

    def _lineage_components(self) -> tuple[object, ...]:
        return (
            self.decision_time.isoformat(),
            self.scoring_policy_version,
            self.league_configuration_version,
            self.projection_model_version,
            self.input_version,
            self.confidence.value,
            _format_optional(self.expected_terminal_score),
            _format_optional(self.best_alternative_score),
            _format_optional(self.observed_terminal_score),
            _format_optional(self.decision_margin),
            None
            if self.distribution_summary is None
            else (
                self.distribution_summary.scenario_count,
                self.distribution_summary.seed,
                self.distribution_summary.approximation,
                repr(round(self.distribution_summary.perfect_information_bound, 6)),
                self.distribution_summary.postgame_scenario_count,
                self.distribution_summary.minimum_lock_in_confidence,
            ),
            tuple(
                passed.player_id
                for passed in sorted(self.passed_opportunities, key=_passed_sort_key)
            ),
            tuple(reason.value for reason in self.warnings),
            tuple(reason.value for reason in self.explanation_reasons),
            tuple(sorted(self.schedule_assumptions)),
            tuple(
                (source.source, source.version, source.available_as_of.isoformat())
                for source in sorted(self.freshness.sources, key=lambda item: item.source)
            ),
        )


def _assignment_player_map(
    assignments: tuple[PlannedAssignment, ...],
) -> dict[int, str | None]:
    players: dict[int, str | None] = {}
    occupied: set[str] = set()
    for assignment in assignments:
        if assignment.slot_index in players:
            raise PlanningStateError("Planned assignments cannot reuse a slot")
        if assignment.player_id is not None:
            if assignment.player_id in occupied:
                raise PlanningStateError("Planned assignments cannot reuse a player")
            occupied.add(assignment.player_id)
        players[assignment.slot_index] = assignment.player_id
    return players


def _passed_sort_key(passed: PassedOpportunity) -> tuple[str, str]:
    return passed.player_id, passed.game_id


def _format_optional(value: float | None) -> str | None:
    return None if value is None else repr(round(value, 6))
