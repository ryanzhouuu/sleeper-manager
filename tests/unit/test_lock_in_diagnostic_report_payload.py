"""Coverage for deterministic Lock-In diagnostic report identities."""

from __future__ import annotations

from dataclasses import replace

from lock_in_diagnostic_support import _team_week

from sleeper_manager.backtesting.experiments.lock_in_diagnostic import run_lock_in_diagnostic
from sleeper_manager.backtesting.experiments.lock_in_diagnostic_models import (
    LockInDiagnosticRequest,
)
from sleeper_manager.backtesting.experiments.lock_in_diagnostic_report_payload import (
    build_diagnostic_report_payload,
    diagnostic_run_id,
)
from sleeper_manager.decisions.lock_in import LockInPolicyConfig


def test_diagnostic_run_id_changes_with_policy_configuration() -> None:
    """Policy configuration changes must select a distinct output directory."""

    team_week = _team_week()
    first = diagnostic_run_id(
        LockInDiagnosticRequest(
            team_week,
            policy_config=LockInPolicyConfig(scenario_count=32, seed=1),
        )
    )
    second = diagnostic_run_id(
        LockInDiagnosticRequest(
            team_week,
            policy_config=LockInPolicyConfig(scenario_count=64, seed=1),
        )
    )
    assert first != second


def test_diagnostic_run_id_changes_with_team_week_content() -> None:
    """Different team-week evidence cannot reuse one report output identity."""

    team_week = _team_week()
    player_games = list(team_week.player_games)
    player_games[0] = replace(player_games[0], actual_score=player_games[0].actual_score + 1)
    changed = replace(team_week, player_games=tuple(player_games))

    request = LockInDiagnosticRequest(team_week)
    changed_request = LockInDiagnosticRequest(changed)

    assert diagnostic_run_id(request) != diagnostic_run_id(changed_request)


def test_v2_policy_payload_omits_retired_fixture_scenario_count() -> None:
    """Keep the report schema aligned with the single production scenario count."""

    request = LockInDiagnosticRequest(
        _team_week(),
        policy_config=LockInPolicyConfig(scenario_count=32, seed=1),
    )
    payload = build_diagnostic_report_payload(request, run_lock_in_diagnostic(request))

    assert payload["report_schema_version"] == "lock-in-diagnostic-report-v2"
    assert "fixture_scenario_count" not in payload["policy"]
