"""Validation for aggregate development-fold summaries stored in checkpoints."""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from typing import Any

from sleeper_manager.backtesting.models import COHORT_NAMES


def validate_development_fold_summary(
    summary: Mapping[str, Any],
    *,
    fold_name: str,
    season_start: int,
    phase: str,
    model_names: tuple[str, ...] | None = None,
) -> None:
    """Require the exact deterministic aggregate-summary schema and identities."""
    _exact_fields(
        summary,
        {
            "fold_name",
            "season_start",
            "phase",
            "evidence_label",
            "target_count",
            "target_skip_reasons",
            "models",
        },
        "fold summary",
    )
    if (
        summary["fold_name"],
        summary["season_start"],
        summary["phase"],
        summary["evidence_label"],
    ) != (fold_name, season_start, phase, "development"):
        raise ValueError("Checkpoint fold summary identity does not match")
    _count(summary["target_count"], "fold target count")
    _reason_counts(summary["target_skip_reasons"], "target skip reasons")
    models = _mapping(summary["models"], "fold models")
    if model_names is not None and set(models) != set(model_names):
        raise ValueError("Checkpoint fold summary model set does not match")
    for name, model_summary in models.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Checkpoint fold summary model names must be non-empty")
        _validate_model_summary(_mapping(model_summary, f"model {name}"))


def _validate_model_summary(summary: Mapping[str, Any]) -> None:
    """Validate one model's aggregate metrics, cohort diagnostics, and skip counts."""
    _exact_fields(summary, {"metrics", "cohort_diagnostics", "skip_reasons"}, "model summary")
    _validate_metrics(_mapping(summary["metrics"], "model metrics"))
    _reason_counts(summary["skip_reasons"], "model skip reasons")
    cohorts = _mapping(summary["cohort_diagnostics"], "cohort diagnostics")
    if set(cohorts) != set(COHORT_NAMES):
        raise ValueError("Checkpoint cohort diagnostic set does not match")
    for cohort, diagnostic in cohorts.items():
        _validate_cohort_diagnostic(_mapping(diagnostic, f"cohort {cohort}"), cohort)


def _validate_metrics(metrics: Mapping[str, Any]) -> None:
    """Validate one serialized BacktestMetrics record."""
    _exact_fields(
        metrics,
        {
            "target_count",
            "sample_count",
            "coverage",
            "mae",
            "rmse",
            "median_absolute_error",
            "intervals",
            "brier_scores",
        },
        "backtest metrics",
    )
    _count(metrics["target_count"], "metrics target count")
    _count(metrics["sample_count"], "metrics sample count")
    _finite(metrics["coverage"], "metrics coverage")
    for field in ("mae", "rmse", "median_absolute_error"):
        _optional_finite(metrics[field], f"metrics {field}")
    intervals = _list(metrics["intervals"], "metrics intervals")
    for interval in intervals:
        record = _mapping(interval, "interval metric")
        _exact_fields(
            record,
            {
                "lower_percentile",
                "upper_percentile",
                "nominal_coverage",
                "observed_coverage",
                "mean_width",
            },
            "interval metric",
        )
        _count(record["lower_percentile"], "lower percentile")
        _count(record["upper_percentile"], "upper percentile")
        _finite(record["nominal_coverage"], "nominal coverage")
        _optional_finite(record["observed_coverage"], "observed coverage")
        _optional_finite(record["mean_width"], "mean interval width")
    for item in _list(metrics["brier_scores"], "brier scores"):
        pair = _pair(item, "brier score")
        _finite(pair[0], "brier threshold")
        _optional_finite(pair[1], "brier value")


def _validate_cohort_diagnostic(diagnostic: Mapping[str, Any], cohort: str) -> None:
    """Validate one serialized CohortDiagnostics record and its component metrics."""
    expected = {
        "cohort",
        "target_count",
        "successful_count",
        "coverage",
        "skip_reasons",
        "full_mixture",
        "participation_brier",
        "participation_sample_count",
        "participation_calibration",
        "minutes_mae",
        "minutes_rmse",
        "minutes_sample_count",
        "rate_mae",
        "rate_rmse",
        "rate_sample_count",
        "control_participation_brier",
        "control_minutes_mae",
        "control_rate_mae",
    }
    _exact_fields(diagnostic, expected, "cohort diagnostic")
    if diagnostic["cohort"] != cohort:
        raise ValueError("Checkpoint cohort diagnostic identity does not match")
    for field in (
        "target_count",
        "successful_count",
        "participation_sample_count",
        "minutes_sample_count",
        "rate_sample_count",
    ):
        _count(diagnostic[field], f"cohort {field}")
    _finite(diagnostic["coverage"], "cohort coverage")
    _reason_counts(diagnostic["skip_reasons"], "cohort skip reasons")
    _validate_metrics(_mapping(diagnostic["full_mixture"], "cohort full mixture"))
    for field in (
        "participation_brier",
        "minutes_mae",
        "minutes_rmse",
        "rate_mae",
        "rate_rmse",
        "control_participation_brier",
        "control_minutes_mae",
        "control_rate_mae",
    ):
        _optional_finite(diagnostic[field], f"cohort {field}")
    for item in _list(diagnostic["participation_calibration"], "participation calibration"):
        record = _mapping(item, "participation calibration bin")
        _exact_fields(
            record,
            {"lower", "upper", "observation_count", "predicted_mean", "observed_frequency"},
            "participation calibration bin",
        )
        _finite(record["lower"], "calibration lower bound")
        _finite(record["upper"], "calibration upper bound")
        _count(record["observation_count"], "calibration observation count")
        _optional_finite(record["predicted_mean"], "calibration predicted mean")
        _optional_finite(record["observed_frequency"], "calibration observed frequency")


def _exact_fields(value: Mapping[str, Any], fields: set[str], label: str) -> None:
    """Reject missing or unknown fields at a persisted summary boundary."""
    if set(value) != fields:
        raise ValueError(f"Checkpoint {label} fields do not match")


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    """Require a string-keyed JSON object."""
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"Checkpoint {label} must be an object")
    return value


def _list(value: object, label: str) -> list[Any]:
    """Require a JSON array."""
    if not isinstance(value, list):
        raise ValueError(f"Checkpoint {label} must be an array")
    return value


def _pair(value: object, label: str) -> tuple[object, object]:
    """Require a two-item JSON array."""
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"Checkpoint {label} must be a two-item array")
    return value[0], value[1]


def _reason_counts(value: object, label: str) -> None:
    """Require string reason keys with non-negative integer counts."""
    mapping = _mapping(value, label)
    for reason, count in mapping.items():
        if not reason:
            raise ValueError(f"Checkpoint {label} keys must be non-empty")
        _count(count, label)


def _count(value: object, label: str) -> None:
    """Require a non-negative integer without accepting booleans."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"Checkpoint {label} must be a non-negative integer")


def _finite(value: object, label: str) -> None:
    """Require a finite JSON number without accepting booleans."""
    if not isinstance(value, int | float) or isinstance(value, bool) or not isfinite(value):
        raise ValueError(f"Checkpoint {label} must be finite")


def _optional_finite(value: object, label: str) -> None:
    """Require either null or a finite JSON number."""
    if value is not None:
        _finite(value, label)


__all__ = ("validate_development_fold_summary",)
