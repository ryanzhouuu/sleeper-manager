"""Characterization contracts for decomposing the weekly planner safely.

These tests pin the import boundary and cyclic move ordering that must survive
the planned extraction of models, assignment evaluation, and move planning.
"""

from datetime import UTC, datetime, timedelta

from sleeper_manager.decisions import weekly_plan
from sleeper_manager.domain.planning import (
    FreshnessSummary,
    GameOpportunity,
    ObservedStarter,
    PlannedAssignment,
    PlanningGameStatus,
    PlanningQuality,
    SourceLineage,
    StarterSlot,
    TeamWeekState,
)
from sleeper_manager.domain.projection import ProjectionDistribution, ProjectionSnapshot

NOW = datetime(2026, 1, 5, 12, tzinfo=UTC)
EXPECTED_PUBLIC_EXPORTS = (
    "DEFAULT_MOVE_LEAD_TIME",
    "PlacementEvaluation",
    "TerminalValueApproximation",
    "WEEKLY_PLANNER_VERSION",
    "WeeklyPlanDecision",
    "WeeklyPlanError",
    "WeeklyPlanOption",
    "WeeklyPlanPolicyConfig",
    "build_weekly_plan",
    "score_weekly_options",
)


def _opportunity(player_id: str, target_slot_index: int) -> GameOpportunity:
    """Create a positive-value opportunity restricted to one target slot."""
    game_id = f"game-{player_id}"
    start = NOW + timedelta(hours=1)
    return GameOpportunity(
        sleeper_player_id=player_id,
        provider_player_id=f"provider-{player_id}",
        game_id=game_id,
        scheduled_start=start,
        status=PlanningGameStatus.SCHEDULED,
        roster_id=1,
        membership_segment="segment-1",
        eligible_slot_indices=(target_slot_index,),
        eligible_positions=("PG",),
        rostered_at_tipoff=True,
        availability_status="available",
        availability_evidence_at=NOW,
        projection=ProjectionSnapshot(
            player_id=player_id,
            game_id=game_id,
            available_as_of=NOW,
            model_version="fixture-model",
            input_version="fixture-inputs",
            scoring_policy_version="fixture-scoring",
            distribution=ProjectionDistribution.from_weighted_observations(((10, 1),)),
            reasons=(),
        ),
        missing_projection_reason=None,
        completed_fantasy_score=None,
        finalized_at=None,
        source_lineage=(SourceLineage("fixture", "v1", NOW, NOW),),
    )


def _cyclic_state() -> TeamWeekState:
    """Build a state whose only optimal lineup rotates all three starters."""
    opportunities = (
        _opportunity("a", 2),
        _opportunity("b", 0),
        _opportunity("c", 1),
    )
    return TeamWeekState(
        league_id="league-1",
        season="2026",
        week=1,
        roster_id=1,
        decision_time=NOW,
        starter_slots=(
            StarterSlot(0, "UTIL"),
            StarterSlot(1, "UTIL"),
            StarterSlot(2, "UTIL"),
        ),
        roster_player_ids=("a", "b", "c"),
        observed_starters=(
            ObservedStarter(0, "a", ("PG",)),
            ObservedStarter(1, "b", ("PG",)),
            ObservedStarter(2, "c", ("PG",)),
        ),
        opportunities=opportunities,
        fixed_slots=(),
        passed_opportunities=(),
        scoring_policy_version="scoring-v1",
        league_configuration_version="league-v1",
        manager_policy_version="policy-v1",
        projection_model_version="fixture-model",
        input_version="fixture-inputs",
        freshness=FreshnessSummary((SourceLineage("fixture", "v1", NOW, NOW),)),
        eligibility_quality=PlanningQuality.EXACT,
        blocking_reasons=(),
    )


def test_weekly_plan_public_exports_remain_stable() -> None:
    """Protect existing import paths while implementation moves to smaller modules."""
    assert weekly_plan.__all__ == EXPECTED_PUBLIC_EXPORTS
    assert all(hasattr(weekly_plan, name) for name in EXPECTED_PUBLIC_EXPORTS)


def test_weekly_plan_orders_a_three_way_rotation_through_the_bench() -> None:
    """Pin the safe move sequence when every desired target begins occupied."""
    plan = weekly_plan.build_weekly_plan(_cyclic_state())

    assert plan.desired_assignments == (
        PlannedAssignment(0, "UTIL", "b"),
        PlannedAssignment(1, "UTIL", "c"),
        PlannedAssignment(2, "UTIL", "a"),
    )
    assert [
        (move.player_id, move.source_slot_index, move.target_slot_index) for move in plan.moves
    ] == [
        ("a", 0, None),
        ("b", 1, 0),
        ("c", 2, 1),
        ("a", None, 2),
    ]
    assert all(
        move.deadline == NOW + timedelta(hours=1) - weekly_plan.DEFAULT_MOVE_LEAD_TIME
        for move in plan.moves
    )
