"""Coverage for chronological Lock-In diagnostic policy adaptation."""

from __future__ import annotations

from datetime import timedelta

import pytest
from lock_in_diagnostic_support import BASE, _team_week

from sleeper_manager.backtesting.experiments.lock_in_diagnostic_adapter import (
    DiagnosticPolicyAdapter,
)
from sleeper_manager.backtesting.experiments.lock_in_diagnostic_models import (
    LockInDiagnosticRequest,
)
from sleeper_manager.decisions.lock_in import (
    LockInPolicyConfig,
    ScoreMaximizingLockInPolicy,
    decision_critical_opportunities,
)


def test_adapter_defers_until_complete_as_of_opportunity_set_is_projected() -> None:
    """Defer a visible outcome until every decision-critical projection is available."""

    early = BASE + timedelta(hours=3)
    late = BASE + timedelta(hours=5, minutes=30)
    adapter = DiagnosticPolicyAdapter(
        LockInDiagnosticRequest(
            _team_week(future_projection_available_as_of=BASE + timedelta(hours=5)),
            policy_config=LockInPolicyConfig(scenario_count=32, seed=7),
            planning_cutoffs=(early, late),
        )
    )

    adapter.run()

    assert any(not item.terminal for item in adapter.deferrals)
    assert any(item.missing_opportunity_keys for item in adapter.deferrals if not item.terminal)
    assert adapter.policy_traces
    assert adapter.policy_traces[0].decision.decision_time == late


def test_adapter_never_invokes_policy_with_reduced_future_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep unavailable future projections visible instead of pruning opportunities."""

    team_week = _team_week(future_projection_available_as_of=BASE + timedelta(hours=5))
    seen_remaining: list[tuple[str, ...]] = []
    original = ScoreMaximizingLockInPolicy.decide_after_game

    def wrapped(self, state, completed):  # type: ignore[no-untyped-def]
        for opportunity in state.opportunities:
            if opportunity.projection is not None:
                assert opportunity.projection.available_as_of <= state.decision_time
        remaining = decision_critical_opportunities(state, completed)
        keys = tuple(f"{item.sleeper_player_id}:{item.game_id}" for item in remaining)
        seen_remaining.append(keys)
        assert "p2:g2" in keys
        return original(self, state, completed)

    monkeypatch.setattr(ScoreMaximizingLockInPolicy, "decide_after_game", wrapped)
    adapter = DiagnosticPolicyAdapter(
        LockInDiagnosticRequest(
            team_week,
            policy_config=LockInPolicyConfig(scenario_count=32, seed=7),
            planning_cutoffs=(
                BASE + timedelta(hours=3),
                BASE + timedelta(hours=5, minutes=30),
            ),
        )
    )

    adapter.run()

    assert adapter.policy_traces
    assert seen_remaining


def test_adapter_does_not_reconsider_passed_candidates() -> None:
    """A pass is terminal even when a later planning cutoff is configured."""

    adapter = DiagnosticPolicyAdapter(
        LockInDiagnosticRequest(
            _team_week(p1_actual=3, p2_expected=40, p2_actual=40),
            policy_config=LockInPolicyConfig(scenario_count=64, seed=5),
            planning_cutoffs=(
                BASE + timedelta(hours=3),
                BASE + timedelta(hours=4),
            ),
        )
    )

    adapter.run()

    pass_ids = [
        trace.candidate_id for trace in adapter.policy_traces if trace.decision.kind == "pass"
    ]
    assert pass_ids
    assert adapter.evaluation_order.count(pass_ids[0]) == 1
