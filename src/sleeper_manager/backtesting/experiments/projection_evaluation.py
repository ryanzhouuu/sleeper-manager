"""Orchestrate development and locked-retrospective projection evaluation."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sleeper_manager.backtesting.cohorts import CohortConfig
from sleeper_manager.backtesting.experiments.data import (
    load_historical_experiment_inputs,
    scoring_policy_from_league_fixture,
)
from sleeper_manager.backtesting.experiments.feature_validation import (
    _build_dataset,
    _historical_player_ids_by_date_team,
)
from sleeper_manager.backtesting.experiments.injuries import (
    acquire_injury_archive,
)
from sleeper_manager.backtesting.experiments.io import _write_json
from sleeper_manager.backtesting.experiments.projection_evaluation_config import (
    CALIBRATED_DIRECT_MODEL,
    CALIBRATED_OPPORTUNITY_MODEL,
    DIRECT_BASELINE_MODEL,
    INTERVAL_TOLERANCE,
    LAST_GAME_MODEL,
    MAX_SELECTION_MAE_DELTA,
    MIN_TOP_180_COVERAGE,
    OPPORTUNITY_FULL_MODEL,
    OPPORTUNITY_NO_DEFENSE_MODEL,
    OPPORTUNITY_NO_PACE_MODEL,
    RAW_SUITE_NAMES,
    SEASON_AVERAGE_MODEL,
    SECONDARY_SUITE_NAMES,
    SELECTION_COHORT,
    ProjectionEvaluationError,
    assert_manifest_frozen,
    freeze_manifest,
    frozen_manifest,
    raw_suite,
    secondary_calibrated_suite,
)
from sleeper_manager.backtesting.experiments.projection_evaluation_report import (
    ProjectionSelectionDecision,
    development_report,
    markdown_report,
    report,
)
from sleeper_manager.backtesting.experiments.projection_evaluation_report import (
    evaluate_selection as evaluate_selection,
)
from sleeper_manager.backtesting.models import BacktestConfig
from sleeper_manager.backtesting.progress import (
    ProgressMode,
    ProgressReporter,
    ProgressSink,
    ProgressStage,
)
from sleeper_manager.backtesting.validation.folds import (
    regular_season_folds,
    run_validation_folds,
)
from sleeper_manager.backtesting.validation.models import (
    ComponentGateConfig,
)


@dataclass(frozen=True, slots=True)
class ProjectionEvaluationOutput:
    """Paths and stable identities produced by one evaluation command."""

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
    progress: ProgressSink | None = None,
) -> ProjectionEvaluationOutput:
    """Run the projection evaluation against cached inputs.

    ``mode="development"`` freezes the manifest and runs development folds only. Locked mode
    requires the frozen manifest, then additionally runs the locked-retrospective folds and writes
    the complete reports. ``progress`` receives operational events that never enter modeled output.
    """
    if mode not in ("development", "locked_retrospective"):
        raise ProjectionEvaluationError(f"Unknown projection evaluation mode: {mode!r}")
    generated_at = now or datetime.now(UTC)
    if generated_at.tzinfo is None:
        raise ProjectionEvaluationError("Evaluation timestamp must be timezone-aware")
    progress_mode = ProgressMode(mode)
    reporter = ProgressReporter(progress_mode, progress)
    with reporter.stage(ProgressStage.RESOLVE_SOURCE_REVISION):
        source_revision = _git_source_revision()
    raw_dir = workspace / "raw"
    reports_dir = workspace / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    scoring_policy = scoring_policy_from_league_fixture(league_fixture)
    with reporter.stage(ProgressStage.LOAD_RAW_INPUTS, unit="resources") as counter:
        inputs = load_historical_experiment_inputs(
            raw_dir,
            retrieved_at=generated_at,
            progress=counter,
        )
    with reporter.stage(ProgressStage.LOAD_INJURY_ARCHIVE, unit="cutoffs") as counter:
        injuries = acquire_injury_archive(
            inputs.games,
            inputs.provider_players,
            workspace / "injuries",
            retrieved_at=generated_at,
            historical_player_ids_by_date_team=_historical_player_ids_by_date_team(inputs),
            progress=counter,
        )
    with reporter.stage(
        ProgressStage.BUILD_HISTORICAL_FEATURES,
        total=len(inputs.player_box_scores),
        unit="rows",
    ) as counter:
        dataset = _build_dataset(
            inputs,
            injuries,
            scoring_policy,
            generated_at,
            progress=counter,
        )
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
        progress=reporter,
        progress_stage=ProgressStage.RUN_DEVELOPMENT_FOLD,
    )
    development_report_path = reports_dir / "projection-evaluation-development-report.json"

    if mode == "development":
        with reporter.stage(ProgressStage.BUILD_REPORTS):
            development_payload = development_report(
                generated_at=generated_at,
                source_revision=source_revision,
                manifest=manifest,
                manifest_path=manifest_path,
                dataset=dataset,
                scoring_policy=scoring_policy,
                development_results=development_results,
            )
        with reporter.stage(
            ProgressStage.WRITE_ARTIFACTS,
            total=2,
            unit="artifacts",
        ) as counter:
            _write_json(development_report_path, development_payload)
            counter.advance(1, 2, detail="development report")
            freeze_manifest(manifest_path, manifest)
            counter.advance(2, 2, detail="manifest")
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
        progress=reporter,
        progress_stage=ProgressStage.RUN_LOCKED_FOLD,
    )
    report_json_path = reports_dir / "projection-evaluation-report.json"
    report_markdown_path = reports_dir / "projection-evaluation-report.md"
    with reporter.stage(ProgressStage.BUILD_REPORTS):
        development_payload = development_report(
            generated_at=generated_at,
            source_revision=source_revision,
            manifest=manifest,
            manifest_path=manifest_path,
            dataset=dataset,
            scoring_policy=scoring_policy,
            development_results=development_results,
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
        rendered_report = markdown_report(evaluation_report)
    with reporter.stage(
        ProgressStage.WRITE_ARTIFACTS,
        total=3,
        unit="artifacts",
    ) as counter:
        _write_json(development_report_path, development_payload)
        counter.advance(1, 3, detail="development report")
        _write_json(report_json_path, evaluation_report)
        counter.advance(2, 3, detail="JSON report")
        report_markdown_path.write_text(rendered_report)
        counter.advance(3, 3, detail="Markdown report")
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


__all__ = (
    "CALIBRATED_DIRECT_MODEL",
    "CALIBRATED_OPPORTUNITY_MODEL",
    "DIRECT_BASELINE_MODEL",
    "INTERVAL_TOLERANCE",
    "LAST_GAME_MODEL",
    "MAX_SELECTION_MAE_DELTA",
    "MIN_TOP_180_COVERAGE",
    "OPPORTUNITY_FULL_MODEL",
    "OPPORTUNITY_NO_DEFENSE_MODEL",
    "OPPORTUNITY_NO_PACE_MODEL",
    "ProjectionEvaluationError",
    "ProjectionEvaluationOutput",
    "RAW_SUITE_NAMES",
    "SECONDARY_SUITE_NAMES",
    "SEASON_AVERAGE_MODEL",
    "SELECTION_COHORT",
    "assert_manifest_frozen",
    "evaluate_selection",
    "freeze_manifest",
    "frozen_manifest",
    "raw_suite",
    "run_projection_evaluation",
    "secondary_calibrated_suite",
)
