"""Verify development checkpoints fail closed under corruption or identity drift."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from checkpoint_support import development_evidence, manifest

from sleeper_manager.backtesting.artifacts import canonical_json_bytes, sha256_bytes
from sleeper_manager.backtesting.calibration import (
    CalibratedProjectionContinuation,
    CalibratedProjectionModel,
)
from sleeper_manager.backtesting.controls import NaiveProjectionBaseline
from sleeper_manager.backtesting.development_checkpoint import (
    DevelopmentCheckpoint,
    DevelopmentCheckpointError,
    build_development_checkpoint,
    load_development_checkpoint,
    validate_development_checkpoint,
    write_development_checkpoint,
)
from sleeper_manager.backtesting.models import BacktestModel
from sleeper_manager.projections.residual_candidates import CachingProjectionModel


def _rehash(checkpoint: DevelopmentCheckpoint) -> DevelopmentCheckpoint:
    """Recompute a checkpoint hash after one intentional test-only mutation."""
    provisional = replace(checkpoint, content_hash="pending")
    return replace(
        provisional,
        content_hash=sha256_bytes(canonical_json_bytes(provisional.payload())),
    )


def _replace_continuation(
    checkpoint: DevelopmentCheckpoint,
    continuation: CalibratedProjectionContinuation,
) -> DevelopmentCheckpoint:
    """Replace the fixture continuation and restore a valid envelope hash."""
    named = replace(checkpoint.calibrated_continuations[0], continuation=continuation)
    return _rehash(replace(checkpoint, calibrated_continuations=(named,)))


def _checkpoint() -> tuple[DevelopmentCheckpoint, tuple[BacktestModel, ...], object, object]:
    """Return one initialized checkpoint and its exact expected identities."""
    _, suite, development, summary = development_evidence()
    exact_manifest = manifest(suite)
    checkpoint = build_development_checkpoint(
        manifest=exact_manifest,
        folds=(development,),
        fold_summaries=(summary,),
        models=suite,
    )
    return checkpoint, suite, development, exact_manifest


def test_checkpoint_load_rejects_missing_truncated_and_tampered_files(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Missing, malformed, hash-mismatched, and stale artifacts require development."""
    checkpoint, suite, development, exact_manifest = _checkpoint()
    path = tmp_path / "checkpoint.json"

    for content in (None, '{"checkpoint_version":'):
        if content is not None:
            path.write_text(content)
        with pytest.raises(DevelopmentCheckpointError, match="rerun development"):
            load_development_checkpoint(
                path,
                expected_manifest=exact_manifest,
                expected_folds=(development,),
                expected_models=suite,
            )

    write_development_checkpoint(path, checkpoint)
    payload = json.loads(path.read_text())
    payload["dataset_version"] = "tampered"
    path.write_text(json.dumps(payload))
    with pytest.raises(DevelopmentCheckpointError, match="rerun development"):
        load_development_checkpoint(
            path,
            expected_manifest=exact_manifest,
            expected_folds=(development,),
            expected_models=suite,
        )

    write_development_checkpoint(path, checkpoint)
    drifted_manifest = {**exact_manifest, "source_revision": "different-revision"}
    with pytest.raises(DevelopmentCheckpointError, match="rerun development"):
        load_development_checkpoint(
            path,
            expected_manifest=drifted_manifest,
            expected_folds=(development,),
            expected_models=suite,
        )


def test_checkpoint_validation_rejects_fold_suite_and_continuation_drift() -> None:
    """Fold, model, continuation-set, identity, and configuration drift fail closed."""
    checkpoint, suite, development, exact_manifest = _checkpoint()
    shifted_fold = replace(development, name="different-development-fold")
    with pytest.raises(DevelopmentCheckpointError, match="fold order"):
        validate_development_checkpoint(
            checkpoint,
            expected_manifest=exact_manifest,
            expected_folds=(shifted_fold,),
            expected_models=suite,
        )

    differently_configured = (
        BacktestModel(
            "calibrated",
            CachingProjectionModel(
                CalibratedProjectionModel(
                    NaiveProjectionBaseline("season_average"),
                    min_samples=2,
                    max_samples=4,
                    refresh_interval=2,
                ),
                max_entries=16,
            ),
        ),
    )
    with pytest.raises(DevelopmentCheckpointError, match="model order"):
        validate_development_checkpoint(
            checkpoint,
            expected_manifest=exact_manifest,
            expected_folds=(development,),
            expected_models=differently_configured,
        )

    missing = _rehash(replace(checkpoint, calibrated_continuations=()))
    with pytest.raises(DevelopmentCheckpointError, match="continuation set"):
        validate_development_checkpoint(
            missing,
            expected_manifest=exact_manifest,
            expected_folds=(development,),
            expected_models=suite,
        )

    continuation = checkpoint.calibrated_continuations[0].continuation
    for drifted in (
        replace(continuation, dataset_version="wrong-dataset"),
        replace(continuation, scoring_policy_version="wrong-policy"),
        replace(continuation, wrapped_model_version="wrong-model-version"),
        replace(continuation, min_samples=1),
    ):
        with pytest.raises(DevelopmentCheckpointError, match="rerun development"):
            validate_development_checkpoint(
                _replace_continuation(checkpoint, drifted),
                expected_manifest=exact_manifest,
                expected_folds=(development,),
                expected_models=suite,
            )


def test_checkpoint_rejects_extra_and_duplicate_continuations() -> None:
    """The named continuation collection must match the suite exactly and uniquely."""
    checkpoint, suite, development, exact_manifest = _checkpoint()
    continuation = checkpoint.calibrated_continuations[0]
    extra_named = replace(continuation, model_name="extra")
    extra = _rehash(
        replace(
            checkpoint,
            calibrated_continuations=(continuation, extra_named),
        )
    )
    with pytest.raises(DevelopmentCheckpointError, match="continuation set"):
        validate_development_checkpoint(
            extra,
            expected_manifest=exact_manifest,
            expected_folds=(development,),
            expected_models=suite,
        )

    with pytest.raises(DevelopmentCheckpointError, match="unique"):
        replace(
            checkpoint,
            calibrated_continuations=(continuation, continuation),
        )
