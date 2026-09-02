"""Typed schema objects for deterministic projection-development checkpoints."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sleeper_manager.backtesting.artifacts import canonicalize
from sleeper_manager.backtesting.calibration_continuation import (
    CalibratedProjectionContinuation,
)
from sleeper_manager.backtesting.development_fold_summary import (
    validate_development_fold_summary,
)

DEVELOPMENT_CHECKPOINT_VERSION = "projection-evaluation-development-checkpoint-v1"


class DevelopmentCheckpointError(ValueError):
    """Raised when development continuation cannot be trusted or restored."""


@dataclass(frozen=True, slots=True)
class CheckpointModelIdentity:
    """One ordered model name and projector-version pair."""

    name: str
    version: str

    def __post_init__(self) -> None:
        """Reject empty model identities."""
        if not self.name.strip() or not self.version.strip():
            raise DevelopmentCheckpointError("Checkpoint model identities must be non-empty")


@dataclass(frozen=True, slots=True)
class NamedCalibratedContinuation:
    """Continuation state associated with its exact backtest model name."""

    model_name: str
    continuation: CalibratedProjectionContinuation

    def __post_init__(self) -> None:
        """Reject empty continuation model names."""
        if not self.model_name.strip():
            raise DevelopmentCheckpointError(
                "Checkpoint continuation model names must be non-empty"
            )


@dataclass(frozen=True, slots=True)
class DevelopmentFoldCheckpoint:
    """One exact development fold definition paired with its aggregate summary."""

    name: str
    season_start: int
    phase: str
    start_at: datetime
    end_at: datetime
    holdout: bool
    summary: Mapping[str, Any]

    def __post_init__(self) -> None:
        """Validate fold identity, ordering, and canonical summary structure."""
        if not self.name.strip() or not self.phase.strip():
            raise DevelopmentCheckpointError("Checkpoint fold identity must be non-empty")
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise DevelopmentCheckpointError("Checkpoint fold timestamps must be timezone-aware")
        if self.start_at > self.end_at:
            raise DevelopmentCheckpointError("Checkpoint fold start cannot follow its end")
        if self.holdout:
            raise DevelopmentCheckpointError("Development checkpoints cannot contain holdout folds")
        validate_development_fold_summary(
            self.summary,
            fold_name=self.name,
            season_start=self.season_start,
            phase=self.phase,
        )


@dataclass(frozen=True, slots=True)
class DevelopmentCheckpoint:
    """Complete deterministic development evidence and calibrated continuation state."""

    checkpoint_version: str
    manifest: Mapping[str, Any]
    manifest_fingerprint: str
    source_revision: str
    dataset_version: str
    feature_schema_version: str
    scoring_policy_version: str
    cohort_config_version: str
    backtest_config_version: str
    development_folds: tuple[DevelopmentFoldCheckpoint, ...]
    models: tuple[CheckpointModelIdentity, ...]
    calibrated_continuations: tuple[NamedCalibratedContinuation, ...]
    content_hash: str

    def __post_init__(self) -> None:
        """Validate envelope identities before hashing or semantic matching."""
        if self.checkpoint_version != DEVELOPMENT_CHECKPOINT_VERSION:
            raise DevelopmentCheckpointError("Unsupported development checkpoint version")
        for label, value in (
            ("manifest fingerprint", self.manifest_fingerprint),
            ("source revision", self.source_revision),
            ("dataset version", self.dataset_version),
            ("feature schema version", self.feature_schema_version),
            ("scoring policy version", self.scoring_policy_version),
            ("cohort config version", self.cohort_config_version),
            ("backtest config version", self.backtest_config_version),
            ("content hash", self.content_hash),
        ):
            if not value.strip():
                raise DevelopmentCheckpointError(f"Checkpoint {label} must be non-empty")
        _require_unique(tuple(model.name for model in self.models), "model names")
        _require_unique(
            tuple(item.model_name for item in self.calibrated_continuations),
            "continuation model names",
        )
        _require_unique(tuple(fold.name for fold in self.development_folds), "fold names")

    def payload(self) -> dict[str, Any]:
        """Return the canonical hash input without the self-referential content hash."""
        payload = canonicalize(self)
        assert isinstance(payload, dict)
        del payload["content_hash"]
        return payload

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical persisted envelope including its verified content hash."""
        return {**self.payload(), "content_hash": self.content_hash}


def _require_unique(values: tuple[str, ...], label: str) -> None:
    """Reject duplicate ordered identities in checkpoint contracts."""
    if len(set(values)) != len(values):
        raise DevelopmentCheckpointError(f"Checkpoint {label} must be unique")


__all__ = (
    "DEVELOPMENT_CHECKPOINT_VERSION",
    "CheckpointModelIdentity",
    "DevelopmentCheckpoint",
    "DevelopmentCheckpointError",
    "DevelopmentFoldCheckpoint",
    "NamedCalibratedContinuation",
)
