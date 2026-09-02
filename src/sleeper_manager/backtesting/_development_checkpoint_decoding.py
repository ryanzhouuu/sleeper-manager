"""Strict JSON decoder for projection-evaluation development checkpoints."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sleeper_manager.backtesting.calibration_continuation import (
    decode_calibrated_continuation,
)
from sleeper_manager.backtesting.development_checkpoint_contract import (
    CheckpointModelIdentity,
    DevelopmentCheckpoint,
    DevelopmentCheckpointError,
    DevelopmentFoldCheckpoint,
    NamedCalibratedContinuation,
)


def decode_development_checkpoint(payload: object) -> DevelopmentCheckpoint:
    """Decode a hash-verified envelope without accepting schema drift or coercion."""
    mapping = _strict_mapping(
        payload,
        "checkpoint",
        {
            "checkpoint_version",
            "manifest",
            "manifest_fingerprint",
            "source_revision",
            "dataset_version",
            "feature_schema_version",
            "scoring_policy_version",
            "cohort_config_version",
            "backtest_config_version",
            "development_folds",
            "models",
            "calibrated_continuations",
            "content_hash",
        },
    )
    return DevelopmentCheckpoint(
        checkpoint_version=_string(mapping, "checkpoint_version"),
        manifest=_mapping(mapping, "manifest"),
        manifest_fingerprint=_string(mapping, "manifest_fingerprint"),
        source_revision=_string(mapping, "source_revision"),
        dataset_version=_string(mapping, "dataset_version"),
        feature_schema_version=_string(mapping, "feature_schema_version"),
        scoring_policy_version=_string(mapping, "scoring_policy_version"),
        cohort_config_version=_string(mapping, "cohort_config_version"),
        backtest_config_version=_string(mapping, "backtest_config_version"),
        development_folds=tuple(_decode_fold(item) for item in _list(mapping, "development_folds")),
        models=tuple(_decode_model(item) for item in _list(mapping, "models")),
        calibrated_continuations=tuple(
            _decode_named_continuation(item) for item in _list(mapping, "calibrated_continuations")
        ),
        content_hash=_string(mapping, "content_hash"),
    )


def _decode_fold(payload: object) -> DevelopmentFoldCheckpoint:
    """Decode one exact fold definition and aggregate summary."""
    mapping = _strict_mapping(
        payload,
        "development fold",
        {"name", "season_start", "phase", "start_at", "end_at", "holdout", "summary"},
    )
    return DevelopmentFoldCheckpoint(
        name=_string(mapping, "name"),
        season_start=_integer(mapping, "season_start"),
        phase=_string(mapping, "phase"),
        start_at=_datetime(mapping, "start_at"),
        end_at=_datetime(mapping, "end_at"),
        holdout=_boolean(mapping, "holdout"),
        summary=_mapping(mapping, "summary"),
    )


def _decode_model(payload: object) -> CheckpointModelIdentity:
    """Decode one ordered model identity."""
    mapping = _strict_mapping(payload, "model identity", {"name", "version"})
    return CheckpointModelIdentity(
        name=_string(mapping, "name"),
        version=_string(mapping, "version"),
    )


def _decode_named_continuation(payload: object) -> NamedCalibratedContinuation:
    """Decode one named calibrated-model continuation."""
    mapping = _strict_mapping(payload, "named continuation", {"model_name", "continuation"})
    return NamedCalibratedContinuation(
        model_name=_string(mapping, "model_name"),
        continuation=decode_calibrated_continuation(mapping["continuation"]),
    )


def _strict_mapping(payload: object, label: str, fields: set[str]) -> dict[str, Any]:
    """Require an object with exactly the supported fields."""
    if not isinstance(payload, dict):
        raise DevelopmentCheckpointError(f"Checkpoint {label} must be an object")
    if set(payload) != fields:
        raise DevelopmentCheckpointError(f"Checkpoint {label} fields do not match")
    return payload


def _mapping(payload: Mapping[str, Any], field: str) -> dict[str, Any]:
    """Require one object-valued field."""
    value = payload[field]
    if not isinstance(value, dict):
        raise DevelopmentCheckpointError(f"Checkpoint field {field} must be an object")
    return value


def _list(payload: Mapping[str, Any], field: str) -> list[Any]:
    """Require one array-valued field."""
    value = payload[field]
    if not isinstance(value, list):
        raise DevelopmentCheckpointError(f"Checkpoint field {field} must be an array")
    return value


def _string(payload: Mapping[str, Any], field: str) -> str:
    """Require one non-empty string field."""
    value = payload[field]
    if not isinstance(value, str) or not value.strip():
        raise DevelopmentCheckpointError(f"Checkpoint field {field} must be a non-empty string")
    return value


def _integer(payload: Mapping[str, Any], field: str) -> int:
    """Require one integer field without accepting booleans."""
    value = payload[field]
    if not isinstance(value, int) or isinstance(value, bool):
        raise DevelopmentCheckpointError(f"Checkpoint field {field} must be an integer")
    return value


def _boolean(payload: Mapping[str, Any], field: str) -> bool:
    """Require one boolean field."""
    value = payload[field]
    if not isinstance(value, bool):
        raise DevelopmentCheckpointError(f"Checkpoint field {field} must be a boolean")
    return value


def _datetime(payload: Mapping[str, Any], field: str) -> datetime:
    """Decode one timezone-aware ISO-8601 timestamp."""
    raw = _string(payload, field)
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as error:
        raise DevelopmentCheckpointError(
            f"Checkpoint field {field} must be an ISO timestamp"
        ) from error
    if value.tzinfo is None:
        raise DevelopmentCheckpointError(f"Checkpoint field {field} must be timezone-aware")
    return value


__all__ = ("decode_development_checkpoint",)
