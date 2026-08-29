"""Compatibility exports for historical Lock-In diagnostic execution helpers."""

from sleeper_manager.backtesting.experiments.lock_in_diagnostic_adapter import (
    DiagnosticPolicyAdapter,
    replay_config,
)
from sleeper_manager.backtesting.experiments.lock_in_diagnostic_oracle import (
    oracle_feasibility_checks,
)
from sleeper_manager.backtesting.experiments.lock_in_diagnostic_results import (
    build_model_result,
    legal_automatic_assignments,
)
from sleeper_manager.decisions.lock_in import ScoreMaximizingLockInPolicy

__all__ = (
    "DiagnosticPolicyAdapter",
    "ScoreMaximizingLockInPolicy",
    "build_model_result",
    "legal_automatic_assignments",
    "oracle_feasibility_checks",
    "replay_config",
)
