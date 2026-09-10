"""Coverage for top-level historical Lock-In diagnostic execution."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from lock_in_diagnostic_support import BASE, _player_game, _team_week

from sleeper_manager.backtesting.experiments.lock_in_diagnostic import (
    LockInDiagnosticError,
    LockInDiagnosticRequest,
    run_lock_in_diagnostic,
)
from sleeper_manager.backtesting.experiments.lock_in_diagnostic_adapter import (
    DiagnosticPolicyAdapter,
)
from sleeper_manager.backtesting.replay.engine import ReplayError
from sleeper_manager.backtesting.replay.inputs.models import (
    HistoricalTeamWeekInput,
    ReplayCoverageSummary,
)
from sleeper_manager.backtesting.replay.models import ReplayGame, ReplayGameStatus
from sleeper_manager.decisions.lock_in import LockInPolicyConfig
from sleeper_manager.domain.planning import PlanningQuality


def test_diagnostic_locks_and_passes_with_stable_batches_and_full_traces() -> None:
    team_week = _team_week(p1_actual=30, p2_expected=1, p2_actual=1)
    result = run_lock_in_diagnostic(
        LockInDiagnosticRequest(
            team_week,
            policy_config=LockInPolicyConfig(scenario_count=64, seed=11),
            planning_cutoffs=(BASE + timedelta(hours=3),),
        )
    )

    assert result.status == "success"
    assert result.model_result is not None
    assert result.oracle_result is not None
    assert result.comparison is not None
    assert result.comparison.lock_in_regret >= 0
    assert all(flag for _, flag in result.comparison.invariant_results)
    assert result.batches
    assert result.evaluation_order
    assert result.automatic_assignments
    kinds = {trace.decision.kind for trace in result.policy_traces}
    assert "lock" in kinds or "pass" in kinds
    for trace in result.policy_traces:
        decision = trace.decision
        replay = next(
            item
            for item in result.model_result.decisions
            if item.player_id == decision.player_id and item.game_id == decision.game_id
        )
        assert replay is decision
        assert replay.expected_terminal_score == decision.expected_terminal_score
        assert replay.counterfactual_value == decision.counterfactual_value
        assert replay.information_version == decision.information_version
        assert replay.reason == decision.reason


def test_all_deferred_run_is_blocked_without_success_comparison() -> None:
    team_week = _team_week(
        future_projection_available_as_of=BASE + timedelta(hours=10),
        include_second_p1_game=False,
    )
    result = run_lock_in_diagnostic(
        LockInDiagnosticRequest(
            team_week,
            policy_config=LockInPolicyConfig(scenario_count=16, seed=1),
            planning_cutoffs=(BASE + timedelta(hours=3),),
        )
    )

    assert result.status == "blocked_no_evaluable_candidate"
    assert result.model_result is None
    assert result.comparison is None
    assert result.deferrals
    assert any(item.terminal for item in result.deferrals)


def test_restricted_diagnostic_filters_roster_members_to_observed_starters() -> None:
    """Keep historical-start diagnostics restricted after membership is persisted separately."""

    team_week = _team_week(p1_actual=30, p2_expected=1, p2_actual=1)
    bench_game = replace(
        _player_game(
            "bench",
            "g1",
            12,
            expected=12,
            available_as_of=BASE - timedelta(hours=1),
        ),
        rostered_at_tipoff=True,
    )
    restricted = DiagnosticPolicyAdapter(
        LockInDiagnosticRequest(
            replace(team_week, player_games=(*team_week.player_games, bench_game))
        )
    )

    membership = {
        player_game.sleeper_id: player_game.rostered_at_tipoff
        for player_game in restricted.state.player_games
    }
    assert membership["bench"] is False
    assert membership["p1"] is True
    assert membership["p2"] is True


def test_infeasible_oracle_deadline_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from sleeper_manager.backtesting.experiments import lock_in_diagnostic as module

    team_week = _team_week(p1_actual=30, p2_expected=1, p2_actual=1)
    monkeypatch.setattr(
        module,
        "oracle_feasibility_checks",
        lambda oracle, state: (
            module.OracleFeasibilityCheck(
                "p1",
                "g1",
                0,
                "lockable_earlier",
                False,
                "finalized after next rostered start",
            ),
        ),
    )

    with pytest.raises(LockInDiagnosticError, match="temporally infeasible"):
        run_lock_in_diagnostic(
            LockInDiagnosticRequest(
                team_week,
                policy_config=LockInPolicyConfig(scenario_count=32, seed=2),
                planning_cutoffs=(BASE + timedelta(hours=3),),
            )
        )


def _zero_oracle_team_week() -> HistoricalTeamWeekInput:
    """Build a two-starter fixture whose oracle and model scores are both zero."""

    games = (
        ReplayGame(
            "g1",
            BASE,
            BASE + timedelta(hours=2),
            1,
            ("home", "away"),
            ReplayGameStatus.FINAL,
        ),
        ReplayGame(
            "g2",
            BASE + timedelta(hours=6),
            BASE + timedelta(hours=8),
            1,
            ("home", "away"),
            ReplayGameStatus.FINAL,
        ),
    )
    player_games = (
        _player_game("p1", "g1", 0, expected=0, available_as_of=BASE - timedelta(hours=1)),
        _player_game("p2", "g2", 0, expected=0, available_as_of=BASE - timedelta(hours=1)),
    )
    return HistoricalTeamWeekInput(
        manifest_id="manifest-zero-oracle",
        league_id="league-1",
        season="2026",
        week=1,
        roster_id=1,
        starter_slots=("UTIL", "UTIL"),
        roster_player_ids=("p1", "p2"),
        observed_starter_ids=("p1", "p2"),
        games=games,
        player_games=player_games,
        eligibility_quality=PlanningQuality.BEST_KNOWN_CONSTRAINTS_ORACLE,
        coverage=ReplayCoverageSummary(
            expected_player_games=2,
            joined_player_games=2,
            resolved_identities=2,
            exact_eligibility=0,
            best_known_eligibility=2,
            scored_player_games=2,
            projected_player_games=2,
        ),
    )


def test_zero_oracle_week_reports_null_score_capture() -> None:
    """Keep score capture undefined when the constrained oracle score is zero."""

    result = run_lock_in_diagnostic(
        LockInDiagnosticRequest(
            _zero_oracle_team_week(),
            policy_config=LockInPolicyConfig(scenario_count=32, seed=4),
            planning_cutoffs=(BASE + timedelta(hours=3),),
        )
    )

    assert result.status == "success"
    assert result.comparison is not None
    assert result.comparison.oracle_team_score == 0
    assert result.comparison.lock_in_regret == 0
    assert result.comparison.score_capture is None
    assert all(flag for _, flag in result.comparison.invariant_results)


def test_model_above_oracle_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject comparisons where the model score exceeds the constrained oracle."""

    from sleeper_manager.backtesting.experiments import lock_in_diagnostic as module
    from sleeper_manager.backtesting.replay.models import TeamWeekReplayResult

    team_week = _team_week(p1_actual=30, p2_expected=1, p2_actual=1)

    def low_oracle(state):  # type: ignore[no-untyped-def]
        return TeamWeekReplayResult(
            league_id=state.league_id,
            week=state.week,
            roster_id=state.roster_id,
            policy_name="oracle",
            realized_score=1,
            decisions=(),
            locked_slots=(),
            automatic_final_scores=(),
            eligibility_quality=state.eligibility_quality.value,
            data_quality="partial",
        )

    monkeypatch.setattr(module, "oracle_from_planning_state", low_oracle)

    with pytest.raises(ReplayError, match="Model policy exceeded the oracle"):
        run_lock_in_diagnostic(
            LockInDiagnosticRequest(
                team_week,
                policy_config=LockInPolicyConfig(scenario_count=32, seed=5),
                planning_cutoffs=(BASE + timedelta(hours=3),),
            )
        )
