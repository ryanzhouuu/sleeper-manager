"""Render and persist deterministic historical Lock-In diagnostic reports."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sleeper_manager.backtesting.artifacts import atomic_write_bytes, atomic_write_json
from sleeper_manager.backtesting.experiments.lock_in_diagnostic_models import (
    LockInDiagnosticExecution,
    LockInDiagnosticRequest,
)
from sleeper_manager.backtesting.experiments.lock_in_diagnostic_report_payload import (
    REPORT_SCHEMA_VERSION,
    REPORT_TYPE,
    build_diagnostic_report_payload,
    diagnostic_run_id,
    logical_artifact_identity,
)


@dataclass(frozen=True, slots=True)
class DiagnosticReportOutput:
    """Locate deterministic JSON and Markdown outputs for one diagnostic run."""

    diagnostic_run_id: str
    output_dir: Path
    json_path: Path
    markdown_path: Path
    payload: dict[str, Any]


def render_diagnostic_markdown(payload: dict[str, Any]) -> str:
    """Render a deterministic Markdown summary for one diagnostic payload."""

    source = payload["source"]
    policy = payload["policy"]
    execution = payload["execution"]
    comparison = payload["comparison"]
    lines = [
        "# Lock-In diagnostic report",
        "",
        (
            "This is diagnostic-only evidence for one historical team-week. "
            "It is not a complete validation result and does not establish release readiness."
        ),
        "",
        "## Identity",
        "",
        f"- report_type: `{payload['report_type']}`",
        f"- report_schema_version: `{payload['report_schema_version']}`",
        f"- diagnostic_adapter_version: `{payload['diagnostic_adapter_version']}`",
        f"- diagnostic_run_id: `{payload['diagnostic_run_id']}`",
        f"- diagnostic_only: `{payload['diagnostic_only']}`",
        f"- status: `{payload['status']}`",
        "",
        "## Source",
        "",
        f"- manifest_id: `{source['manifest_id']}`",
        f"- team_week_fingerprint: `{source['team_week_fingerprint']}`",
        f"- league_id: `{source['league_id']}`",
        f"- season: `{source['season']}`",
        f"- week: `{source['week']}`",
        f"- roster_id: `{source['roster_id']}`",
        f"- logical_artifact_identity: `{source['logical_artifact_identity']}`",
        f"- complete: `{source['complete']}`",
        f"- eligibility_quality: `{source['eligibility_quality']}`",
        f"- coverage: `{source['coverage']}`",
        f"- exclusions: `{source['exclusions']}`",
        "",
        "## Admission",
        "",
    ]
    for check in payload["admission"]["checks"]:
        outcome = "pass" if check["passed"] else "fail"
        lines.append(f"- `{check['code']}`: {outcome} ({check['detail']})")
    lines.extend(
        [
            "",
            "## Policy",
            "",
            f"- name: `{policy['name']}`",
            f"- scenario_count: `{policy['scenario_count']}`",
            f"- seed: `{policy['seed']}`",
            f"- tie_tolerance: `{policy['tie_tolerance']}`",
            f"- planning_lead_time_seconds: `{policy['planning_lead_time_seconds']}`",
            "",
            "## Execution",
            "",
            f"- evaluation_order: `{execution['evaluation_order']}`",
            f"- batches: `{execution['batches']}`",
            f"- deferrals: `{execution['deferrals']}`",
            f"- policy_traces: `{execution['policy_traces']}`",
            f"- automatic_assignments: `{execution['automatic_assignments']}`",
            f"- oracle_feasibility: `{execution['oracle_feasibility']}`",
            "",
            "## Model",
            "",
            f"- `{payload['model']}`",
            "",
            "## Oracle",
            "",
            f"- `{payload['oracle']}`",
            "",
            "## Comparison",
            "",
            f"- `{comparison}`",
            "",
            "## Limitations",
            "",
        ]
    )
    lines.extend(f"- {item}" for item in payload["limitations"])
    lines.append("")
    return "\n".join(lines)


def write_diagnostic_report(
    output_root: Path,
    request: LockInDiagnosticRequest,
    execution: LockInDiagnosticExecution,
) -> DiagnosticReportOutput:
    """Write manifest/run-keyed JSON and Markdown artifacts atomically."""

    payload = build_diagnostic_report_payload(request, execution)
    run_id = str(payload["diagnostic_run_id"])
    team_week = request.team_week
    output_dir = output_root / "lock-in-diagnostics" / team_week.manifest_id / run_id
    stem = f"league-{team_week.league_id}-week-{team_week.week}-roster-{team_week.roster_id}"
    json_path = output_dir / f"{stem}.json"
    markdown_path = output_dir / f"{stem}.md"
    atomic_write_json(json_path, payload)
    atomic_write_bytes(markdown_path, render_diagnostic_markdown(payload).encode())
    return DiagnosticReportOutput(
        diagnostic_run_id=run_id,
        output_dir=output_dir,
        json_path=json_path,
        markdown_path=markdown_path,
        payload=payload,
    )


__all__ = (
    "REPORT_SCHEMA_VERSION",
    "REPORT_TYPE",
    "DiagnosticReportOutput",
    "build_diagnostic_report_payload",
    "diagnostic_run_id",
    "logical_artifact_identity",
    "render_diagnostic_markdown",
    "write_diagnostic_report",
)
