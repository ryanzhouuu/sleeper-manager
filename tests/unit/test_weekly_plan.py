from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from sleeper_manager.decisions.weekly_plan import (
    WEEKLY_PLANNER_VERSION,
    build_weekly_plan,
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
    PlanningStateError,
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


def test_build_weekly_plan_requires_the_better_bench_starter() -> None:
    start = NOW + timedelta(hours=1)
    state = _state(
        (
            _opportunity("p1", "g1", start, ("PG",), ((10, 1),)),
            _opportunity("p2", "g1", start, ("PG",), ((30, 1),)),
        ),
        observed=(ObservedStarter(0, "p1", ("PG",)),),
        starter_slots=(StarterSlot(0, "G"),),
    )

    plan = build_weekly_plan(state)

    assert plan.status is PlanStatus.ACTION_REQUIRED
    assert [
        (move.player_id, move.source_slot_index, move.target_slot_index) for move in plan.moves
    ] == [("p1", 0, None), ("p2", None, 0)]
    assert all(move.deadline == start - timedelta(minutes=10) for move in plan.moves)
    assert plan.expected_terminal_score == 30
    assert plan.best_alternative_score == 10
    assert plan.decision_margin == 20
    assert plan.confidence is PlanConfidence.LOW
    assert plan.desired_assignments == (_planned(0, "G", "p2"),)


def test_build_weekly_plan_starts_a_bench_player_into_an_open_slot() -> None:
    start = NOW + timedelta(hours=1)
    state = _state(
        (
            _opportunity("p1", "g1", start, ("PG",), ((10, 1),)),
            _opportunity("p2", "g1", start, ("PG",), ((30, 1),)),
        ),
        observed=(ObservedStarter(0, "p1", ("PG",)),),
    )

    plan = build_weekly_plan(state)

    assert [
        (move.player_id, move.source_slot_index, move.target_slot_index) for move in plan.moves
    ] == [("p2", None, 1)]
    assert plan.status is PlanStatus.ACTION_REQUIRED
    assert plan.desired_assignments == (
        _planned(0, "G", "p1"),
        _planned(1, "UTIL", "p2"),
    )


def test_build_weekly_plan_keeps_an_optimal_observed_lineup() -> None:
    start = NOW + timedelta(hours=1)
    state = _state(
        (
            _opportunity("p1", "g1", start, ("PG",), ((30, 1),)),
            _opportunity("p2", "g1", start, ("PG",), ((10, 1),)),
        ),
        observed=(ObservedStarter(0, "p1", ("PG",)),),
        starter_slots=(StarterSlot(0, "G"),),
    )

    plan = build_weekly_plan(state)

    assert plan.status is PlanStatus.NO_ACTION
    assert plan.moves == ()
    assert plan.expected_terminal_score == 30
    assert plan.observed_assignments == plan.desired_assignments
    assert build_weekly_plan(state) == plan


def test_build_weekly_plan_reports_blocked_states_without_scoring() -> None:
    start = NOW + timedelta(hours=1)
    state = _state(
        (
            _opportunity(
                "p1",
                "g1",
                start,
                ("PG",),
                (),
                missing_projection_reason=PlanningReasonCode.MISSING_PROJECTION,
            ),
        ),
        observed=(ObservedStarter(0, "p1", ("PG",)),),
        blocking=(PlanningReasonCode.STALE_SLEEPER_STATE,),
    )

    plan = build_weekly_plan(state)

    assert plan.status is PlanStatus.BLOCKED
    assert plan.blocking_reasons == (PlanningReasonCode.STALE_SLEEPER_STATE,)
    assert plan.moves == ()
    assert plan.expected_terminal_score is None
    assert plan.confidence is PlanConfidence.LOW


def test_build_weekly_plan_blocks_when_a_batch_projection_is_missing() -> None:
    start = NOW + timedelta(hours=1)
    state = _state(
        (
            _opportunity("p1", "g1", start, ("PG",), ((10, 1),)),
            _opportunity(
                "p2",
                "g1",
                start,
                ("PG",),
                (),
                missing_projection_reason=PlanningReasonCode.MISSING_PROJECTION,
            ),
        ),
        observed=(ObservedStarter(0, "p1", ("PG",)),),
    )

    plan = build_weekly_plan(state)

    assert plan.status is PlanStatus.BLOCKED
    assert plan.blocking_reasons == (PlanningReasonCode.MISSING_PROJECTION,)


def test_build_weekly_plan_blocks_when_the_lead_time_elapses_the_deadline() -> None:
    start = NOW + timedelta(hours=1)
    state = _state(
        (
            _opportunity("p1", "g1", start, ("PG",), ((10, 1),)),
            _opportunity("p2", "g1", start, ("PG",), ((30, 1),)),
        ),
        observed=(ObservedStarter(0, "p1", ("PG",)),),
    )

    plan = build_weekly_plan(state, lead_time=timedelta(hours=2))

    assert plan.status is PlanStatus.BLOCKED
    assert plan.blocking_reasons == (PlanningReasonCode.DEADLINE_ELAPSED,)


def test_build_weekly_plan_never_moves_a_fixed_slot() -> None:
    start = NOW + timedelta(hours=1)
    finalized_at = NOW - timedelta(hours=1)
    fixed_opportunity = _opportunity(
        "p3",
        "g0",
        NOW - timedelta(hours=2),
        ("PG",),
        ((20, 1),),
        status=PlanningGameStatus.FINAL,
        completed_score=20,
        finalized_at=finalized_at,
    )
    state = _state(
        (
            fixed_opportunity,
            _opportunity("p1", "g1", start, ("PG",), ((10, 1),)),
            _opportunity("p2", "g1", start, ("PG",), ((40, 1),)),
        ),
        observed=(
            ObservedStarter(0, "p3", ("PG",)),
            ObservedStarter(1, "p1", ("PG",)),
        ),
        fixed=(FixedSlot(0, "G", "p3", "g0", 20, NOW - timedelta(hours=3), "lock-1", "fixture"),),
    )

    plan = build_weekly_plan(state)

    assert all(0 not in (move.source_slot_index, move.target_slot_index) for move in plan.moves)
    assert plan.desired_assignments[0] == _planned(0, "G", "p3")
    assert plan.fixed_slots[0].player_id == "p3"
    assert plan.status is PlanStatus.ACTION_REQUIRED


def test_build_weekly_plan_without_remaining_games_needs_no_action() -> None:
    finalized_at = NOW - timedelta(hours=1)
    state = _state(
        (
            _opportunity(
                "p1",
                "g1",
                NOW - timedelta(hours=2),
                ("PG",),
                ((12, 1),),
                status=PlanningGameStatus.FINAL,
                completed_score=12,
                finalized_at=finalized_at,
            ),
        ),
        observed=(ObservedStarter(0, "p1", ("PG",)),),
        starter_slots=(StarterSlot(0, "G"),),
    )

    plan = build_weekly_plan(state)

    assert plan.status is PlanStatus.NO_ACTION
    assert plan.expected_terminal_score is None
    assert plan.schedule_assumptions == ()


def test_build_weekly_plan_labels_warned_plans_degraded() -> None:
    start = NOW + timedelta(hours=1)
    state = _state(
        (
            _opportunity("p1", "g1", start, ("PG",), ((30, 1),)),
            _opportunity("p2", "g1", start, ("PG",), ((10, 1),)),
        ),
        observed=(ObservedStarter(0, "p1", ("PG",)),),
        starter_slots=(StarterSlot(0, "G"),),
        quality=PlanningQuality.EXACT,
        warnings=(PlanningReasonCode.STALE_NBA_STATE,),
    )

    plan = build_weekly_plan(state, lead_time=timedelta(minutes=10))

    assert plan.status is PlanStatus.DEGRADED
    assert plan.moves == ()
    assert plan.warnings == (PlanningReasonCode.STALE_NBA_STATE,)
    assert plan.confidence is PlanConfidence.HIGH


def test_high_confidence_requires_exact_quality_or_a_clear_margin() -> None:
    start = NOW + timedelta(hours=1)
    state = _state(
        (
            _opportunity("p1", "g1", start, ("PG",), ((10, 1),)),
            _opportunity("p2", "g1", start, ("PG",), ((30, 1),)),
        ),
        observed=(ObservedStarter(0, "p1", ("PG",)),),
        quality=PlanningQuality.EXACT,
        starter_slots=(StarterSlot(0, "G"),),
    )

    plan = build_weekly_plan(state)

    assert plan.status is PlanStatus.ACTION_REQUIRED
    assert plan.decision_margin == 20
    assert plan.confidence is PlanConfidence.HIGH


def test_swap_moves_require_a_temporary_bench_step() -> None:
    plan = _plan()

    assert plan.status is PlanStatus.ACTION_REQUIRED
    assert plan.material_hash


def test_double_occupying_move_orders_fail_closed() -> None:
    with pytest.raises(PlanningStateError, match="targets an occupied slot"):
        _plan(
            moves=(
                LineupMove("p1", 0, 1, DEADLINE),
                LineupMove("p2", 1, 0, DEADLINE),
            )
        )


def test_three_way_cycles_require_a_temporary_bench_step() -> None:
    observed = (
        _planned(0, "G", "a"),
        _planned(1, "UTIL", "b"),
        _planned(2, "UTIL", "c"),
    )
    desired = (
        _planned(0, "G", "b"),
        _planned(1, "UTIL", "c"),
        _planned(2, "UTIL", "a"),
    )
    naive = (
        LineupMove("a", 0, 2, DEADLINE),
        LineupMove("b", 1, 0, DEADLINE),
        LineupMove("c", 2, 1, DEADLINE),
    )
    staged = (
        LineupMove("a", 0, None, DEADLINE),
        LineupMove("b", 1, 0, DEADLINE),
        LineupMove("c", 2, 1, DEADLINE),
        LineupMove("a", None, 2, DEADLINE),
    )

    with pytest.raises(PlanningStateError, match="targets an occupied slot"):
        _plan(observed_assignments=observed, desired_assignments=desired, moves=naive)

    plan = _plan(observed_assignments=observed, desired_assignments=desired, moves=staged)
    assert len(plan.moves) == 4


def test_fixed_slots_demand_desired_preservation() -> None:
    fixed = (FixedSlot(1, "UTIL", "p2", "g9", 5, NOW - timedelta(days=1), "lock-1", "fixture"),)
    with pytest.raises(PlanningStateError, match="preserve a fixed slot"):
        _plan(fixed_slots=fixed)


def test_material_hash_ignores_explanation_only_drift() -> None:
    plan = _plan()

    drifted = replace(plan, expected_terminal_score=999.5, decision_margin=-1.25)
    retimed = replace(
        plan,
        moves=tuple(
            replace(move, deadline=move.deadline + timedelta(minutes=1)) for move in plan.moves
        ),
    )

    assert plan.material_hash == drifted.material_hash
    assert plan.plan_id != drifted.plan_id
    assert plan.material_hash != retimed.material_hash


def test_plan_identity_tracks_lineage_versions() -> None:
    plan = _plan()

    relined = replace(plan, input_version="fixture-inputs-2")

    assert plan.material_hash == relined.material_hash
    assert plan.plan_id != relined.plan_id


def test_actionable_deadlines_must_follow_the_decision_time() -> None:
    expired = tuple(replace(move, deadline=NOW - timedelta(minutes=1)) for move in _plan().moves)
    with pytest.raises(PlanningStateError, match="follow the decision time"):
        _plan(moves=expired)


def test_status_and_move_counts_must_agree() -> None:
    with pytest.raises(PlanningStateError, match="No-action plans"):
        _plan(status=PlanStatus.NO_ACTION)
    with pytest.raises(PlanningStateError, match="Actionable plans require moves"):
        _plan(
            moves=(),
            desired_assignments=(
                _planned(0, "G", "p1"),
                _planned(1, "UTIL", "p2"),
            ),
        )
    with pytest.raises(PlanningStateError, match="blocking reasons"):
        _plan(blocking_reasons=(PlanningReasonCode.MISSING_PROJECTION,))
    blocked = _plan(
        status=PlanStatus.BLOCKED,
        blocking_reasons=(PlanningReasonCode.DEADLINE_ELAPSED,),
        moves=tuple(replace(move, deadline=NOW - timedelta(minutes=1)) for move in _plan().moves),
    )
    assert blocked.status is PlanStatus.BLOCKED
