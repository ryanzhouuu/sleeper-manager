"""Strict persisted-state contract for chronological residual calibration.

This module validates JSON-facing state independently from projector execution so malformed or
incompatible checkpoints fail before they can mutate a live model.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Any

from sleeper_manager.backtesting.models import BacktestError

CALIBRATED_CONTINUATION_VERSION = "calibrated-projection-continuation-v1"


@dataclass(frozen=True, slots=True)
class PendingCalibrationContinuation:
    """One unresolved projection retained until a strictly later target is evaluated."""

    player_id: str
    game_id: str
    game_start: datetime
    expected_value: float

    def __post_init__(self) -> None:
        if not self.player_id or not self.game_id:
            raise BacktestError("Pending calibration keys must be non-empty")
        if self.game_start.tzinfo is None:
            raise BacktestError("Pending calibration game starts must be timezone-aware")
        if not isfinite(self.expected_value):
            raise BacktestError("Pending calibration expectations must be finite")


@dataclass(frozen=True, slots=True)
class CalibratedProjectionContinuation:
    """Typed state sufficient to resume one calibrated projector exactly."""

    continuation_version: str
    model_version: str
    wrapped_model_version: str
    min_samples: int
    max_samples: int
    refresh_interval: int
    dataset_version: str
    scoring_policy_version: str
    residuals: tuple[float, ...]
    total_residual_count: int
    last_refresh_count: int
    sorted_residuals: tuple[float, ...]
    residual_mean: float
    pending: tuple[PendingCalibrationContinuation, ...]
    last_evaluated_game_start: datetime

    def __post_init__(self) -> None:
        self._validate_versions()
        self._validate_residuals()
        self._validate_pending()

    def _validate_versions(self) -> None:
        """Require the complete identity and configuration needed for exact restore."""
        for label, value in (
            ("continuation version", self.continuation_version),
            ("model version", self.model_version),
            ("wrapped model version", self.wrapped_model_version),
            ("dataset version", self.dataset_version),
            ("scoring policy version", self.scoring_policy_version),
        ):
            if not value.strip():
                raise BacktestError(f"Calibration {label} must be non-empty")
        if self.continuation_version != CALIBRATED_CONTINUATION_VERSION:
            raise BacktestError("Unsupported calibrated continuation version")
        if self.min_samples <= 0:
            raise BacktestError("Calibration minimum samples must be positive")
        if self.max_samples < self.min_samples:
            raise BacktestError("Calibration sample capacity must cover minimum samples")
        if self.refresh_interval <= 0:
            raise BacktestError("Calibration refresh interval must be positive")
        if self.last_evaluated_game_start.tzinfo is None:
            raise BacktestError("Calibration last game start must be timezone-aware")

    def _validate_residuals(self) -> None:
        """Validate counters and materialized residual snapshots without recomputing them."""
        if self.total_residual_count < 0 or self.last_refresh_count < 0:
            raise BacktestError("Calibration residual counters must be non-negative")
        if self.last_refresh_count > self.total_residual_count:
            raise BacktestError("Calibration refresh count cannot exceed total residuals")
        expected_residual_count = min(self.total_residual_count, self.max_samples)
        if len(self.residuals) != expected_residual_count:
            raise BacktestError("Calibration residual capacity does not match its total count")
        if any(not isfinite(value) for value in self.residuals):
            raise BacktestError("Calibration residuals must be finite")
        expected_sorted_count = min(self.last_refresh_count, self.max_samples)
        if len(self.sorted_residuals) != expected_sorted_count:
            raise BacktestError("Sorted calibration residuals do not match the refresh count")
        if any(not isfinite(value) for value in self.sorted_residuals):
            raise BacktestError("Sorted calibration residuals must be finite")
        if tuple(sorted(self.sorted_residuals)) != self.sorted_residuals:
            raise BacktestError("Sorted calibration residuals must be nondecreasing")
        if not isfinite(self.residual_mean):
            raise BacktestError("Calibration residual mean must be finite")
        if self.last_refresh_count == 0:
            first_refresh = max(self.min_samples, self.refresh_interval)
            if self.total_residual_count >= first_refresh:
                raise BacktestError("Calibration state omitted a required residual refresh")
            if self.residual_mean != 0.0:
                raise BacktestError("Unrefreshed calibration state must have a zero mean")
        elif self.total_residual_count - self.last_refresh_count >= self.refresh_interval:
            raise BacktestError("Calibration state omitted a required residual refresh")

    def _validate_pending(self) -> None:
        """Require unique pending keys at the last evaluated tipoff, preserving their order."""
        keys = tuple((item.player_id, item.game_id) for item in self.pending)
        if len(set(keys)) != len(keys):
            raise BacktestError("Pending calibration keys must be unique")
        if any(item.game_start != self.last_evaluated_game_start for item in self.pending):
            raise BacktestError("Pending calibrations must share the last evaluated tipoff")


def decode_calibrated_continuation(payload: object) -> CalibratedProjectionContinuation:
    """Decode a strict JSON continuation object without accepting schema drift."""
    mapping = _strict_mapping(
        payload,
        "calibrated continuation",
        {
            "continuation_version",
            "model_version",
            "wrapped_model_version",
            "min_samples",
            "max_samples",
            "refresh_interval",
            "dataset_version",
            "scoring_policy_version",
            "residuals",
            "total_residual_count",
            "last_refresh_count",
            "sorted_residuals",
            "residual_mean",
            "pending",
            "last_evaluated_game_start",
        },
    )
    return CalibratedProjectionContinuation(
        continuation_version=_string(mapping, "continuation_version"),
        model_version=_string(mapping, "model_version"),
        wrapped_model_version=_string(mapping, "wrapped_model_version"),
        min_samples=_integer(mapping, "min_samples"),
        max_samples=_integer(mapping, "max_samples"),
        refresh_interval=_integer(mapping, "refresh_interval"),
        dataset_version=_string(mapping, "dataset_version"),
        scoring_policy_version=_string(mapping, "scoring_policy_version"),
        residuals=_float_tuple(mapping, "residuals"),
        total_residual_count=_integer(mapping, "total_residual_count"),
        last_refresh_count=_integer(mapping, "last_refresh_count"),
        sorted_residuals=_float_tuple(mapping, "sorted_residuals"),
        residual_mean=_number(mapping, "residual_mean"),
        pending=tuple(_decode_pending(item) for item in _list(mapping, "pending")),
        last_evaluated_game_start=_datetime(mapping, "last_evaluated_game_start"),
    )


def _decode_pending(payload: object) -> PendingCalibrationContinuation:
    """Decode one strict pending-continuation record."""
    mapping = _strict_mapping(
        payload,
        "pending calibration",
        {"player_id", "game_id", "game_start", "expected_value"},
    )
    return PendingCalibrationContinuation(
        player_id=_string(mapping, "player_id"),
        game_id=_string(mapping, "game_id"),
        game_start=_datetime(mapping, "game_start"),
        expected_value=_number(mapping, "expected_value"),
    )


def _strict_mapping(payload: object, label: str, fields: set[str]) -> dict[str, Any]:
    """Require a JSON object with exactly the requested field names."""
    if not isinstance(payload, dict):
        raise BacktestError(f"{label} must be a JSON object")
    if set(payload) != fields:
        raise BacktestError(f"{label} fields do not match the supported schema")
    return payload


def _string(payload: Mapping[str, Any], field: str) -> str:
    """Require one non-empty string field."""
    value = payload[field]
    if not isinstance(value, str) or not value.strip():
        raise BacktestError(f"Calibration field {field} must be a non-empty string")
    return value


def _integer(payload: Mapping[str, Any], field: str) -> int:
    """Require one integer field without accepting booleans."""
    value = payload[field]
    if not isinstance(value, int) or isinstance(value, bool):
        raise BacktestError(f"Calibration field {field} must be an integer")
    return value


def _number(payload: Mapping[str, Any], field: str) -> float:
    """Require one finite JSON numeric field without accepting booleans."""
    value = payload[field]
    if not isinstance(value, int | float) or isinstance(value, bool) or not isfinite(value):
        raise BacktestError(f"Calibration field {field} must be a finite number")
    return float(value)


def _list(payload: Mapping[str, Any], field: str) -> list[Any]:
    """Require one JSON array field."""
    value = payload[field]
    if not isinstance(value, list):
        raise BacktestError(f"Calibration field {field} must be an array")
    return value


def _float_tuple(payload: Mapping[str, Any], field: str) -> tuple[float, ...]:
    """Decode a finite numeric array while preserving JSON numeric values."""
    return tuple(_number({field: value}, field) for value in _list(payload, field))


def _datetime(payload: Mapping[str, Any], field: str) -> datetime:
    """Decode one timezone-aware ISO-8601 timestamp."""
    raw = _string(payload, field)
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as error:
        raise BacktestError(f"Calibration field {field} must be an ISO timestamp") from error
    if value.tzinfo is None:
        raise BacktestError(f"Calibration field {field} must be timezone-aware")
    return value


__all__ = (
    "CALIBRATED_CONTINUATION_VERSION",
    "CalibratedProjectionContinuation",
    "PendingCalibrationContinuation",
    "decode_calibrated_continuation",
)
