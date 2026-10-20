"""Protect active game eligibility through intermediate lineup moves."""

from datetime import timedelta

import pytest

from sleeper_manager.decisions._weekly_plan_moves import plan_moves
from sleeper_manager.decisions.weekly_plan import WeeklyPlanError, build_weekly_plan
from sleeper_manager.domain.planning import (
    ObservedStarter,
    PlanningGameStatus,
    PlanningReasonCode,
    PlanStatus,
    StarterSlot,
)
from tests.sleeper_manager.decisions.weekly_plan_support import NOW, _opportunity, _planned, _state


def test_swap_breaks_a_cycle_with_the_inactive_player() -> None:
    state = _state(
        (
            _opportunity(
                "a-active",
                "active",
                NOW - timedelta(hours=1),
                ("PG",),
                ((20, 1),),
                status=PlanningGameStatus.ACTIVE,
            ),
            _opportunity("b-upcoming", "early", NOW + timedelta(hours=1), ("PG",), ((40, 1),)),
        ),
        observed=(
            ObservedStarter(0, "a-active", ("PG",)),
            ObservedStarter(1, "b-upcoming", ("PG",)),
        ),
    )

    moves = plan_moves(
        state,
        (_planned(0, "G", "b-upcoming"), _planned(1, "UTIL", "a-active")),
        NOW + timedelta(hours=1),
        timedelta(minutes=10),
    )

    assert [(move.player_id, move.target_slot_index) for move in moves] == [
        ("b-upcoming", None),
        ("a-active", 1),
        ("b-upcoming", 0),
    ]
    assert all(move.deadline == NOW + timedelta(minutes=50) for move in moves)


def test_move_generation_rejects_active_player_omission() -> None:
    state = _state(
        (
            _opportunity(
                "active",
                "active",
                NOW - timedelta(hours=1),
                ("PG",),
                ((20, 1),),
                status=PlanningGameStatus.ACTIVE,
            ),
        ),
        observed=(ObservedStarter(0, "active", ("PG",)),),
    )

    with pytest.raises(WeeklyPlanError, match="bench an active starter"):
        plan_moves(
            state,
            (_planned(0, "G", None), _planned(1, "UTIL", None)),
            NOW + timedelta(hours=1),
            timedelta(minutes=10),
        )


def test_infeasible_active_cycle_blocks_without_emitting_bench_instructions() -> None:
    active = tuple(
        _opportunity(
            player,
            player,
            NOW - timedelta(hours=1),
            ("PG",),
            ((20, 1),),
            status=PlanningGameStatus.ACTIVE,
            eligible_slot_indices=(1 - i,),
        )
        for i, player in enumerate(("a", "b"))
    )
    upcoming = _opportunity(
        "early",
        "early",
        NOW + timedelta(hours=1),
        ("PG",),
        ((40, 1),),
        eligible_slot_indices=(2,),
    )
    state = _state(
        (*active, upcoming),
        observed=(ObservedStarter(0, "a", ("PG",)), ObservedStarter(1, "b", ("PG",))),
        starter_slots=tuple(StarterSlot(i, "UTIL") for i in range(3)),
    )

    plan = build_weekly_plan(state)

    assert plan.status is PlanStatus.BLOCKED
    assert plan.blocking_reasons == (PlanningReasonCode.ROSTER_STATE_MISMATCH,)
    assert plan.moves == ()
    assert plan.desired_assignments == plan.observed_assignments
