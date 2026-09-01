from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sleeper_manager.backtesting.cohorts import CohortConfig
from sleeper_manager.backtesting.controls import CalibratedProjectionModel, NaiveProjectionBaseline
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
from sleeper_manager.backtesting.experiments.io import _json_value, _write_json
from sleeper_manager.backtesting.experiments.projection_evaluation_report import (
    ProjectionSelectionDecision,
    development_report,
    markdown_report,
    report,
)
from sleeper_manager.backtesting.experiments.projection_evaluation_report import (
    evaluate_selection as evaluate_selection,
)
from sleeper_manager.backtesting.models import (
    BacktestConfig,
    BacktestModel,
)
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

    ``mode="development"`` freezes (or refreshes) the manifest and runs development folds only
    -- no report is written, since a selection decision requires locked-retrospective evidence.
    ``mode="locked_retrospective"`` refuses to proceed on a missing or mismatched manifest, then
    additionally runs the 2025-26 locked-retrospective folds and writes the complete JSON and
    Markdown reports. ``progress`` receives operational events that never enter modeled output.
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
