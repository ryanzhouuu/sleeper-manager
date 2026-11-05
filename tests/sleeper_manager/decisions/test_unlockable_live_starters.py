"""A live game that can no longer be locked must not reserve a slot or block advice."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from sleeper_manager.decisions.team_recommendation import build_team_recommendation
from sleeper_manager.decisions.weekly_plan import WeeklyPlanPolicyConfig
from sleeper_manager.domain.planning import (
    FixedSlot,
    GameOpportunity,
    ObservedStarter,
    PlanningGameStatus,
    PlanningReasonCode,
    StarterSlot,
    TeamWeekState,
)
from sleeper_manager.workflows.plan_rendering import serialize_weekly_plan_trace
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


def _unplaceable_state() -> TeamWeekState:
    live = _live(rostered=True, eligible=(0,))
    locked = _opportunity(
        "locked",
        "done",
        NOW - timedelta(hours=4),
        ("PG",),
        ((20, 1),),
        status=PlanningGameStatus.FINAL,
        completed_score=20,
        finalized_at=NOW - timedelta(hours=1),
        eligible_slot_indices=(0,),
    )
    upcoming = _opportunity(
        "upcoming",
        "next",
        NOW + timedelta(hours=2),
        ("PG",),
        ((30, 1),),
        eligible_slot_indices=(1,),
    )
    return _state(
        (live, locked, upcoming),
        observed=(
            ObservedStarter(0, "locked", ("PG",)),
            ObservedStarter(1, "late", ("PG",)),
        ),
        fixed=(
            FixedSlot(0, "G", "locked", "done", 20, NOW - timedelta(hours=1), "decision", "test"),
        ),
    )


def test_blocked_plan_keeps_the_planner_error_in_its_trace() -> None:
    plan = build_team_recommendation(_unplaceable_state(), policy=POLICY)

    assert plan.blocking_reasons == (PlanningReasonCode.ROSTER_STATE_MISMATCH,)
    assert plan.blocking_detail == "Active starter has no eligible open slot: late"
    trace = json.loads(serialize_weekly_plan_trace(plan))
    assert trace["blocking_detail"] == plan.blocking_detail


def test_blocking_detail_does_not_change_the_plan_identity() -> None:
    plan = build_team_recommendation(_unplaceable_state(), policy=POLICY)

    assert replace(plan, blocking_detail=None).material_hash == plan.material_hash
    assert replace(plan, blocking_detail=None).plan_id == plan.plan_id
