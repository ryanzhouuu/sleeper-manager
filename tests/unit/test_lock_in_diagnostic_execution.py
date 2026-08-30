"""Coverage for top-level historical Lock-In diagnostic execution."""

from __future__ import annotations

from datetime import timedelta

import pytest
from lock_in_diagnostic_support import BASE, _team_week

from sleeper_manager.backtesting.experiments.lock_in_diagnostic import (
    LockInDiagnosticError,
    LockInDiagnosticRequest,
    run_lock_in_diagnostic,
)
from sleeper_manager.decisions.lock_in import LockInPolicyConfig


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


def test_infeasible_oracle_deadline_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from sleeper_manager.backtesting.experiments import lock_in_diagnostic as module

    team_week = _team_week(p1_actual=30, p2_expected=1, p2_actual=1)
    monkeypatch.setattr(
        module,
        "oracle_feasibility_checks",
        lambda oracle, tw: (
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
