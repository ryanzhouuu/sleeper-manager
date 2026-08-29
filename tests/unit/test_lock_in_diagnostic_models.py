"""Coverage for historical Lock-In diagnostic request contracts."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from lock_in_diagnostic_support import _team_week

from sleeper_manager.backtesting.experiments.lock_in_diagnostic_models import (
    LockInDiagnosticError,
    LockInDiagnosticRequest,
)


def test_request_rejects_negative_planning_lead_time() -> None:
    """Planning cannot be scheduled after a candidate cutoff."""

    with pytest.raises(LockInDiagnosticError, match="cannot be negative"):
        LockInDiagnosticRequest(_team_week(), planning_lead_time=timedelta(seconds=-1))


def test_request_rejects_blank_policy_identity() -> None:
    """Every reproducible diagnostic run requires a stable policy name."""

    with pytest.raises(LockInDiagnosticError, match="policy name"):
        LockInDiagnosticRequest(_team_week(), policy_name="  ")


def test_request_rejects_naive_planning_cutoffs() -> None:
    """Naive cutoffs cannot be ordered safely against aware replay events."""

    with pytest.raises(LockInDiagnosticError, match="timezone-aware"):
        LockInDiagnosticRequest(_team_week(), planning_cutoffs=(datetime(2026, 2, 2, 18),))
