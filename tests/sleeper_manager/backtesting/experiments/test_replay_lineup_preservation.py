"""Keep later starters across successive simulated tipoff decisions."""

from dataclasses import replace
from datetime import timedelta

from sleeper_manager.backtesting.experiments.full_advisor_replay import (
    FullAdvisorReplayRequest,
    run_full_advisor_replay,
)
from sleeper_manager.backtesting.replay.projection_surface import full_advisor_planning_cutoffs
from sleeper_manager.decisions.lock_in import LockInPolicyConfig
from sleeper_manager.decisions.weekly_plan import WeeklyPlanPolicyConfig
from tests.sleeper_manager.backtesting.experiments.lock_in_diagnostic_support import (
    BASE,
    _player_game,
    _projection_surface,
    _team_week,
)


def test_replay_preserves_a_later_starter_after_passing_their_first_game() -> None:
    team_week = _team_week()
    player_games = tuple(
        _player_game(
            player, game, actual, expected=expected, available_as_of=BASE - timedelta(hours=1)
        )
        for player, game, actual, expected in (
            ("p1", "g1", 1, 60),
            ("p2", "g1", 1, 60),
            ("p1", "g2", 40, 40),
            ("p2", "g3", 40, 40),
        )
    )
    team_week = replace(
        team_week,
        player_games=player_games,
        coverage=replace(
            team_week.coverage,
            expected_player_games=4,
            joined_player_games=4,
            resolved_identities=4,
            best_known_eligibility=4,
            scored_player_games=4,
            projected_player_games=4,
        ),
    )
    request = FullAdvisorReplayRequest(
        team_week=team_week,
        projection_surface=_projection_surface(team_week),
        weekly_policy_config=WeeklyPlanPolicyConfig(scenario_count=16, postgame_scenario_count=16),
        lock_in_policy_config=LockInPolicyConfig(scenario_count=16),
        minimum_confidence=0,
    )

    execution = run_full_advisor_replay(request)

    second_cutoff = full_advisor_planning_cutoffs(team_week)[1]
    second_plan = next(plan for plan in execution.plans if plan.decision_time == second_cutoff)
    assert {item.player_id for item in second_plan.observed_assignments} == {"p1", "p2"}
    assert second_plan.desired_assignments == second_plan.observed_assignments
    assert second_plan.moves == ()
    assert {(item.player_id, item.game_id) for item in execution.started_player_games} == {
        ("p1", "g1"),
        ("p2", "g1"),
        ("p1", "g2"),
        ("p2", "g3"),
    }
    assert execution.model_result.realized_score == 80
    assert all(passed for _, passed in execution.invariant_results)
