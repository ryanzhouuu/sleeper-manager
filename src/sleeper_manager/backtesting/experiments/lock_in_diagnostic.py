"""Deterministic Lock-In diagnostic over one historical team-week artifact."""

from __future__ import annotations

from dataclasses import replace

from sleeper_manager.backtesting.experiments.lock_in_diagnostic_engine import (
    DiagnosticPolicyAdapter,
    build_model_result,
    legal_automatic_assignments,
    oracle_feasibility_checks,
    replay_config,
)
from sleeper_manager.backtesting.experiments.lock_in_diagnostic_models import (
    DECISION_CRITICAL_EXCLUSIONS,
    DIAGNOSTIC_ADAPTER_VERSION,
    AdmissionCheck,
    AdmissionResult,
    AutomaticSlotAssignment,
    CandidateBatch,
    DiagnosticDeferral,
    DiagnosticPolicyTrace,
    LockInDiagnosticError,
    LockInDiagnosticExecution,
    LockInDiagnosticRequest,
    OracleFeasibilityCheck,
)
from sleeper_manager.backtesting.experiments.lock_in_diagnostic_report import (
    DiagnosticReportOutput,
    diagnostic_run_id,
    logical_artifact_identity,
    write_diagnostic_report,
)
from sleeper_manager.backtesting.replay.engine import (
    compare_team_week,
    oracle_team_week_result,
)
from sleeper_manager.backtesting.replay.inputs.models import HistoricalTeamWeekInput
from sleeper_manager.domain.planning import PlanningQuality


def admit_historical_team_week(team_week: HistoricalTeamWeekInput) -> AdmissionResult:
    """Return structured admission checks for one persisted team-week artifact."""

    coverage = team_week.coverage
    checks = (
        AdmissionCheck(
            "has_expected_player_games",
            coverage.expected_player_games > 0,
            f"expected_player_games={coverage.expected_player_games}",
        ),
        AdmissionCheck(
            "coverage_counts_agree",
            coverage.expected_player_games
            == coverage.joined_player_games
            == coverage.resolved_identities
            == coverage.scored_player_games
            == coverage.projected_player_games,
            (
                "expected="
                f"{coverage.expected_player_games} joined={coverage.joined_player_games} "
                f"resolved={coverage.resolved_identities} scored={coverage.scored_player_games} "
                f"projected={coverage.projected_player_games}"
            ),
        ),
        AdmissionCheck(
            "eligibility_quality_allowed",
            team_week.eligibility_quality
            in (
                PlanningQuality.EXACT,
                PlanningQuality.BEST_KNOWN_CONSTRAINTS_ORACLE,
            ),
            f"eligibility_quality={team_week.eligibility_quality.value}",
        ),
        AdmissionCheck(
            "no_decision_critical_exclusions",
            not any(item.reason in DECISION_CRITICAL_EXCLUSIONS for item in team_week.exclusions),
            f"exclusions={tuple(item.reason.value for item in team_week.exclusions)}",
        ),
        AdmissionCheck(
            "starter_slots_nonempty",
            bool(team_week.starter_slots),
            f"starter_slot_count={len(team_week.starter_slots)}",
        ),
        AdmissionCheck(
            "games_nonempty",
            bool(team_week.games),
            f"game_count={len(team_week.games)}",
        ),
        AdmissionCheck(
            "player_games_nonempty",
            bool(team_week.player_games),
            f"player_game_count={len(team_week.player_games)}",
        ),
        AdmissionCheck(
            "roster_identity_present",
            bool(team_week.league_id.strip())
            and bool(team_week.season.strip())
            and team_week.week > 0
            and team_week.roster_id > 0
            and bool(team_week.roster_player_ids),
            (
                f"league={team_week.league_id} season={team_week.season} "
                f"week={team_week.week} roster={team_week.roster_id} "
                f"roster_players={len(team_week.roster_player_ids)}"
            ),
        ),
        AdmissionCheck(
            "projections_present",
            all(item.projection is not None for item in team_week.player_games),
            "every player-game carries a projection snapshot",
        ),
        AdmissionCheck(
            "observed_starters_align",
            len(team_week.observed_starter_ids) == len(team_week.starter_slots),
            (
                f"observed={len(team_week.observed_starter_ids)} "
                f"slots={len(team_week.starter_slots)}"
            ),
        ),
    )
    return AdmissionResult(all(check.passed for check in checks), checks)


def run_lock_in_diagnostic(request: LockInDiagnosticRequest) -> LockInDiagnosticExecution:
    """Execute model-policy and constrained-oracle comparison for one admitted team-week."""

    admission = admit_historical_team_week(request.team_week)
    if not admission.admitted:
        failed = tuple(check.code for check in admission.checks if not check.passed)
        raise LockInDiagnosticError(f"Team-week artifact failed diagnostic admission: {failed}")

    adapter = DiagnosticPolicyAdapter(request)
    adapter.run()
    if not adapter.policy_traces:
        return LockInDiagnosticExecution(
            status="blocked_no_evaluable_candidate",
            admission=admission,
            model_result=None,
            oracle_result=None,
            comparison=None,
            policy_traces=(),
            deferrals=tuple(adapter.deferrals),
            batches=tuple(adapter.batches),
            evaluation_order=tuple(adapter.evaluation_order),
            automatic_assignments=(),
            oracle_feasibility=(),
        )

    automatic = legal_automatic_assignments(adapter.state, request.team_week)
    built_model = build_model_result(request, adapter, automatic)
    oracle_result = oracle_team_week_result(
        request.team_week.player_games,
        config=replay_config(request.team_week),
        games=request.team_week.games,
        require_full_cardinality=True,
    )
    oracle_result = replace(
        oracle_result,
        data_quality="complete" if request.team_week.complete else "partial",
    )
    feasibility = oracle_feasibility_checks(oracle_result, request.team_week)
    if any(not check.feasible for check in feasibility):
        raise LockInDiagnosticError("Oracle assignment is temporally infeasible for this artifact")
    comparison = compare_team_week(oracle_result, built_model)
    return LockInDiagnosticExecution(
        status="success",
        admission=admission,
        model_result=built_model,
        oracle_result=oracle_result,
        comparison=comparison,
        policy_traces=tuple(adapter.policy_traces),
        deferrals=tuple(adapter.deferrals),
        batches=tuple(adapter.batches),
        evaluation_order=tuple(adapter.evaluation_order),
        automatic_assignments=automatic,
        oracle_feasibility=feasibility,
    )


def main(argv: list[str] | None = None) -> int:
    """Delegate module-command execution to the isolated CLI boundary."""

    from sleeper_manager.backtesting.experiments.lock_in_diagnostic_cli import (
        main as cli_main,
    )

    return cli_main(argv)


__all__ = (
    "AdmissionCheck",
    "AdmissionResult",
    "AutomaticSlotAssignment",
    "CandidateBatch",
    "DIAGNOSTIC_ADAPTER_VERSION",
    "DiagnosticDeferral",
    "DiagnosticPolicyTrace",
    "DiagnosticReportOutput",
    "LockInDiagnosticError",
    "LockInDiagnosticExecution",
    "LockInDiagnosticRequest",
    "OracleFeasibilityCheck",
    "admit_historical_team_week",
    "diagnostic_run_id",
    "logical_artifact_identity",
    "main",
    "run_lock_in_diagnostic",
    "write_diagnostic_report",
)


if __name__ == "__main__":
    raise SystemExit(main())
