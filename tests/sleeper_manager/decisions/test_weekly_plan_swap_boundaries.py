"""Reject incomplete or unreachable swap targets before emitting instructions."""

from dataclasses import replace
from datetime import timedelta

import pytest

from sleeper_manager.decisions._weekly_plan_moves import plan_moves
from sleeper_manager.decisions.weekly_plan import (
    WeeklyPlanError,
    WeeklyPlanPolicyConfig,
    build_weekly_plan,
)
from sleeper_manager.domain.planning import (
    ObservedStarter,
    PlannedAssignment,
    PlanStatus,
    StarterSlot,
)
from tests.sleeper_manager.decisions.weekly_plan_support import NOW, _opportunity, _planned, _state

START = NOW + timedelta(hours=1)


@pytest.mark.parametrize(
    "desired, reason",
    (
        ((_planned(0, "G", "a"), _planned(0, "G", "b")), "duplicate or unknown slots"),
        ((_planned(0, "G", "a"), _planned(8, "UTIL", "b")), "duplicate or unknown slots"),
        ((_planned(0, "C", "a"), _planned(1, "UTIL", "b")), "slot positions"),
        ((_planned(0, "G", "a"),), "cover every open slot"),
        ((_planned(0, "G", "a"), _planned(1, "UTIL", "a")), "repeats a player"),
        ((_planned(0, "G", "a"), _planned(1, "UTIL", "missing")), "unrostered player"),
    ),
)
def test_malformed_target_is_rejected(desired: tuple[PlannedAssignment, ...], reason: str) -> None:
    state = _state(
        tuple(_opportunity(p, p, START, ("PG",), ((20, 1),)) for p in ("a", "b")),
        observed=(ObservedStarter(0, "a", ("PG",)), ObservedStarter(1, "b", ("PG",))),
    )

    with pytest.raises(WeeklyPlanError, match=reason):
        plan_moves(state, desired, START, timedelta(0))


def test_unreachable_position_cycle_produces_no_executable_instructions() -> None:
    positions = (("PG", "SG"), ("SG", "SF"), ("SF", "PG"))
    players = ("a", "b", "c")
    state = replace(
        _state(
            tuple(
                _opportunity(
                    player,
                    player,
                    START,
                    positions[i],
                    ((20, 1),),
                    eligible_slot_indices=((i + 1) % 3,),
                )
                for i, player in enumerate(players)
            ),
            observed=tuple(
                ObservedStarter(i, player, positions[i]) for i, player in enumerate(players)
            ),
            starter_slots=tuple(StarterSlot(i, slot) for i, slot in enumerate(("PG", "SG", "SF"))),
        ),
        bench_capacity=0,
    )

    plan = build_weekly_plan(
        state, policy=WeeklyPlanPolicyConfig(scenario_count=8, postgame_scenario_count=8)
    )

    assert plan.status is PlanStatus.BLOCKED
    assert plan.moves == ()
    assert plan.desired_assignments == plan.observed_assignments
