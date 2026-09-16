"""build_weekly_plan starter, bench, lock, and blocked-state coverage."""

from datetime import timedelta

from sleeper_manager.decisions.weekly_plan import (
    build_weekly_plan,
)
from sleeper_manager.domain.planning import (
    FixedSlot,
    ObservedStarter,
    PlanConfidence,
    PlanningGameStatus,
    PlanningReasonCode,
    PlanStatus,
    StarterSlot,
)
from tests.sleeper_manager.decisions.weekly_plan_support import (
    NOW,
    _opportunity,
    _planned,
    _state,
)


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
    assert plan.observed_terminal_score == 10
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


def test_build_weekly_plan_ignores_later_games_for_locked_players() -> None:
    """Do not duplicate a fixed player into an open slot on a later game."""

    fixed_opportunity = _opportunity(
        "p3",
        "g0",
        NOW - timedelta(hours=2),
        ("PG",),
        ((20, 1),),
        status=PlanningGameStatus.FINAL,
        completed_score=20,
        finalized_at=NOW - timedelta(hours=1),
    )
    state = _state(
        (
            fixed_opportunity,
            _opportunity("p3", "g1", NOW + timedelta(hours=1), ("PG",), ((100, 1),)),
            _opportunity("p2", "g2", NOW + timedelta(hours=2), ("PG",), ((10, 1),)),
        ),
        observed=(ObservedStarter(0, "p3", ("PG",)),),
        fixed=(FixedSlot(0, "G", "p3", "g0", 20, NOW, "lock-1", "fixture"),),
    )

    plan = build_weekly_plan(state)

    assert plan.status is PlanStatus.ACTION_REQUIRED
    assert plan.desired_assignments[0] == _planned(0, "G", "p3")
    assert plan.desired_assignments[1] == _planned(1, "UTIL", "p2")


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
