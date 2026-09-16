"""Shared TeamWeekState factories for weekly-plan tests."""

from datetime import UTC, datetime, timedelta

from sleeper_manager.decisions.weekly_plan import (
    WEEKLY_PLANNER_VERSION,
)
from sleeper_manager.domain.planning import (
    FixedSlot,
    FreshnessSummary,
    GameOpportunity,
    LineupMove,
    ObservedStarter,
    PassedOpportunity,
    PlanConfidence,
    PlannedAssignment,
    PlanningGameStatus,
    PlanningQuality,
    PlanningReasonCode,
    PlanStatus,
    SourceLineage,
    StarterSlot,
    TeamWeekState,
    WeeklyPlan,
)
from sleeper_manager.domain.projection import ProjectionDistribution, ProjectionSnapshot

NOW = datetime(2026, 1, 5, 12, tzinfo=UTC)
DEADLINE = NOW + timedelta(minutes=50)


def _projection(
    player_id: str,
    game_id: str,
    observations: tuple[tuple[float, float], ...],
) -> ProjectionSnapshot:
    return ProjectionSnapshot(
        player_id=player_id,
        game_id=game_id,
        available_as_of=NOW,
        model_version="fixture-model",
        input_version="fixture-inputs",
        scoring_policy_version="fixture-scoring",
        distribution=ProjectionDistribution.from_weighted_observations(observations),
        reasons=(),
    )


def _opportunity(
    player_id: str,
    game_id: str,
    start: datetime,
    positions: tuple[str, ...],
    observations: tuple[tuple[float, float], ...],
    *,
    status: PlanningGameStatus = PlanningGameStatus.SCHEDULED,
    rostered: bool | None = True,
    completed_score: float | None = None,
    finalized_at: datetime | None = None,
    eligible_slot_indices: tuple[int, ...] | None = None,
    missing_projection_reason: PlanningReasonCode | None = None,
) -> GameOpportunity:
    return GameOpportunity(
        sleeper_player_id=player_id,
        provider_player_id=f"provider-{player_id}",
        game_id=game_id,
        scheduled_start=start,
        status=status,
        roster_id=1,
        membership_segment="segment-1",
        eligible_slot_indices=(
            eligible_slot_indices if eligible_slot_indices is not None else (0, 1)
        ),
        eligible_positions=positions,
        rostered_at_tipoff=rostered,
        availability_status="available",
        availability_evidence_at=NOW,
        projection=None
        if missing_projection_reason is not None
        else _projection(player_id, game_id, observations),
        missing_projection_reason=missing_projection_reason,
        completed_fantasy_score=completed_score,
        finalized_at=finalized_at,
        source_lineage=(SourceLineage("fixture", "v1", NOW, NOW),),
    )


def _state(
    opportunities: tuple[GameOpportunity, ...],
    *,
    observed: tuple[ObservedStarter, ...] = (),
    fixed: tuple[FixedSlot, ...] = (),
    passed: tuple[PassedOpportunity, ...] = (),
    blocking: tuple[PlanningReasonCode, ...] = (),
    warnings: tuple[PlanningReasonCode, ...] = (),
    quality: PlanningQuality = PlanningQuality.UNKNOWN,
    starter_slots: tuple[StarterSlot, ...] = (
        StarterSlot(0, "G"),
        StarterSlot(1, "UTIL"),
    ),
) -> TeamWeekState:
    return TeamWeekState(
        league_id="league-1",
        season="2026",
        week=1,
        roster_id=1,
        decision_time=NOW,
        starter_slots=starter_slots,
        roster_player_ids=tuple(
            sorted({opportunity.sleeper_player_id for opportunity in opportunities})
        ),
        observed_starters=observed,
        opportunities=opportunities,
        fixed_slots=fixed,
        passed_opportunities=passed,
        scoring_policy_version="scoring-v1",
        league_configuration_version="league-v1",
        manager_policy_version="policy-v1",
        projection_model_version="fixture-model",
        input_version="fixture-inputs",
        freshness=FreshnessSummary((SourceLineage("fixture", "v1", NOW, NOW),)),
        eligibility_quality=quality,
        warnings=warnings,
        blocking_reasons=blocking,
    )


def _planned(slot_index: int, position: str, player_id: str | None) -> PlannedAssignment:
    return PlannedAssignment(slot_index, position, player_id)


def _plan(**overrides: object) -> WeeklyPlan:
    values: dict[str, object] = {
        "league_id": "league-1",
        "season": "2026",
        "week": 1,
        "roster_id": 1,
        "decision_time": NOW,
        "status": PlanStatus.ACTION_REQUIRED,
        "observed_assignments": (
            _planned(0, "G", "p1"),
            _planned(1, "UTIL", "p2"),
        ),
        "desired_assignments": (
            _planned(0, "G", "p2"),
            _planned(1, "UTIL", "p1"),
        ),
        "moves": (
            LineupMove("p1", 0, None, DEADLINE),
            LineupMove("p2", 1, 0, DEADLINE),
            LineupMove("p1", None, 1, DEADLINE),
        ),
        "confidence": PlanConfidence.HIGH,
        "planner_version": WEEKLY_PLANNER_VERSION,
        "manager_policy_version": "policy-v1",
    }
    values.update(overrides)
    return WeeklyPlan(**values)
