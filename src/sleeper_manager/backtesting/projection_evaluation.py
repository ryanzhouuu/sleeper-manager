from __future__ import annotations

import json
import subprocess
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sleeper_manager.backtesting.cohorts import CohortConfig
from sleeper_manager.backtesting.controls import CalibratedProjectionModel, NaiveProjectionBaseline
from sleeper_manager.backtesting.experiment_data import (
    load_historical_experiment_inputs,
    scoring_policy_from_league_fixture,
)
from sleeper_manager.backtesting.experiment_injuries import (
    acquire_injury_archive,
)
from sleeper_manager.backtesting.experiment_io import _json_value, _write_json
from sleeper_manager.backtesting.feature_validation import (
    _build_dataset,
    _historical_player_ids_by_date_team,
)
from sleeper_manager.backtesting.models import (
    BacktestConfig,
    BacktestModel,
    CohortDiagnostics,
)
from sleeper_manager.backtesting.validation_folds import (
    cohort_comparison_across_folds,
    regular_season_folds,
    run_validation_folds,
)
from sleeper_manager.backtesting.validation_gates import (
    evaluate_component_gates,
)
from sleeper_manager.backtesting.validation_models import (
    ComponentGateConfig,
    FoldResult,
)
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.integrations.nba.historical_feature_models import (
    HistoricalFeatureDataset,
)
from sleeper_manager.projections.direct_baseline import DirectFantasyPointBaseline
from sleeper_manager.projections.opportunity_model import InterpretableOpportunityModel
from sleeper_manager.projections.opportunity_types import OpportunityModelConfig
from sleeper_manager.projections.residual_candidates import CachingProjectionModel

# --- Projection evaluation --------------------------------------------------------------
#
# A frozen, interpretable-opportunity-model-centered comparison, separate from the older
# residual-feature-selection experiment above. That older experiment (and its
# `frozen-development-manifest.json` / `model-feature-validation-report.json` artifacts) remain
# available as historical evidence but are not part of the projection evaluation report.

DIRECT_BASELINE_MODEL = "direct_baseline"
SEASON_AVERAGE_MODEL = "season_average"
LAST_GAME_MODEL = "last_game"
OPPORTUNITY_FULL_MODEL = "opportunity_full"
OPPORTUNITY_NO_PACE_MODEL = "opportunity_no_pace"
OPPORTUNITY_NO_DEFENSE_MODEL = "opportunity_no_defense"
RAW_SUITE_NAMES: tuple[str, ...] = (
    DIRECT_BASELINE_MODEL,
    SEASON_AVERAGE_MODEL,
    LAST_GAME_MODEL,
    OPPORTUNITY_FULL_MODEL,
    OPPORTUNITY_NO_PACE_MODEL,
    OPPORTUNITY_NO_DEFENSE_MODEL,
)
CALIBRATED_DIRECT_MODEL = "direct_baseline_calibrated"
CALIBRATED_OPPORTUNITY_MODEL = "opportunity_full_calibrated"
SECONDARY_SUITE_NAMES: tuple[str, ...] = (
    CALIBRATED_DIRECT_MODEL,
    CALIBRATED_OPPORTUNITY_MODEL,
)
SELECTION_COHORT = "top_108"
MAX_SELECTION_MAE_DELTA = 0.0
MIN_TOP_180_COVERAGE = 0.98
INTERVAL_TOLERANCE = 0.05


class ProjectionEvaluationError(RuntimeError):
    pass


def raw_suite(
    *, opportunity_config: OpportunityModelConfig | None = None
) -> tuple[BacktestModel, ...]:
    """The frozen raw comparison. Names and order are deterministic and never reused for a
    different candidate roster -- ablations exist to explain the selected model, not to reopen
    feature search."""
    base_config = opportunity_config or OpportunityModelConfig()
    no_pace_config = replace(base_config, disable_pace=True)
    no_defense_config = replace(base_config, disable_defense=True)
    return (
        BacktestModel(DIRECT_BASELINE_MODEL, DirectFantasyPointBaseline()),
        BacktestModel(SEASON_AVERAGE_MODEL, NaiveProjectionBaseline("season_average")),
        BacktestModel(LAST_GAME_MODEL, NaiveProjectionBaseline("last_game")),
        BacktestModel(OPPORTUNITY_FULL_MODEL, InterpretableOpportunityModel(base_config)),
        BacktestModel(OPPORTUNITY_NO_PACE_MODEL, InterpretableOpportunityModel(no_pace_config)),
        BacktestModel(
            OPPORTUNITY_NO_DEFENSE_MODEL, InterpretableOpportunityModel(no_defense_config)
        ),
    )


def secondary_calibrated_suite(
    *, opportunity_config: OpportunityModelConfig | None = None
) -> tuple[BacktestModel, ...]:
    """Identically-configured rolling residual-calibrated variants of the direct baseline and
    the full opportunity model, as secondary diagnostics only. No calibrated ablations -- a
    calibrated result can never override a failed raw-distribution gate."""
    base_config = opportunity_config or OpportunityModelConfig()
    return (
        BacktestModel(
            CALIBRATED_DIRECT_MODEL,
            CachingProjectionModel(
                CalibratedProjectionModel(DirectFantasyPointBaseline()), max_entries=4096
            ),
        ),
        BacktestModel(
            CALIBRATED_OPPORTUNITY_MODEL,
            CachingProjectionModel(
                CalibratedProjectionModel(InterpretableOpportunityModel(base_config)),
                max_entries=4096,
            ),
        ),
    )


def frozen_manifest(
    *,
    dataset: HistoricalFeatureDataset,
    scoring_policy: ScoringPolicy,
    cohort_config: CohortConfig,
    backtest_config: BacktestConfig,
    component_gate: ComponentGateConfig,
    raw_suite: tuple[BacktestModel, ...],
    secondary_suite: tuple[BacktestModel, ...],
    source_revision: str,
) -> dict[str, Any]:
    """Freeze candidate, cohort, metric, gate, and index configuration together.

    The dataset/scoring-policy/cohort-config/backtest-config versions and each model's own
    version already fully determine the deterministic index behavior in
    `opportunity_model.py` and `cohorts.py` -- no separate free-standing "index config" exists.
    """
    return {
        "manifest_version": "projection-evaluation-v1",
        "source_revision": source_revision,
        "dataset_version": dataset.dataset_version,
        "feature_schema_version": dataset.feature_schema_version,
        "scoring_policy_version": scoring_policy.version,
        "cohort_config_version": cohort_config.version,
        "backtest_config_version": backtest_config.version,
        "component_gate": {
            "max_regression_fraction": component_gate.max_regression_fraction,
            "min_calibration_bin_size": component_gate.min_calibration_bin_size,
            "calibration_tolerance": component_gate.calibration_tolerance,
        },
        "raw_suite": tuple((model.name, _model_version(model)) for model in raw_suite),
        "secondary_suite": tuple((model.name, _model_version(model)) for model in secondary_suite),
        "selection_rule": {
            "reference_model": DIRECT_BASELINE_MODEL,
            "candidate_model": OPPORTUNITY_FULL_MODEL,
            "selection_cohort": SELECTION_COHORT,
            "max_mae_delta": MAX_SELECTION_MAE_DELTA,
            "min_top_180_coverage": MIN_TOP_180_COVERAGE,
            "interval_tolerance": INTERVAL_TOLERANCE,
        },
    }


def _model_version(model: BacktestModel) -> str:
    return str(getattr(model.projector, "model_version", type(model.projector).__name__))


def freeze_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(path, manifest)


def assert_manifest_frozen(path: Path, expected: Mapping[str, Any]) -> None:
    """Refuse locked-retrospective evaluation on a missing or mismatched frozen manifest."""
    if not path.exists():
        raise ProjectionEvaluationError(
            f"Locked retrospective evaluation requires a frozen manifest at {path!s}, "
            "but none has been written"
        )
    try:
        actual = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ProjectionEvaluationError(
            f"Frozen projection evaluation manifest at {path!s} is unreadable"
        ) from error
    if actual != _json_value(expected):
        raise ProjectionEvaluationError(
            "Frozen projection evaluation manifest does not match the current source revision and "
            "configuration; locked retrospective evaluation is refused"
        )


@dataclass(frozen=True, slots=True)
class ProjectionSelectionDecision:
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
    """Apply the strict, mechanical projection selection rule to locked-retrospective evidence.

    The candidate advances only when every gate passes in every given fold -- coverage,
    interval calibration, and component gates are each required to hold fold-by-fold (a
    stricter bar than pooling across folds, and it avoids merging per-fold calibration bins).
    The top-108 MAE meet-or-beat comparison is the one gate pooled across folds on common
    successful observations, since it is the frozen headline selection number.

    ``backtest_config`` must be the exact config used to produce ``locked_retrospective_results``
    (e.g. via ``run_validation_folds``) -- exceedance probabilities are baked into each
    observation at that config's thresholds, and a mismatched config fails closed with a
    ``BacktestError`` rather than silently returning a comparison for the wrong thresholds.
    """
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
        _interval_within_tolerance(fold_result, OPPORTUNITY_FULL_MODEL)
        for fold_result in locked_retrospective_results
    )
    component_gate_passed = all(
        gate.passed
        for fold_result in locked_retrospective_results
        for gate in evaluate_component_gates(
            _cohort_diagnostic(fold_result, OPPORTUNITY_FULL_MODEL, "top_180"), config=gate_config
        )
    )
    # run_backtest raises BacktestError before any report is produced if a cohort-invariance,
    # point-in-time, or lineage violation occurs -- reaching this point already proves the
    # invariant held for every fold evaluated.
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
    return next(
        diagnostic
        for diagnostic in fold_result.report.result_for(model_name).cohort_diagnostics
        if diagnostic.cohort == cohort
    )


def _interval_within_tolerance(fold_result: FoldResult, model_name: str) -> bool:
    diagnostics = _cohort_diagnostic(fold_result, model_name, "top_180")
    for interval in diagnostics.full_mixture.intervals:
        if interval.observed_coverage is None:
            return False
        if abs(interval.observed_coverage - interval.nominal_coverage) > INTERVAL_TOLERANCE:
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
    """Build the complete projection evaluation report.

    ``generated_at`` is the only field that varies between identical cached reruns; every other
    field lives under ``modeled`` so two reports can be compared for byte-equivalence by
    comparing ``modeled`` alone.
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


@dataclass(frozen=True, slots=True)
class ProjectionEvaluationOutput:
    manifest_path: Path
    development_report_path: Path
    report_json_path: Path | None
    report_markdown_path: Path | None
    dataset_version: str
    mode: str
    selected_model: str | None


def run_projection_evaluation(
    workspace: Path,
    *,
    league_fixture: Path,
    mode: str,
    now: datetime | None = None,
) -> ProjectionEvaluationOutput:
    """Run the projection evaluation against cached inputs.

    ``mode="development"`` freezes (or refreshes) the manifest and runs development folds only
    -- no report is written, since a selection decision requires locked-retrospective evidence.
    ``mode="locked_retrospective"`` refuses to proceed on a missing or mismatched manifest, then
    additionally runs the 2025-26 locked-retrospective folds and writes the complete JSON and
    Markdown reports.
    """
    if mode not in ("development", "locked_retrospective"):
        raise ProjectionEvaluationError(f"Unknown projection evaluation mode: {mode!r}")
    generated_at = now or datetime.now(UTC)
    if generated_at.tzinfo is None:
        raise ProjectionEvaluationError("Evaluation timestamp must be timezone-aware")
    source_revision = _git_source_revision()
    raw_dir = workspace / "raw"
    reports_dir = workspace / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    scoring_policy = scoring_policy_from_league_fixture(league_fixture)
    inputs = load_historical_experiment_inputs(raw_dir, retrieved_at=generated_at)
    injuries = acquire_injury_archive(
        inputs.games,
        inputs.provider_players,
        workspace / "injuries",
        retrieved_at=generated_at,
        historical_player_ids_by_date_team=_historical_player_ids_by_date_team(inputs),
    )
    dataset = _build_dataset(inputs, injuries, scoring_policy, generated_at)
    backtest_config = BacktestConfig(
        thresholds=(20.0, 30.0, 40.0, 50.0, 60.0),
        intervals=((10, 90), (25, 75)),
    )
    cohort_config = CohortConfig()
    component_gate_config = ComponentGateConfig()
    raw_models = raw_suite()
    secondary_models = secondary_calibrated_suite()
    comparison_suite = (*raw_models, *secondary_models)
    manifest = frozen_manifest(
        dataset=dataset,
        scoring_policy=scoring_policy,
        cohort_config=cohort_config,
        backtest_config=backtest_config,
        component_gate=component_gate_config,
        raw_suite=raw_models,
        secondary_suite=secondary_models,
        source_revision=source_revision,
    )
    manifest_path = reports_dir / "projection-evaluation-manifest.json"

    if mode == "locked_retrospective":
        assert_manifest_frozen(manifest_path, manifest)

    folds = regular_season_folds()
    development_folds = tuple(fold for fold in folds if not fold.holdout)
    development_results = run_validation_folds(
        dataset,
        scoring_policy=scoring_policy,
        models=comparison_suite,
        folds=development_folds,
        config=backtest_config,
        reference_model=DIRECT_BASELINE_MODEL,
    )
    development_report_path = reports_dir / "projection-evaluation-development-report.json"
    _write_json(
        development_report_path,
        {
            "report_version": "projection-evaluation-development-v1",
            "generated_at": generated_at,
            "modeled": {
                "source_revision": source_revision,
                "manifest_path": str(manifest_path),
                "manifest": manifest,
                "dataset": {
                    "dataset_version": dataset.dataset_version,
                    "feature_schema_version": dataset.feature_schema_version,
                    "source_versions": dataset.source_versions,
                },
                "scoring_policy_version": scoring_policy.version,
                "development_folds": tuple(_fold_summary(result) for result in development_results),
            },
        },
    )

    if mode == "development":
        freeze_manifest(manifest_path, manifest)
        return ProjectionEvaluationOutput(
            manifest_path=manifest_path,
            development_report_path=development_report_path,
            report_json_path=None,
            report_markdown_path=None,
            dataset_version=dataset.dataset_version,
            mode=mode,
            selected_model=None,
        )

    locked_retrospective_folds = tuple(fold for fold in folds if fold.holdout)
    locked_retrospective_results = run_validation_folds(
        dataset,
        scoring_policy=scoring_policy,
        models=comparison_suite,
        folds=locked_retrospective_folds,
        config=backtest_config,
        reference_model=DIRECT_BASELINE_MODEL,
    )
    evaluation_report = report(
        generated_at=generated_at,
        manifest=manifest,
        manifest_path=manifest_path,
        dataset=dataset,
        scoring_policy=scoring_policy,
        backtest_config=backtest_config,
        component_gate_config=component_gate_config,
        development_results=development_results,
        locked_retrospective_results=locked_retrospective_results,
    )
    report_json_path = reports_dir / "projection-evaluation-report.json"
    report_markdown_path = reports_dir / "projection-evaluation-report.md"
    _write_json(report_json_path, evaluation_report)
    report_markdown_path.write_text(markdown_report(evaluation_report))
    selection: ProjectionSelectionDecision = evaluation_report["modeled"]["selection"]
    return ProjectionEvaluationOutput(
        manifest_path=manifest_path,
        development_report_path=development_report_path,
        report_json_path=report_json_path,
        report_markdown_path=report_markdown_path,
        dataset_version=dataset.dataset_version,
        mode=mode,
        selected_model=selection.selected_model,
    )


def _git_source_revision() -> str:
    repository = Path(__file__).resolve().parents[3]
    try:
        status = subprocess.run(
            ("git", "status", "--porcelain", "--untracked-files=no"),
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        )
        revision = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise ProjectionEvaluationError(
            "Could not resolve the projection evaluation source revision"
        ) from error
    if status.stdout.strip():
        raise ProjectionEvaluationError(
            "Projection evaluation requires a clean tracked worktree so the source revision "
            "is exact"
        )
    if not revision:
        raise ProjectionEvaluationError("Projection evaluation source revision was empty")
    return revision
