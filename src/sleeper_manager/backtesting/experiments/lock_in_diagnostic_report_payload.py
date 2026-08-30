"""Build stable identities and JSON payloads for Lock-In diagnostic reports."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sleeper_manager.backtesting.artifacts import canonical_json, canonicalize, sha256_text
from sleeper_manager.backtesting.experiments.lock_in_diagnostic_models import (
    DIAGNOSTIC_ADAPTER_VERSION,
    DiagnosticDeferral,
    DiagnosticPolicyTrace,
    LockInDiagnosticExecution,
    LockInDiagnosticRequest,
)
from sleeper_manager.backtesting.replay.inputs.models import HistoricalTeamWeekInput
from sleeper_manager.backtesting.replay.models import (
    LockedSlot,
    ReplayDecision,
    TeamWeekReplayResult,
)

REPORT_SCHEMA_VERSION = "lock-in-diagnostic-report-v2"
REPORT_TYPE = "lock_in_diagnostic_report"

LIMITATIONS = (
    "Eligibility may be late-captured best-known constraints rather than exact historical "
    "eligibility.",
    "Game finalization timestamps may be approximate next-local-day completion bounds.",
    "Projection snapshot cadence can force deferrals until later as-of evidence exists.",
    "Simultaneous approximate-finalization batches may introduce order effects inside a batch.",
    "This report is diagnostic_only evidence for one team-week and is not release validation.",
)


def logical_artifact_identity(team_week: HistoricalTeamWeekInput) -> str:
    """Return the durable relative identity for one roster-week artifact."""

    return (
        f"team-weeks/{team_week.league_id}/"
        f"week-{team_week.week:02d}/roster-{team_week.roster_id}.json"
    )


def diagnostic_run_id(request: LockInDiagnosticRequest) -> str:
    """Derive a configuration-keyed run fingerprint for reproducible output paths."""

    team_week = request.team_week
    config = request.policy_config
    fingerprint = {
        "manifest_id": team_week.manifest_id,
        "team_week_fingerprint": _team_week_fingerprint(team_week),
        "league_id": team_week.league_id,
        "season": team_week.season,
        "week": team_week.week,
        "roster_id": team_week.roster_id,
        "policy_name": request.policy_name,
        "policy_config": {
            "scenario_count": config.scenario_count,
            "seed": config.seed,
            "tie_tolerance": config.tie_tolerance,
        },
        "planning_lead_time_seconds": _lead_time_seconds(request.planning_lead_time),
        "planning_cutoffs": (
            None
            if request.planning_cutoffs is None
            else tuple(cutoff.isoformat() for cutoff in request.planning_cutoffs)
        ),
        "diagnostic_adapter_version": DIAGNOSTIC_ADAPTER_VERSION,
        "report_schema_version": REPORT_SCHEMA_VERSION,
    }
    return sha256_text(canonical_json(fingerprint))


def build_diagnostic_report_payload(
    request: LockInDiagnosticRequest,
    execution: LockInDiagnosticExecution,
) -> dict[str, Any]:
    """Build the deterministic JSON payload for one diagnostic execution."""

    team_week = request.team_week
    run_id = diagnostic_run_id(request)
    return {
        "report_type": REPORT_TYPE,
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "diagnostic_adapter_version": DIAGNOSTIC_ADAPTER_VERSION,
        "diagnostic_run_id": run_id,
        "diagnostic_only": True,
        "status": execution.status,
        "source": {
            "manifest_id": team_week.manifest_id,
            "team_week_fingerprint": _team_week_fingerprint(team_week),
            "league_id": team_week.league_id,
            "season": team_week.season,
            "week": team_week.week,
            "roster_id": team_week.roster_id,
            "logical_artifact_identity": logical_artifact_identity(team_week),
            "complete": team_week.complete,
            "eligibility_quality": team_week.eligibility_quality.value,
            "coverage": canonicalize(team_week.coverage),
            "exclusions": canonicalize(team_week.exclusions),
        },
        "admission": {
            "admitted": execution.admission.admitted,
            "checks": canonicalize(execution.admission.checks),
        },
        "policy": {
            "name": request.policy_name,
            "scenario_count": request.policy_config.scenario_count,
            "seed": request.policy_config.seed,
            "tie_tolerance": request.policy_config.tie_tolerance,
            "planning_lead_time_seconds": _lead_time_seconds(request.planning_lead_time),
        },
        "execution": {
            "batches": canonicalize(execution.batches),
            "evaluation_order": list(execution.evaluation_order),
            "deferrals": [_deferral_payload(item) for item in execution.deferrals],
            "policy_traces": [_trace_payload(item) for item in execution.policy_traces],
            "automatic_assignments": canonicalize(execution.automatic_assignments),
            "oracle_feasibility": canonicalize(execution.oracle_feasibility),
        },
        "model": _team_week_payload(execution.model_result),
        "oracle": _team_week_payload(execution.oracle_result),
        "comparison": canonicalize(execution.comparison) if execution.comparison else None,
        "limitations": list(LIMITATIONS),
    }


def _lead_time_seconds(value: timedelta) -> float:
    """Serialize a planning lead time without losing fractional seconds."""

    return value.total_seconds()


def _team_week_fingerprint(team_week: HistoricalTeamWeekInput) -> str:
    """Hash the exact reconstructed evidence that can affect diagnostic output."""

    return sha256_text(canonical_json(team_week))


def _deferral_payload(item: DiagnosticDeferral) -> dict[str, Any]:
    """Serialize a deferral with stable timestamp and opportunity identities."""

    return {
        "candidate_id": item.candidate_id,
        "sleeper_id": item.sleeper_id,
        "game_id": item.game_id,
        "cutoff": item.cutoff.isoformat(),
        "event_id": item.event_id,
        "batch_id": item.batch_id,
        "missing_opportunity_keys": list(item.missing_opportunity_keys),
        "reason": item.reason,
        "terminal": item.terminal,
    }


def _trace_payload(item: DiagnosticPolicyTrace) -> dict[str, Any]:
    """Serialize one ordered policy trace without implementation-only fields."""

    decision = item.decision
    return {
        "candidate_id": item.candidate_id,
        "decision_time": item.decision_time.isoformat(),
        "event_id": item.event_id,
        "batch_id": item.batch_id,
        "evaluation_order": item.evaluation_order,
        "kind": decision.kind,
        "player_id": decision.player_id,
        "game_id": decision.game_id,
        "slot_index": decision.slot_index,
        "expected_terminal_score": decision.expected_terminal_score,
        "counterfactual_value": decision.counterfactual_value,
        "information_version": decision.information_version,
        "reason": decision.reason,
    }


def _team_week_payload(result: TeamWeekReplayResult | None) -> dict[str, Any] | None:
    """Serialize an optional shared replay result for diagnostic reporting."""

    if result is None:
        return None
    return {
        "league_id": result.league_id,
        "week": result.week,
        "roster_id": result.roster_id,
        "policy_name": result.policy_name,
        "realized_score": result.realized_score,
        "eligibility_quality": result.eligibility_quality,
        "data_quality": result.data_quality,
        "exclusions": list(result.exclusions),
        "decisions": [_decision_payload(item) for item in result.decisions],
        "locked_slots": [_locked_slot_payload(item) for item in result.locked_slots],
        "automatic_final_scores": [list(item) for item in result.automatic_final_scores],
    }


def _decision_payload(item: ReplayDecision) -> dict[str, Any]:
    """Serialize one model or oracle replay decision."""

    return {
        "decision_time": item.decision_time.isoformat(),
        "kind": item.kind,
        "player_id": item.player_id,
        "game_id": item.game_id,
        "slot_index": item.slot_index,
        "information_version": item.information_version,
        "expected_terminal_score": item.expected_terminal_score,
        "counterfactual_value": item.counterfactual_value,
        "reason": item.reason,
    }


def _locked_slot_payload(item: LockedSlot) -> dict[str, Any]:
    """Serialize one realized locked-slot assignment."""

    return {
        "slot_index": item.slot_index,
        "slot_position": item.slot_position,
        "sleeper_id": item.sleeper_id,
        "game_id": item.game_id,
        "score": item.score,
        "locked_at": item.locked_at.isoformat(),
    }


__all__ = (
    "LIMITATIONS",
    "REPORT_SCHEMA_VERSION",
    "REPORT_TYPE",
    "build_diagnostic_report_payload",
    "diagnostic_run_id",
    "logical_artifact_identity",
)
