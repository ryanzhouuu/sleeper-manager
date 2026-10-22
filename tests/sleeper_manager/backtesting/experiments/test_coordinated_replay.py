"""Chronological replay takes recoverable chances and records active trial forfeitures."""

from dataclasses import replace
from datetime import timedelta

import pytest

from sleeper_manager.backtesting.experiments.full_advisor_replay import (
    FullAdvisorReplayRequest,
    run_full_advisor_replay,
)
from sleeper_manager.decisions.lock_in import LockInPolicyConfig
from sleeper_manager.decisions.weekly_plan import WeeklyPlanPolicyConfig
from sleeper_manager.domain.projection import ProjectionDistribution
from tests.sleeper_manager.backtesting.experiments.lock_in_diagnostic_support import (
    BASE,
    _projection_surface,
    _team_week,
)
from tests.sleeper_manager.backtesting.experiments.test_full_advisor_replay import _request


def test_finalization_honors_custom_lock_in_simulation_settings(monkeypatch) -> None:
    from sleeper_manager.backtesting.experiments import full_advisor_replay

    original = full_advisor_replay.build_team_recommendation
    configurations = []

    def capture(state, **kwargs):
        configurations.append(kwargs["policy"])
        return original(state, **kwargs)

    monkeypatch.setattr(full_advisor_replay, "build_team_recommendation", capture)
    request = replace(
        _request(),
        lock_in_policy_config=LockInPolicyConfig(scenario_count=7, seed=19, tie_tolerance=0.25),
    )
    run_full_advisor_replay(request)
    assert any(config.scenario_count == 32 and config.seed == 0 for config in configurations)
    assert any(
        config.scenario_count == config.postgame_scenario_count == 7
        and config.seed == 19
        and config.tie_tolerance == 0.25
        for config in configurations
    )


@pytest.mark.parametrize("overlapping", (False, True))
def test_zero_gain_attempt_preserves_later_points_without_reviving_a_forfeited_game(
    overlapping: bool,
) -> None:
    team = _team_week(p1_actual=5, p2_expected=60, p2_actual=60, include_second_p1_game=False)
    games = (
        (replace(team.games[0], final_time=BASE + timedelta(hours=7)), team.games[1])
        if overlapping
        else team.games
    )
    players = tuple(
        replace(
            row,
            projection=replace(
                row.projection,
                distribution=ProjectionDistribution.from_weighted_observations(((10, 1),)),
            ),
        )
        if row.sleeper_id == "p1"
        else row
        for row in team.player_games
    )
    team = replace(
        team,
        starter_slots=("UTIL",),
        observed_starter_ids=("p2",),
        games=games,
        player_games=players,
    )
    request = FullAdvisorReplayRequest(
        team,
        _projection_surface(team),
        weekly_policy_config=WeeklyPlanPolicyConfig(scenario_count=64, postgame_scenario_count=16),
        lock_in_policy_config=LockInPolicyConfig(scenario_count=64),
    )
    first = run_full_advisor_replay(request)
    second = run_full_advisor_replay(request)
    assert first.to_dict() == second.to_dict()
    assert {(row.player_id, row.game_id) for row in first.started_player_games} == {
        ("p1", "g1"),
        ("p2", "g2"),
    }
    assert first.model_result.realized_score == 60
    assert any(plan.forfeited_opportunities == (("p1", "g1"),) for plan in first.plans)
    assert all(passed for _, passed in first.invariant_results)
    assert all(slot.player_id != "p1" for slot in first.model_result.locked_slots)
