"""Parse, execute, and report one historical Lock-In diagnostic command."""

from __future__ import annotations

import argparse
from datetime import timedelta
from pathlib import Path

from sleeper_manager.backtesting.experiments.lock_in_diagnostic import (
    LockInDiagnosticError,
    LockInDiagnosticRequest,
    run_lock_in_diagnostic,
)
from sleeper_manager.backtesting.experiments.lock_in_diagnostic_report import (
    logical_artifact_identity,
    write_diagnostic_report,
)
from sleeper_manager.backtesting.replay.engine import ReplayError
from sleeper_manager.backtesting.replay.inputs import (
    HistoricalTeamWeekArtifactError,
    load_historical_team_week_artifact,
)
from sleeper_manager.backtesting.replay.runner import ReplayRunnerError
from sleeper_manager.decisions.lock_in import LockInPolicyConfig


def _parser() -> argparse.ArgumentParser:
    """Build the diagnostic-only artifact and policy configuration parser."""

    parser = argparse.ArgumentParser(
        description=(
            "Run one historical Lock-In diagnostic comparison. "
            "Consumes an existing team-week artifact and writes diagnostic-only evidence."
        )
    )
    parser.add_argument("--team-week-path", type=Path, required=True)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path(".local/model-validation/reports"),
    )
    parser.add_argument("--scenario-count", type=int, default=200)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--tie-tolerance", type=float, default=0.01)
    parser.add_argument("--planning-lead-time-seconds", type=float, default=60.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Load one team-week artifact, run the diagnostic, and persist deterministic reports."""

    args = _parser().parse_args(argv)
    try:
        team_week = load_historical_team_week_artifact(args.team_week_path)
        request = LockInDiagnosticRequest(
            team_week=team_week,
            policy_config=LockInPolicyConfig(
                scenario_count=args.scenario_count,
                seed=args.seed,
                tie_tolerance=args.tie_tolerance,
            ),
            planning_lead_time=timedelta(seconds=args.planning_lead_time_seconds),
        )
        execution = run_lock_in_diagnostic(request)
        report = write_diagnostic_report(args.output_root, request, execution)
    except (
        HistoricalTeamWeekArtifactError,
        LockInDiagnosticError,
        ReplayError,
        ReplayRunnerError,
        OSError,
        ValueError,
    ) as error:
        print(f"Lock-In diagnostic failed: {error}")
        return 2

    print(f"Status: {execution.status}")
    print(f"Diagnostic run: {report.diagnostic_run_id}")
    print(f"Logical artifact: {logical_artifact_identity(request.team_week)}")
    print(f"JSON: {report.json_path}")
    print(f"Markdown: {report.markdown_path}")
    if execution.status != "success":
        return 1
    return 0


__all__ = ("main",)
