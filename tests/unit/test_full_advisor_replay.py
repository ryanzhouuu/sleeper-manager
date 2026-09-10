"""End-to-end coverage for counterfactual weekly lineup and Lock-In replay."""

from __future__ import annotations

from datetime import timedelta

import pytest
from lock_in_diagnostic_support import BASE, _team_week

from sleeper_manager.backtesting.experiments.full_advisor_replay import (
    FullAdvisorReplayError,
    FullAdvisorReplayRequest,
    run_full_advisor_replay,
)
from sleeper_manager.decisions.lock_in import LockInPolicyConfig
from sleeper_manager.decisions.weekly_plan import WeeklyPlanPolicyConfig


def _request(**team_week_overrides: object) -> FullAdvisorReplayRequest:
    """Build a small deterministic current/current replay request."""

    return FullAdvisorReplayRequest(
        team_week=_team_week(**team_week_overrides),
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


def test_full_replay_rejects_future_projection_at_the_first_cutoff() -> None:
    """Fail closed instead of using a later pregame projection in an earlier plan."""

    request = _request(future_projection_available_as_of=BASE + timedelta(hours=10))

    with pytest.raises(FullAdvisorReplayError, match="projection_after_decision"):
        run_full_advisor_replay(request)
