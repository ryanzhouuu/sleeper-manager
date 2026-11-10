"""A win-chance objective trades expected points for the chance of beating this opponent."""

from dataclasses import replace
from datetime import timedelta

import pytest

from sleeper_manager.decisions.team_recommendation import build_team_recommendation
from sleeper_manager.decisions.weekly_plan import build_weekly_plan
from sleeper_manager.decisions.win_probability import Finish, WinObjective
from sleeper_manager.domain.lock_in import LockInDecisionKind
from sleeper_manager.domain.planning import ObservedStarter, PlanningGameStatus, StarterSlot
from tests.sleeper_manager.decisions.test_recoverable_attempts import POLICY
from tests.sleeper_manager.decisions.weekly_plan_support import NOW, _opportunity, _state

PLAN_POLICY = replace(
    POLICY, scenario_count=256, postgame_scenario_count=64, minimum_lock_in_confidence=0.4
)


def _safe_score_or_coin_flip():
    """A 30-point final, or pass for a later game worth 0 or 70 (35 expected)."""
    final = _opportunity(
        "held",
        "done",
        NOW - timedelta(hours=3),
        ("PG",),
        ((30, 1),),
        status=PlanningGameStatus.FINAL,
        completed_score=30,
        finalized_at=NOW - timedelta(hours=1),
        eligible_slot_indices=(0,),
    )
    later = _opportunity(
        "held",
        "later",
        NOW + timedelta(days=1),
        ("PG",),
        ((0, 1), (70, 1)),
        eligible_slot_indices=(0,),
    )
    return replace(
        _state(
            (final, later),
            starter_slots=(StarterSlot(0, "UTIL"),),
            observed=(ObservedStarter(0, "held", ("PG",)),),
        ),
        week_ends_at=NOW + timedelta(days=6),
    )


def _decision(objective: WinObjective | None) -> LockInDecisionKind:
    plan = build_team_recommendation(
        _safe_score_or_coin_flip(), policy=replace(PLAN_POLICY, win_objective=objective)
    )
    return plan.score_actions[0].kind


def test_expected_points_pass_the_safe_score() -> None:
    assert _decision(None) is LockInDecisionKind.PASS


def test_a_lead_locks_the_score_that_wins() -> None:
    assert _decision(WinObjective(Finish(25.0, 0.0))) is LockInDecisionKind.LOCK


def test_a_deficit_passes_for_the_score_that_can_still_win() -> None:
    assert _decision(WinObjective(Finish(50.0, 0.0))) is LockInDecisionKind.PASS


def test_an_out_of_reach_opponent_leaves_the_choice_to_points() -> None:
    assert _decision(WinObjective(Finish(500.0, 0.0))) is LockInDecisionKind.PASS


@pytest.mark.parametrize(("opponent", "expected"), [(25.0, 30.0), (50.0, 35.0)])
def test_the_plan_still_reports_expected_points(opponent: float, expected: float) -> None:
    plan = build_team_recommendation(
        _safe_score_or_coin_flip(),
        policy=replace(PLAN_POLICY, win_objective=WinObjective(Finish(opponent, 0.0))),
    )

    assert plan.expected_terminal_score == pytest.approx(expected, abs=6)


def _steady_or_boom_start():
    """One open slot: a certain 30 or a 0-or-70 game (35 expected), both tipping off soon."""
    start = NOW + timedelta(hours=1)
    return _state(
        (
            _opportunity("steady", "g1", start, ("PG",), ((30, 1),)),
            _opportunity("boom", "g2", start, ("PG",), ((0, 1), (70, 1))),
        ),
        observed=(ObservedStarter(0, "steady", ("PG",)),),
        starter_slots=(StarterSlot(0, "G"),),
    )


def _starter(objective: WinObjective | None) -> str | None:
    plan = build_weekly_plan(
        _steady_or_boom_start(), policy=replace(PLAN_POLICY, win_objective=objective)
    )
    return plan.desired_assignments[0].player_id


def test_expected_points_start_the_higher_mean() -> None:
    assert _starter(None) == "boom"


def test_a_lead_starts_the_certain_score() -> None:
    assert _starter(WinObjective(Finish(25.0, 0.0))) == "steady"


def test_a_deficit_starts_the_player_who_can_still_win() -> None:
    assert _starter(WinObjective(Finish(50.0, 0.0))) == "boom"
