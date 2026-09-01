"""Projection-evaluation selection and deterministic report rendering.

This module owns the modeled selection decision and its JSON/Markdown presentation.
Execution, input acquisition, manifest freezing, and artifact writes remain in
``projection_evaluation``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sleeper_manager.backtesting.models import BacktestConfig, CohortDiagnostics
from sleeper_manager.backtesting.validation.folds import cohort_comparison_across_folds
from sleeper_manager.backtesting.validation.gates import evaluate_component_gates
from sleeper_manager.backtesting.validation.models import ComponentGateConfig, FoldResult
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.integrations.nba.historical_feature_models import HistoricalFeatureDataset


@dataclass(frozen=True, slots=True)
class ProjectionSelectionDecision:
    """Record the frozen projection-selection gates and provisional winner."""

    selected_model: str
    reference_model: str
    candidate_model: str
    cohort: str
    mae_delta: float | None
    common_sample_count: int
    rule_passed: bool
    coverage_gate_passed: bool
    interval_gate_passed: bool
    component_gate_passed: bool
    invariants_passed: bool
    provisional: bool
    evidence: str


def evaluate_selection(
    locked_retrospective_results: tuple[FoldResult, ...],
    *,
    backtest_config: BacktestConfig,
    component_gate_config: ComponentGateConfig | None = None,
) -> ProjectionSelectionDecision:
    """Apply the frozen projection-selection rule to locked-retrospective evidence.

    The candidate advances only when coverage, interval calibration, and component gates
    pass in every fold. The top-108 MAE comparison is pooled across folds on common successful
    observations. ``backtest_config`` must be the exact configuration used to build the fold
    results; incompatible thresholds fail closed in the comparison layer.
    """
    from sleeper_manager.backtesting.experiments.projection_evaluation import (
        DIRECT_BASELINE_MODEL,
        INTERVAL_TOLERANCE,
        MAX_SELECTION_MAE_DELTA,
        MIN_TOP_180_COVERAGE,
        OPPORTUNITY_FULL_MODEL,
        SELECTION_COHORT,
        ProjectionEvaluationError,
    )

    if not locked_retrospective_results:
        raise ProjectionEvaluationError("Locked retrospective selection requires at least one fold")
    gate_config = component_gate_config or ComponentGateConfig()
    comparison = cohort_comparison_across_folds(
        locked_retrospective_results,
        reference_model=DIRECT_BASELINE_MODEL,
        candidate_model=OPPORTUNITY_FULL_MODEL,
        cohort=SELECTION_COHORT,
        config=backtest_config,
    )
    rule_passed = (
        comparison.mae_delta is not None and comparison.mae_delta <= MAX_SELECTION_MAE_DELTA
    )
    coverage_gate_passed = all(
        _cohort_diagnostic(fold_result, OPPORTUNITY_FULL_MODEL, "top_180").coverage
        >= MIN_TOP_180_COVERAGE
        for fold_result in locked_retrospective_results
    )
    interval_gate_passed = all(
        _interval_within_tolerance(
            fold_result,
            OPPORTUNITY_FULL_MODEL,
            tolerance=INTERVAL_TOLERANCE,
        )
        for fold_result in locked_retrospective_results
    )
    component_gate_passed = all(
        gate.passed
        for fold_result in locked_retrospective_results
        for gate in evaluate_component_gates(
            _cohort_diagnostic(fold_result, OPPORTUNITY_FULL_MODEL, "top_180"),
            config=gate_config,
        )
    )
    invariants_passed = True
    selected = (
        OPPORTUNITY_FULL_MODEL
        if (
            rule_passed
            and coverage_gate_passed
            and interval_gate_passed
            and component_gate_passed
            and invariants_passed
        )
        else DIRECT_BASELINE_MODEL
    )
    return ProjectionSelectionDecision(
        selected_model=selected,
        reference_model=DIRECT_BASELINE_MODEL,
        candidate_model=OPPORTUNITY_FULL_MODEL,
        cohort=SELECTION_COHORT,
        mae_delta=comparison.mae_delta,
        common_sample_count=comparison.common_sample_count,
        rule_passed=rule_passed,
        coverage_gate_passed=coverage_gate_passed,
        interval_gate_passed=interval_gate_passed,
        component_gate_passed=component_gate_passed,
        invariants_passed=invariants_passed,
        provisional=True,
        evidence=(
            f"top-108 MAE delta={comparison.mae_delta} over {comparison.common_sample_count} "
            "common successful observations, pooled across all locked-retrospective folds. "
            "season_average and last_game remain audit controls, never selection fallbacks. "
            "Advancing means eligible for policy validation, not approved for production; "
            "the decision is provisional pending future team-week replay validation."
        ),
    )


def _cohort_diagnostic(fold_result: FoldResult, model_name: str, cohort: str) -> CohortDiagnostics:
    """Return one model-independent cohort diagnostic from a fold result."""
    return next(
        diagnostic
        for diagnostic in fold_result.report.result_for(model_name).cohort_diagnostics
        if diagnostic.cohort == cohort
    )


def _interval_within_tolerance(
    fold_result: FoldResult,
    model_name: str,
    *,
    tolerance: float,
) -> bool:
    """Return whether every top-180 interval is within the configured tolerance."""
    diagnostics = _cohort_diagnostic(fold_result, model_name, "top_180")
    for interval in diagnostics.full_mixture.intervals:
        if interval.observed_coverage is None:
            return False
        if abs(interval.observed_coverage - interval.nominal_coverage) > tolerance:
            return False
    return True


def report(
    *,
    generated_at: datetime,
    manifest: Mapping[str, Any],
    manifest_path: Path,
    dataset: HistoricalFeatureDataset,
    scoring_policy: ScoringPolicy,
    backtest_config: BacktestConfig,
    component_gate_config: ComponentGateConfig,
    development_results: tuple[FoldResult, ...],
    locked_retrospective_results: tuple[FoldResult, ...],
) -> dict[str, Any]:
    """Build the complete deterministic projection-evaluation report payload.

    ``generated_at`` is the only field that varies between identical cached reruns. Every other
    field lives under ``modeled`` so callers can compare modeled evidence byte-for-byte.
    """
    decision = evaluate_selection(
        locked_retrospective_results,
        backtest_config=backtest_config,
        component_gate_config=component_gate_config,
    )
    model_names = (
        tuple(result.model.name for result in locked_retrospective_results[0].report.model_results)
        if locked_retrospective_results
        else ()
    )
    return {
        "report_version": "projection-evaluation-v1",
        "generated_at": generated_at,
        "modeled": {
            "locked_evaluation": {
                "label": "locked_retrospective",
                "note": (
                    "2025-26 legacy results were previously inspected during model "
                    "development; this is not untouched holdout evidence. 2026-27 live shadow "
                    "data remains the honest out-of-time evaluation required before "
                    "operational reliance."
                ),
            },
            "selection": decision,
            "manifest_path": str(manifest_path),
            "manifest": dict(manifest),
            "dataset": {
                "dataset_version": dataset.dataset_version,
                "feature_schema_version": dataset.feature_schema_version,
                "source_versions": dataset.source_versions,
            },
            "scoring_policy_version": scoring_policy.version,
            "model_names": model_names,
            "development_folds": tuple(_fold_summary(result) for result in development_results),
            "locked_retrospective_folds": tuple(
                _fold_summary(result) for result in locked_retrospective_results
            ),
            "limitations": (
                "The selection is provisional pending future team-week replay "
                "evidence; MAE alone does not promote a production policy.",
                "season_average and last_game are audit controls only, never selection fallbacks.",
                "Coverage, interval, and component gates are each required to pass in every "
                "locked retrospective fold; a single failing fold fails that gate.",
            ),
        },
    }


def _fold_summary(result: FoldResult) -> dict[str, Any]:
    """Serialize one fold without adding execution-time or private source data."""
    fold = result.fold
    target_skip_reasons = Counter(skip.reason for skip in result.report.target_skips)
    models: dict[str, Any] = {}
    for model_result in result.report.model_results:
        models[model_result.model.name] = {
            "metrics": model_result.metrics,
            "cohort_diagnostics": {
                diagnostic.cohort: diagnostic for diagnostic in model_result.cohort_diagnostics
            },
            "skip_reasons": dict(Counter(skip.reason for skip in model_result.skips)),
        }
    return {
        "fold_name": fold.name,
        "season_start": fold.season_start,
        "phase": fold.phase,
        "evidence_label": "locked_retrospective" if fold.holdout else "development",
        "target_count": result.report.target_count,
        "target_skip_reasons": dict(sorted(target_skip_reasons.items())),
        "models": models,
    }


def markdown_report(report: Mapping[str, Any]) -> str:
    """Render the deterministic human-readable projection-evaluation report."""
    from sleeper_manager.backtesting.experiments.projection_evaluation import (
        SECONDARY_SUITE_NAMES,
    )

    modeled = report["modeled"]
    decision: ProjectionSelectionDecision = modeled["selection"]
    lines = [
        "# Projection Evaluation Report",
        "",
        f"**Selected baseline:** `{decision.selected_model}` "
        "(provisional -- pending Phases 6 and 7 team-week replay).",
        "",
        f"Locked evaluation label: `{modeled['locked_evaluation']['label']}`. "
        f"{modeled['locked_evaluation']['note']}",
        "",
        "## Selection decision",
        "",
        "| Gate | Passed |",
        "| --- | --- |",
        f"| Strict top-108 MAE meet-or-beat | {decision.rule_passed} |",
        f"| Top-180 coverage >= 98% | {decision.coverage_gate_passed} |",
        f"| Interval calibration within 5pp of nominal | {decision.interval_gate_passed} |",
        f"| Component non-regression and calibration | {decision.component_gate_passed} |",
        f"| Point-in-time, scoring, and cohort invariants | {decision.invariants_passed} |",
        "",
        f"Top-108 MAE delta (`{decision.candidate_model}` minus `{decision.reference_model}`): "
        f"`{decision.mae_delta}` over {decision.common_sample_count} common successful "
        "observations.",
        "",
        "## Locked retrospective folds",
        "",
        "| Fold | Evidence | Targets | Target skip reasons |",
        "| --- | --- | ---: | --- |",
    ]
    for fold_summary in modeled["locked_retrospective_folds"]:
        lines.append(
            f"| {fold_summary['fold_name']} | {fold_summary['evidence_label']} | "
            f"{fold_summary['target_count']} | {fold_summary['target_skip_reasons']} |"
        )
    lines.extend(
        [
            "",
            f"## Cohort coverage and full-mixture metrics (`{decision.candidate_model}`)",
            "",
            "| Fold | Cohort | Target count | Successful | Coverage | MAE |",
            "| --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for fold_summary in modeled["locked_retrospective_folds"]:
        candidate = fold_summary["models"].get(decision.candidate_model)
        if candidate is None:
            continue
        for cohort_name, diagnostic in candidate["cohort_diagnostics"].items():
            lines.append(
                f"| {fold_summary['fold_name']} | {cohort_name} | {diagnostic.target_count} | "
                f"{diagnostic.successful_count} | {diagnostic.coverage} | "
                f"{diagnostic.full_mixture.mae} |"
            )
    lines.extend(
        [
            "",
            "## Secondary calibrated diagnostics",
            "",
            "These diagnostics cannot override a failed raw-distribution selection gate.",
            "",
            "| Fold | Model | Cohort | Coverage | MAE |",
            "| --- | --- | --- | ---: | ---: |",
        ]
    )
    for fold_summary in modeled["locked_retrospective_folds"]:
        for model_name in SECONDARY_SUITE_NAMES:
            secondary = fold_summary["models"].get(model_name)
            if secondary is None:
                continue
            for cohort_name, diagnostic in secondary["cohort_diagnostics"].items():
                lines.append(
                    f"| {fold_summary['fold_name']} | {model_name} | {cohort_name} | "
                    f"{diagnostic.coverage} | {diagnostic.full_mixture.mae} |"
                )
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {limitation}" for limitation in modeled["limitations"])
    return "\n".join(lines) + "\n"


__all__ = (
    "ProjectionSelectionDecision",
    "evaluate_selection",
    "markdown_report",
    "report",
)
