"""A live game that can no longer be locked must not reserve a slot or block advice."""

from dataclasses import replace
from datetime import timedelta

import pytest

from sleeper_manager.decisions.team_recommendation import build_team_recommendation
from sleeper_manager.decisions.weekly_plan import WeeklyPlanPolicyConfig
from sleeper_manager.domain.planning import (
    GameOpportunity,
    ObservedStarter,
    PlanningGameStatus,
    StarterSlot,
    TeamWeekState,
)
from tests.sleeper_manager.decisions.weekly_plan_support import NOW, _opportunity, _state

POLICY = WeeklyPlanPolicyConfig(scenario_count=16, postgame_scenario_count=4)


def _live(*, rostered: bool | None, eligible: tuple[int, ...]) -> GameOpportunity:
    return _opportunity(
        "late",
        "live",
        NOW - timedelta(hours=1),
        ("PG",),
        ((10, 1),),
        status=PlanningGameStatus.ACTIVE,
        rostered=rostered,
        eligible_slot_indices=eligible,
    )


def _one_slot_state(live: GameOpportunity) -> TeamWeekState:
    upcoming = _opportunity(
        "upcoming",
        "next",
        NOW + timedelta(hours=2),
        ("PG",),
        ((30, 1),),
        eligible_slot_indices=(0,),
    )
    return replace(
        _state(
            (live, upcoming),
            observed=(ObservedStarter(0, "late", ("PG",)),),
            starter_slots=(StarterSlot(0, "UTIL"),),
        ),
        bench_capacity=1,
    )


@pytest.mark.parametrize("rostered", (False, True))
def test_unlockable_live_starter_gives_up_the_slot_without_blocking(rostered: bool) -> None:
    state = _one_slot_state(_live(rostered=rostered, eligible=()))

    plan = build_team_recommendation(state, policy=POLICY)

    assert plan.blocking_reasons == ()
    assert plan.desired_assignments[0].player_id == "upcoming"


def test_late_starter_recorded_as_benched_at_tipoff_gives_up_the_slot() -> None:
    state = _one_slot_state(_live(rostered=False, eligible=(0,)))

    plan = build_team_recommendation(state, policy=POLICY)

    assert plan.blocking_reasons == ()
    assert plan.desired_assignments[0].player_id == "upcoming"


def test_lockable_live_starter_keeps_the_slot() -> None:
    state = _one_slot_state(_live(rostered=True, eligible=(0,)))

    plan = build_team_recommendation(state, policy=POLICY)

    assert plan.desired_assignments[0].player_id == "late"
