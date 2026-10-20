"""Prevent batch-only optimization from becoming destructive full-lineup advice."""

from dataclasses import replace
from datetime import timedelta

from sleeper_manager.decisions.weekly_plan import (
    WeeklyPlanPolicyConfig,
    build_weekly_plan,
    score_weekly_options,
)
from sleeper_manager.domain.planning import ObservedStarter, PlanStatus, StarterSlot
from tests.sleeper_manager.decisions.weekly_plan_support import NOW, _opportunity, _state

POLICY = WeeklyPlanPolicyConfig(scenario_count=16, postgame_scenario_count=16)


def test_staggered_starters_require_no_moves_or_extra_scoring() -> None:
    state = _state(
        (
            _opportunity("early", "early", NOW + timedelta(hours=1), ("PG",), ((20, 1),)),
            _opportunity("later", "later", NOW + timedelta(hours=2), ("C",), ((30, 1),)),
        ),
        observed=(ObservedStarter(0, "early", ("PG",)), ObservedStarter(1, "later", ("C",))),
    )

    plan = build_weekly_plan(state, policy=POLICY)
    decision = score_weekly_options(state, config=POLICY)

    assert plan.status is PlanStatus.NO_ACTION
    assert plan.moves == ()
    assert plan.desired_assignments == plan.observed_assignments
    assert plan.expected_terminal_score == plan.observed_terminal_score == 50
    assert decision.selected.move_count == 0
    assert decision.selected.retained_observed_count == 2


def test_first_tipoff_does_not_clear_eight_later_starters() -> None:
    slots = tuple(StarterSlot(i, "UTIL") for i in range(9))
    state = _state(
        tuple(
            _opportunity(
                f"p{i}",
                f"g{i}",
                NOW + timedelta(hours=1 + i),
                ("PG",),
                ((20 + i, 1),),
                eligible_slot_indices=tuple(range(9)),
            )
            for i in range(9)
        ),
        observed=tuple(ObservedStarter(i, f"p{i}", ("PG",)) for i in range(9)),
        starter_slots=slots,
    )

    plan = build_weekly_plan(state, policy=POLICY)

    assert plan.desired_assignments == plan.observed_assignments
    assert plan.moves == ()
    assert plan.expected_terminal_score == 216


def test_valuable_center_util_swap_preserves_both_starters() -> None:
    state = _state(
        (
            _opportunity("early", "early", NOW + timedelta(hours=1), ("C",), ((60, 1),)),
            _opportunity("later", "later", NOW + timedelta(hours=2), ("C",), ((30, 1),)),
            _opportunity("guard", "later", NOW + timedelta(hours=2), ("PG",), ((100, 1),)),
        ),
        observed=(ObservedStarter(0, "later", ("C",)), ObservedStarter(1, "early", ("C",))),
        starter_slots=(StarterSlot(0, "C"), StarterSlot(1, "UTIL")),
    )

    plan = build_weekly_plan(state, policy=POLICY)

    assert tuple(item.player_id for item in plan.desired_assignments) == ("early", "later")
    assert plan.expected_terminal_score == 160
    assert plan.observed_terminal_score == 130
    assert len(plan.moves) == 1
    assert {plan.moves[0].player_id, plan.moves[0].swap_player_id} == {"early", "later"}


def test_selected_bench_player_can_still_replace_a_later_starter() -> None:
    state = _state(
        (
            _opportunity("early", "early", NOW + timedelta(hours=1), ("PG",), ((40, 1),)),
            _opportunity("later", "later", NOW + timedelta(hours=2), ("PG",), ((10, 1),)),
        ),
        observed=(ObservedStarter(0, "later", ("PG",)),),
        starter_slots=(StarterSlot(0, "G"),),
    )

    plan = build_weekly_plan(state, policy=POLICY)

    assert tuple(item.player_id for item in plan.desired_assignments) == ("early",)
    assert [(move.player_id, move.target_slot_index) for move in plan.moves] == [
        ("early", 0),
    ]
    assert plan.moves[0].swap_player_id == "later"


def test_completion_does_not_restore_a_deliberately_skipped_batch_player() -> None:
    state = _state(
        tuple(
            _opportunity(player, "early", NOW + timedelta(hours=1), ("PG",), ((value, 1),))
            for player, value in (("skip", 5), ("selected", 40))
        ),
        observed=(ObservedStarter(0, "skip", ("PG",)),),
        starter_slots=(StarterSlot(0, "G"),),
    )

    plan = build_weekly_plan(state, policy=POLICY)

    assert tuple(item.player_id for item in plan.desired_assignments) == ("selected",)
    assert [(move.player_id, move.target_slot_index) for move in plan.moves] == [
        ("selected", 0),
    ]
    assert plan.moves[0].swap_player_id == "skip"


def test_starter_without_remaining_opportunities_is_not_silently_removed() -> None:
    state = _state(
        (_opportunity("early", "early", NOW + timedelta(hours=1), ("PG",), ((20, 1),)),),
    )
    state = replace(
        state,
        roster_player_ids=("early", "finished"),
        observed_starters=(
            ObservedStarter(0, "early", ("PG",)),
            ObservedStarter(1, "finished", ("C",)),
        ),
    )

    plan = build_weekly_plan(state, policy=POLICY)

    assert plan.desired_assignments == plan.observed_assignments
    assert plan.status is PlanStatus.NO_ACTION
