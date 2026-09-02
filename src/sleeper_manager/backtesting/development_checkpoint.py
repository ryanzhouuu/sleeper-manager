"""Manifest-keyed development checkpoints for separate locked evaluation processes.

The checkpoint is the executable continuation authority. This module owns suite-state discovery,
identity validation, canonical hashing, atomic publication, and restoration into fresh models.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from sleeper_manager.backtesting._development_checkpoint_artifact import (
    checkpoint_content_hash,
    load_hash_verified_json,
)
from sleeper_manager.backtesting.artifacts import atomic_write_json, canonicalize
from sleeper_manager.backtesting.calibration import CalibratedProjectionModel
from sleeper_manager.backtesting.development_checkpoint_contract import (
    DEVELOPMENT_CHECKPOINT_VERSION,
    CheckpointModelIdentity,
    DevelopmentCheckpoint,
    DevelopmentCheckpointError,
    DevelopmentFoldCheckpoint,
    NamedCalibratedContinuation,
)
from sleeper_manager.backtesting.development_fold_summary import (
    validate_development_fold_summary,
)
from sleeper_manager.backtesting.models import BacktestModel
from sleeper_manager.backtesting.performance import manifest_fingerprint
from sleeper_manager.backtesting.validation.models import ChronologicalFold
from sleeper_manager.projections.residual_candidates import CachingProjectionModel


def build_development_checkpoint(
    *,
    manifest: Mapping[str, Any],
    folds: tuple[ChronologicalFold, ...],
    fold_summaries: tuple[Mapping[str, Any], ...],
    models: tuple[BacktestModel, ...],
) -> DevelopmentCheckpoint:
    """Build and self-validate a deterministic checkpoint after development succeeds."""
    if len(folds) != len(fold_summaries):
        raise DevelopmentCheckpointError("Checkpoint folds and summaries must have equal length")
    canonical_manifest = canonicalize(manifest)
    if not isinstance(canonical_manifest, dict):
        raise DevelopmentCheckpointError("Projection manifest must be a JSON object")
    identities = _manifest_identities(canonical_manifest)
    fold_records = tuple(
        DevelopmentFoldCheckpoint(
            name=fold.name,
            season_start=fold.season_start,
            phase=fold.phase,
            start_at=fold.start_at,
            end_at=fold.end_at,
            holdout=fold.holdout,
            summary=_canonical_mapping(summary, "fold summary"),
        )
        for fold, summary in zip(folds, fold_summaries, strict=True)
    )
    model_identities = checkpoint_model_identities(models)
    continuations = tuple(
        NamedCalibratedContinuation(name, projector.export_continuation())
        for name, projector in calibrated_projectors(models)
    )
    provisional = DevelopmentCheckpoint(
        checkpoint_version=DEVELOPMENT_CHECKPOINT_VERSION,
        manifest=canonical_manifest,
        manifest_fingerprint=manifest_fingerprint(canonical_manifest),
        source_revision=identities["source_revision"],
        dataset_version=identities["dataset_version"],
        feature_schema_version=identities["feature_schema_version"],
        scoring_policy_version=identities["scoring_policy_version"],
        cohort_config_version=identities["cohort_config_version"],
        backtest_config_version=identities["backtest_config_version"],
        development_folds=fold_records,
        models=model_identities,
        calibrated_continuations=continuations,
        content_hash="pending",
    )
    return _with_content_hash(provisional)


def development_checkpoint_path(workspace: Path, fingerprint: str) -> Path:
    """Return the content-addressed checkpoint path beneath the ignored workspace."""
    if len(fingerprint) != 64 or any(
        character not in "0123456789abcdef" for character in fingerprint
    ):
        raise DevelopmentCheckpointError(
            "Checkpoint manifest fingerprint must be lowercase SHA-256"
        )
    return workspace / "checkpoints" / "projection-evaluation" / f"{fingerprint}.json"


def write_development_checkpoint(path: Path, checkpoint: DevelopmentCheckpoint) -> str:
    """Atomically publish one fully validated checkpoint and return its file digest."""
    _verify_content_hash(checkpoint)
    return atomic_write_json(path, checkpoint.to_dict())


def load_development_checkpoint(
    path: Path,
    *,
    expected_manifest: Mapping[str, Any],
    expected_folds: tuple[ChronologicalFold, ...],
    expected_models: tuple[BacktestModel, ...],
) -> DevelopmentCheckpoint:
    """Load and validate exact current-run identities or instruct a development rerun."""
    try:
        raw = load_hash_verified_json(path)
        from sleeper_manager.backtesting._development_checkpoint_decoding import (
            decode_development_checkpoint,
        )

        checkpoint = decode_development_checkpoint(raw)
        validate_development_checkpoint(
            checkpoint,
            expected_manifest=expected_manifest,
            expected_folds=expected_folds,
            expected_models=expected_models,
        )
        return checkpoint
    except (OSError, KeyError, TypeError, ValueError) as error:
        if isinstance(error, DevelopmentCheckpointError) and "rerun development" in str(error):
            raise
        raise DevelopmentCheckpointError(
            f"Development checkpoint at {path!s} is missing or incompatible; "
            "rerun development evaluation"
        ) from error


def validate_development_checkpoint(
    checkpoint: DevelopmentCheckpoint,
    *,
    expected_manifest: Mapping[str, Any],
    expected_folds: tuple[ChronologicalFold, ...],
    expected_models: tuple[BacktestModel, ...],
) -> None:
    """Require exact manifest, fold, model, and continuation identities before restore."""
    _verify_content_hash(checkpoint)
    canonical_manifest = _canonical_mapping(expected_manifest, "expected manifest")
    if checkpoint.manifest != canonical_manifest:
        raise DevelopmentCheckpointError("Checkpoint manifest does not match; rerun development")
    if checkpoint.manifest_fingerprint != manifest_fingerprint(canonical_manifest):
        raise DevelopmentCheckpointError(
            "Checkpoint manifest fingerprint does not match; rerun development"
        )
    identities = _manifest_identities(canonical_manifest)
    for field, expected in identities.items():
        if getattr(checkpoint, field) != expected:
            raise DevelopmentCheckpointError(
                f"Checkpoint {field} does not match; rerun development"
            )
    expected_fold_identities = tuple(
        (fold.name, fold.season_start, fold.phase, fold.start_at, fold.end_at, fold.holdout)
        for fold in expected_folds
    )
    actual_fold_identities = tuple(
        (fold.name, fold.season_start, fold.phase, fold.start_at, fold.end_at, fold.holdout)
        for fold in checkpoint.development_folds
    )
    if actual_fold_identities != expected_fold_identities:
        raise DevelopmentCheckpointError("Checkpoint fold order does not match; rerun development")
    expected_identities = checkpoint_model_identities(expected_models)
    if checkpoint.models != expected_identities:
        raise DevelopmentCheckpointError("Checkpoint model order does not match; rerun development")
    model_names = tuple(model.name for model in expected_identities)
    for fold in checkpoint.development_folds:
        validate_development_fold_summary(
            fold.summary,
            fold_name=fold.name,
            season_start=fold.season_start,
            phase=fold.phase,
            model_names=model_names,
        )
    expected_stateful_names = tuple(name for name, _ in calibrated_projectors(expected_models))
    actual_stateful_names = tuple(item.model_name for item in checkpoint.calibrated_continuations)
    if actual_stateful_names != expected_stateful_names:
        raise DevelopmentCheckpointError(
            "Checkpoint calibrated continuation set does not match; rerun development"
        )
    expected_projectors = dict(calibrated_projectors(expected_models))
    for item in checkpoint.calibrated_continuations:
        projector = expected_projectors[item.model_name]
        continuation = item.continuation
        wrapped_version = str(
            getattr(
                projector.projector,
                "model_version",
                type(projector.projector).__name__,
            )
        )
        if (
            continuation.model_version != projector.model_version
            or continuation.wrapped_model_version != wrapped_version
            or (
                continuation.min_samples,
                continuation.max_samples,
                continuation.refresh_interval,
            )
            != (
                projector.min_samples,
                projector.max_samples,
                projector.refresh_interval,
            )
        ):
            raise DevelopmentCheckpointError(
                "Checkpoint continuation model configuration does not match; rerun development"
            )
        if item.continuation.dataset_version != checkpoint.dataset_version:
            raise DevelopmentCheckpointError(
                "Checkpoint continuation dataset does not match; rerun development"
            )
        if item.continuation.scoring_policy_version != checkpoint.scoring_policy_version:
            raise DevelopmentCheckpointError(
                "Checkpoint continuation scoring policy does not match; rerun development"
            )


def restore_development_continuations(
    checkpoint: DevelopmentCheckpoint,
    models: tuple[BacktestModel, ...],
    *,
    on_restored: Callable[[int, int, str], None] | None = None,
) -> None:
    """Restore named calibrated state into the explicit fresh cached-model wrapper shape."""
    projectors = calibrated_projectors(models)
    expected_names = tuple(name for name, _ in projectors)
    actual_names = tuple(item.model_name for item in checkpoint.calibrated_continuations)
    if actual_names != expected_names:
        raise DevelopmentCheckpointError(
            "Checkpoint calibrated continuation set does not match; rerun development"
        )
    states = {item.model_name: item.continuation for item in checkpoint.calibrated_continuations}
    total = len(projectors)
    for position, (name, projector) in enumerate(projectors, start=1):
        try:
            continuation = states[name]
        except KeyError as error:
            raise DevelopmentCheckpointError(
                f"Checkpoint has no continuation for {name}; rerun development"
            ) from error
        projector.restore_continuation(
            continuation,
            dataset_version=checkpoint.dataset_version,
            scoring_policy_version=checkpoint.scoring_policy_version,
        )
        if on_restored is not None:
            on_restored(position, total, name)


def checkpoint_model_identities(
    models: tuple[BacktestModel, ...],
) -> tuple[CheckpointModelIdentity, ...]:
    """Return the exact ordered name and projector-version identity of a model suite."""
    return tuple(
        CheckpointModelIdentity(
            model.name,
            str(getattr(model.projector, "model_version", type(model.projector).__name__)),
        )
        for model in models
    )


def calibrated_projectors(
    models: tuple[BacktestModel, ...],
) -> tuple[tuple[str, CalibratedProjectionModel], ...]:
    """Discover only the frozen suite's explicit cache-then-calibration wrapper shape."""
    records: list[tuple[str, CalibratedProjectionModel]] = []
    for model in models:
        cached = model.projector
        if isinstance(cached, CachingProjectionModel) and isinstance(
            cached.projector, CalibratedProjectionModel
        ):
            records.append((model.name, cached.projector))
    return tuple(records)


def checkpoint_fold_summaries(
    checkpoint: DevelopmentCheckpoint,
) -> tuple[Mapping[str, Any], ...]:
    """Return validated development summaries in their original chronological order."""
    return tuple(fold.summary for fold in checkpoint.development_folds)


def _with_content_hash(checkpoint: DevelopmentCheckpoint) -> DevelopmentCheckpoint:
    """Replace the provisional hash with the canonical payload digest."""
    return replace(
        checkpoint,
        content_hash=checkpoint_content_hash(checkpoint.payload()),
    )


def _verify_content_hash(checkpoint: DevelopmentCheckpoint) -> None:
    """Reject an object whose stored digest differs from its canonical payload."""
    expected = checkpoint_content_hash(checkpoint.payload())
    if checkpoint.content_hash != expected:
        raise DevelopmentCheckpointError("Checkpoint content hash does not match")


def _manifest_identities(manifest: Mapping[str, Any]) -> dict[str, str]:
    """Extract the redundant checkpoint identities from an exact frozen manifest."""
    fields = (
        "source_revision",
        "dataset_version",
        "feature_schema_version",
        "scoring_policy_version",
        "cohort_config_version",
        "backtest_config_version",
    )
    identities: dict[str, str] = {}
    for field in fields:
        value = manifest.get(field)
        if not isinstance(value, str) or not value.strip():
            raise DevelopmentCheckpointError(f"Projection manifest requires {field}")
        identities[field] = value
    return identities


def _canonical_mapping(value: object, label: str) -> dict[str, Any]:
    """Canonicalize a mapping while rejecting non-object results."""
    canonical = canonicalize(value)
    if not isinstance(canonical, dict):
        raise DevelopmentCheckpointError(f"Checkpoint {label} must be a JSON object")
    return canonical


__all__ = (
    "DEVELOPMENT_CHECKPOINT_VERSION",
    "CheckpointModelIdentity",
    "DevelopmentCheckpoint",
    "DevelopmentCheckpointError",
    "DevelopmentFoldCheckpoint",
    "NamedCalibratedContinuation",
    "build_development_checkpoint",
    "calibrated_projectors",
    "checkpoint_fold_summaries",
    "checkpoint_model_identities",
    "development_checkpoint_path",
    "load_development_checkpoint",
    "restore_development_continuations",
    "validate_development_checkpoint",
    "write_development_checkpoint",
)
