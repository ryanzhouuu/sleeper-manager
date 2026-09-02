"""Frozen model suites and manifest contract for projection evaluation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from sleeper_manager.backtesting.cohorts import CohortConfig
from sleeper_manager.backtesting.controls import CalibratedProjectionModel, NaiveProjectionBaseline
from sleeper_manager.backtesting.experiments.io import _json_value, _write_json
from sleeper_manager.backtesting.models import BacktestConfig, BacktestModel
from sleeper_manager.backtesting.validation.models import ComponentGateConfig
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.integrations.nba.historical_feature_models import (
    HistoricalFeatureDataset,
)
from sleeper_manager.projections.direct_baseline import DirectFantasyPointBaseline
from sleeper_manager.projections.opportunity_model import InterpretableOpportunityModel
from sleeper_manager.projections.opportunity_types import OpportunityModelConfig
from sleeper_manager.projections.residual_candidates import CachingProjectionModel

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
    """Raised when projection evaluation cannot preserve its frozen contract."""


def raw_suite(
    *, opportunity_config: OpportunityModelConfig | None = None
) -> tuple[BacktestModel, ...]:
    """Build the frozen raw comparison in deterministic order."""
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
    """Build the two rolling residual-calibrated secondary diagnostics."""
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
    """Freeze candidate, cohort, metric, gate, and index configuration together."""
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


def freeze_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    """Write the development manifest used to authorize locked evaluation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(path, manifest)


def assert_manifest_frozen(path: Path, expected: Mapping[str, Any]) -> None:
    """Refuse locked evaluation on a missing or mismatched frozen manifest."""
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
            "Frozen projection evaluation manifest does not match the current source revision "
            "and configuration; locked retrospective evaluation is refused"
        )


def _model_version(model: BacktestModel) -> str:
    """Return the explicit projector version or its stable class fallback."""
    return str(getattr(model.projector, "model_version", type(model.projector).__name__))


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
    "RAW_SUITE_NAMES",
    "SECONDARY_SUITE_NAMES",
    "SEASON_AVERAGE_MODEL",
    "SELECTION_COHORT",
    "assert_manifest_frozen",
    "freeze_manifest",
    "frozen_manifest",
    "raw_suite",
    "secondary_calibrated_suite",
)
