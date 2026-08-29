"""Coverage for diagnostic automatic assignments and model result construction."""

from __future__ import annotations

from datetime import timedelta

from lock_in_diagnostic_support import BASE, _team_week

from sleeper_manager.backtesting.experiments.lock_in_diagnostic_adapter import (
    DiagnosticPolicyAdapter,
)
from sleeper_manager.backtesting.experiments.lock_in_diagnostic_models import (
    LockInDiagnosticRequest,
)
from sleeper_manager.backtesting.experiments.lock_in_diagnostic_results import (
    build_model_result,
    legal_automatic_assignments,
)
from sleeper_manager.decisions.lock_in import LockInPolicyConfig


def test_results_assign_negative_automatic_scores_with_full_cardinality() -> None:
    """Fill every open starter slot even when the final legal score is negative."""

    request = LockInDiagnosticRequest(
        _team_week(p1_actual=30, p2_expected=1, p2_actual=-4),
        policy_config=LockInPolicyConfig(scenario_count=64, seed=3),
        planning_cutoffs=(BASE + timedelta(hours=3),),
    )
    adapter = DiagnosticPolicyAdapter(request)
    adapter.run()

    automatic = legal_automatic_assignments(adapter.state, request.team_week)
    model = build_model_result(request, adapter, automatic)

    assert any(item.score < 0 for item in automatic)
    assert model.realized_score == 26
    covered = {item.slot_index for item in automatic} | {
        slot.slot_index for slot in model.locked_slots
    }
    assert covered == {0, 1}
