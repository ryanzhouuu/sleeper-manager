"""Coverage for deterministic Lock-In diagnostic reports and CLI output."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from lock_in_diagnostic_support import BASE, _team_week

from sleeper_manager.backtesting.experiments.lock_in_diagnostic import (
    LockInDiagnosticRequest,
    run_lock_in_diagnostic,
)
from sleeper_manager.decisions.lock_in import LockInPolicyConfig


def test_report_is_byte_identical_across_reruns(tmp_path: Path) -> None:
    from sleeper_manager.backtesting.experiments.lock_in_diagnostic_report import (
        write_diagnostic_report,
    )

    team_week = _team_week(p1_actual=30, p2_expected=1, p2_actual=1)
    request = LockInDiagnosticRequest(
        team_week,
        policy_config=LockInPolicyConfig(scenario_count=32, seed=9),
        planning_cutoffs=(BASE + timedelta(hours=3),),
    )
    execution = run_lock_in_diagnostic(request)

    first = write_diagnostic_report(tmp_path / "a", request, execution)
    second = write_diagnostic_report(tmp_path / "b", request, execution)

    assert first.json_path.read_bytes() == second.json_path.read_bytes()
    assert first.markdown_path.read_bytes() == second.markdown_path.read_bytes()
    assert first.payload["diagnostic_only"] is True
    assert first.payload["source"]["complete"] is False
    assert first.payload["source"]["logical_artifact_identity"] == (
        "team-weeks/league-1/week-01/roster-1.json"
    )
    assert "diagnostic_run_id" in first.payload
    assert "team_week_fingerprint" in first.payload["source"]
    assert "team_week_fingerprint" in first.markdown_path.read_text()
    assert first.payload["execution"]["policy_traces"]
    assert first.payload["limitations"]
