"""Only explicitly named, unlocked trial performances may bypass active-starter protection."""

from dataclasses import replace
from datetime import timedelta

import pytest

from sleeper_manager.decisions._weekly_plan_moves import plan_moves
from sleeper_manager.decisions.weekly_plan import WeeklyPlanError
from sleeper_manager.domain.planning import (
    ObservedStarter,
    PlanningGameStatus,
    PlanningStateError,
    StarterSlot,
)
from sleeper_manager.domain.planning_actions import RecoveryObligation
from tests.sleeper_manager.decisions.weekly_plan_support import NOW, _opportunity, _planned, _state


def test_active_recovery_requires_a_named_obligation_and_atomic_swap() -> None:
    attempt = _opportunity(
        "trial",
        "game",
        NOW - timedelta(hours=1),
        ("PG",),
        ((10, 1),),
        status=PlanningGameStatus.ACTIVE,
        eligible_slot_indices=(0,),
    )
    later = _opportunity(
        "baseline",
        "future",
        NOW + timedelta(hours=1),
        ("PG",),
        ((60, 1),),
        eligible_slot_indices=(0,),
    )
    target = (_planned(0, "UTIL", "baseline"),)
    state = replace(
        _state(
            (attempt, later),
            starter_slots=(StarterSlot(0, "UTIL"),),
            observed=(ObservedStarter(0, "trial", ("PG",)),),
        ),
        bench_capacity=1,
    )
    with pytest.raises(WeeklyPlanError, match="active starter"):
        plan_moves(state, target, later.scheduled_start, timedelta(minutes=10))
    with pytest.raises(PlanningStateError, match="named recovery"):
        replace(state, recovery_forfeitures=(("trial", "game"),))
    proof = RecoveryObligation(
        (("trial", "game"),),
        target,
        (_planned(0, "UTIL", "trial"),),
        later.scheduled_start,
        later.scheduled_start - timedelta(minutes=10),
        NOW - timedelta(hours=2),
        "observed-before-trial",
    )
    permitted = replace(
        state, recovery_obligations=(proof,), recovery_forfeitures=proof.added_player_games
    )
    moves = plan_moves(permitted, target, later.scheduled_start, timedelta(minutes=10))
    assert len(moves) == 1 and moves[0].swap_player_id == "trial"


def test_recovery_cannot_authorize_a_different_active_performance() -> None:
    target = (_planned(0, "UTIL", "baseline"),)
    proof = RecoveryObligation(
        (("trial", "game"),),
        target,
        (_planned(0, "UTIL", "trial"),),
        NOW + timedelta(hours=2),
        NOW + timedelta(hours=1),
        NOW,
        "before-trial",
    )
    state = _state((), starter_slots=(StarterSlot(0, "UTIL"),))
    with pytest.raises(PlanningStateError, match="named recovery"):
        replace(state, recovery_obligations=(proof,), recovery_forfeitures=(("other", "game"),))
