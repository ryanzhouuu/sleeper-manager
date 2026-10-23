"""Passing frees a starter for lineup planning without requiring a substitution."""

from dataclasses import replace
from datetime import timedelta

import pytest

from sleeper_manager.decisions.weekly_plan import WeeklyPlanPolicyConfig, build_weekly_plan
from sleeper_manager.domain.planning import (
    ObservedStarter,
    PassedOpportunity,
    PlanningGameStatus,
    PlanStatus,
    StarterSlot,
)
from tests.sleeper_manager.decisions.weekly_plan_support import NOW, _opportunity, _state


@pytest.mark.parametrize(("future_score", "selected"), ((80, "p1"), (30, "p2")))
def test_pass_allows_but_does_not_require_substitution(future_score: float, selected: str) -> None:
    state = replace(
        _state(
            (
                _opportunity(
                    "p1",
                    "completed",
                    NOW - timedelta(hours=3),
                    ("PG",),
                    ((20, 1),),
                    status=PlanningGameStatus.FINAL,
                    completed_score=20,
                    finalized_at=NOW - timedelta(hours=1),
                    eligible_slot_indices=(0,),
                ),
                _opportunity(
                    "p1",
                    "next",
                    NOW + timedelta(hours=1),
                    ("PG",),
                    ((future_score, 1),),
                    eligible_slot_indices=(0,),
                ),
                _opportunity(
                    "p2",
                    "replacement",
                    NOW + timedelta(hours=1),
                    ("PG",),
                    ((60, 1),),
                    eligible_slot_indices=(0,),
                ),
            ),
            observed=(ObservedStarter(0, "p1", ("PG",)),),
            starter_slots=(StarterSlot(0, "UTIL"),),
            passed=(PassedOpportunity("p1", "completed", NOW, "decision", "fixture"),),
        ),
        bench_capacity=1,
        week_ends_at=NOW + timedelta(days=6),
    )

    plan = build_weekly_plan(
        state, policy=WeeklyPlanPolicyConfig(scenario_count=8, postgame_scenario_count=8)
    )

    assert plan.status is not PlanStatus.BLOCKED
    assert plan.desired_assignments[0].player_id == selected
    assert bool(plan.moves) == (selected != "p1")
    assert plan.passed_opportunities == state.passed_opportunities
    assert plan.fixed_slots == ()
    assert state.observed_starters[0].player_id == "p1"
