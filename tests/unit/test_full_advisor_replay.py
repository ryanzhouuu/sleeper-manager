"""End-to-end coverage for counterfactual weekly lineup and Lock-In replay."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from lock_in_diagnostic_support import BASE, _projection_surface, _team_week

from sleeper_manager.backtesting.experiments.full_advisor_replay import (
    FullAdvisorReplayError,
    FullAdvisorReplayRequest,
    run_full_advisor_replay,
)
from sleeper_manager.backtesting.replay.projection_surface import full_advisor_planning_cutoffs
from sleeper_manager.decisions.lock_in import LockInPolicyConfig
from sleeper_manager.decisions.weekly_plan import WeeklyPlanPolicyConfig


def _request(**team_week_overrides: object) -> FullAdvisorReplayRequest:
    """Build a small deterministic current/current replay request."""

    team_week = _team_week(**team_week_overrides)
    return FullAdvisorReplayRequest(
        team_week=team_week,
        projection_surface=_projection_surface(team_week),
        weekly_policy_config=WeeklyPlanPolicyConfig(scenario_count=32, seed=0),
        lock_in_policy_config=LockInPolicyConfig(scenario_count=32, seed=0),
        minimum_confidence=0,
    )


def test_full_replay_builds_lineups_and_scores_a_legal_week() -> None:
    """Choose simulated starters, evaluate their games, and finish below the oracle."""

    first = run_full_advisor_replay(_request(p1_actual=30, p2_expected=8, p2_actual=8))
    second = run_full_advisor_replay(_request(p1_actual=30, p2_expected=8, p2_actual=8))

    assert first.to_dict() == second.to_dict()
    assert first.status == "success"
    assert first.plans
    assert first.lineup_traces
    assert first.started_player_games
    assert first.model_result.realized_score <= first.oracle_result.realized_score
    assert all(passed for _, passed in first.invariant_results)


def test_full_replay_ignores_bundle_projection_timestamps() -> None:
    """Use the explicit cutoff surface instead of bundle diagnostic projections."""

    request = _request(future_projection_available_as_of=BASE + timedelta(hours=10))

    assert run_full_advisor_replay(request).status == "success"


def test_full_replay_rejects_recorded_surface_failure() -> None:
    """Fail closed when the exact first-cutoff projection could not be generated."""

    team_week = _team_week()
    first_cutoff = full_advisor_planning_cutoffs(team_week)[0]
    request = FullAdvisorReplayRequest(
        team_week=team_week,
        projection_surface=_projection_surface(
            team_week,
            fail_key=(first_cutoff, "p1", "g1"),
        ),
        weekly_policy_config=WeeklyPlanPolicyConfig(scenario_count=32, seed=0),
        lock_in_policy_config=LockInPolicyConfig(scenario_count=32, seed=0),
        minimum_confidence=0,
    )

    with pytest.raises(FullAdvisorReplayError, match="projection_surface_failure"):
        run_full_advisor_replay(request)


def test_full_replay_preserves_a_starter_during_an_overlapping_game() -> None:
    """Keep the first game's starter eligible while planning the second tipoff."""

    team_week = _team_week(include_second_p1_game=False)
    overlapping_second = replace(
        team_week.games[1],
        start_time=BASE + timedelta(hours=1),
        final_time=BASE + timedelta(hours=3),
    )
    team_week = replace(team_week, games=(team_week.games[0], overlapping_second))
    request = FullAdvisorReplayRequest(
        team_week=team_week,
        projection_surface=_projection_surface(team_week),
        weekly_policy_config=WeeklyPlanPolicyConfig(scenario_count=32, seed=0),
        lock_in_policy_config=LockInPolicyConfig(scenario_count=32, seed=0),
        minimum_confidence=0,
    )

    execution = run_full_advisor_replay(request)

    assert execution.status == "success"
    assert any(
        {player for _, player in trace.assignments} == {"p1", "p2"}
        for trace in execution.lineup_traces
    )
