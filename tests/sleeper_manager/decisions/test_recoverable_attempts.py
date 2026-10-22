"""Extra attempts must preserve a verifiable joint return path without invented score bonuses."""

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
    PlanningGameStatus,
    PlanningReasonCode,
    StarterSlot,
)
from tests.sleeper_manager.decisions.weekly_plan_support import NOW, _opportunity, _planned, _state

POLICY = WeeklyPlanPolicyConfig(scenario_count=32, postgame_scenario_count=8)


def _trial():
    earlier = _opportunity(
        "early",
        "tonight",
        NOW + timedelta(hours=1),
        ("PG",),
        ((10, 1), (30, 1)),
        eligible_slot_indices=(0,),
    )
    later = _opportunity(
        "later",
        "tomorrow",
        NOW + timedelta(days=1),
        ("PG",),
        ((60, 1),),
        eligible_slot_indices=(0,),
    )
    return replace(
        _state(
            (earlier, later),
            observed=(ObservedStarter(0, "later", ("PG",)),),
            starter_slots=(StarterSlot(0, "UTIL"),),
        ),
        bench_capacity=1,
    )


def test_zero_modeled_gain_still_prefers_a_verified_recoverable_attempt() -> None:
    state = _trial()
    plan = build_weekly_plan(state, policy=POLICY)
    assert plan.desired_assignments[0].player_id == "early"
    assert plan.expected_terminal_score == plan.observed_terminal_score == 60
    assert len(plan.recovery_obligations) == 1
    proof = plan.recovery_obligations[0]
    assert proof.added_player_games == (("early", "tonight"),)
    assert proof.deadline == state.opportunities[1].scheduled_start - timedelta(minutes=10)
    assert proof.recovery_moves[0].player_id == "later"


def test_unknown_capacity_can_recover_through_an_atomic_swap() -> None:
    plan = build_weekly_plan(replace(_trial(), bench_capacity=None), policy=POLICY)
    assert plan.desired_assignments[0].player_id == "early"
    assert plan.recovery_obligations[0].recovery_moves[0].swap_player_id == "early"


def test_only_the_named_trial_can_be_forfeited_during_active_recovery() -> None:
    state = _trial()
    proof = build_weekly_plan(state, policy=POLICY).recovery_obligations[0]
    active = replace(
        state,
        observed_starters=(ObservedStarter(0, "early", ("PG",)),),
        opportunities=(
            replace(state.opportunities[0], status=PlanningGameStatus.ACTIVE),
            state.opportunities[1],
        ),
        recovery_obligations=(proof,),
    )
    target = (_planned(0, "UTIL", "later"),)
    with pytest.raises(WeeklyPlanError, match="active starter"):
        plan_moves(active, target, proof.recovery_at, timedelta(minutes=10))
    moves = plan_moves(
        replace(active, recovery_forfeitures=proof.added_player_games),
        target,
        proof.recovery_at,
        timedelta(minutes=10),
    )
    assert moves[0].swap_player_id == "early"


def test_a_completed_idle_star_is_not_a_disposable_baseline_option() -> None:
    state = _trial()
    completed = replace(
        state.opportunities[1],
        game_id="old",
        scheduled_start=NOW - timedelta(hours=4),
        status=PlanningGameStatus.FINAL,
        completed_fantasy_score=60,
        finalized_at=NOW - timedelta(hours=1),
        projection=None,
        missing_projection_reason=PlanningReasonCode.MISSING_PROJECTION,
    )
    plan = build_weekly_plan(
        replace(state, opportunities=(state.opportunities[0], completed, state.opportunities[1])),
        policy=POLICY,
    )
    assert plan.recovery_obligations == ()


def test_multiple_added_attempts_share_one_combined_recovery_proof() -> None:
    games = tuple(
        _opportunity(player, game, start, ("PG",), ((points, 1),))
        for player, game, start, points in (
            ("early-a", "early-a", NOW + timedelta(hours=1), 10),
            ("early-b", "early-b", NOW + timedelta(hours=1), 10),
            ("later-a", "later-a", NOW + timedelta(days=1), 60),
            ("later-b", "later-b", NOW + timedelta(days=1), 60),
        )
    )
    state = replace(
        _state(
            games,
            starter_slots=(StarterSlot(0, "UTIL"), StarterSlot(1, "UTIL")),
            observed=(
                ObservedStarter(0, "later-a", ("PG",)),
                ObservedStarter(1, "later-b", ("PG",)),
            ),
        ),
        bench_capacity=2,
    )
    plan = build_weekly_plan(state, policy=POLICY)
    assert {row.player_id for row in plan.desired_assignments} == {"early-a", "early-b"}
    assert len(plan.recovery_obligations) == 1
    assert len(plan.recovery_obligations[0].added_player_games) == 2
    assert plan.expected_terminal_score == 120


def test_an_extra_attempt_can_share_an_nba_game_with_a_retained_starter() -> None:
    first = _opportunity("retained", "shared", NOW + timedelta(hours=1), ("PG",), ((40, 1),))
    extra = _opportunity("trial", "shared", NOW + timedelta(hours=1), ("C",), ((10, 1),))
    later = _opportunity("later", "later", NOW + timedelta(days=1), ("C",), ((60, 1),))
    state = replace(
        _state(
            (first, extra, later),
            observed=(ObservedStarter(0, "retained", ("PG",)), ObservedStarter(1, "later", ("C",))),
        ),
        bench_capacity=1,
    )
    plan = build_weekly_plan(state, policy=POLICY)
    assert {row.player_id for row in plan.desired_assignments} == {"retained", "trial"}
    assert plan.recovery_obligations[0].added_player_games == (("trial", "shared"),)
    assert plan.expected_terminal_score == 100


def test_reserves_do_not_create_attempts_or_inflate_future_team_value() -> None:
    state = replace(_trial(), reserve_player_ids=("early",))
    plan = build_weekly_plan(state, policy=POLICY)
    assert plan.desired_assignments[0].player_id == "later"
    assert plan.recovery_obligations == ()
    assert plan.expected_terminal_score == 60
