"""Verify deterministic checkpoint envelopes and strict JSON decoding."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from checkpoint_support import development_evidence, manifest

from sleeper_manager.backtesting._development_checkpoint_artifact import (
    checkpoint_content_hash,
    load_hash_verified_json,
)
from sleeper_manager.backtesting._development_checkpoint_decoding import (
    decode_development_checkpoint,
)
from sleeper_manager.backtesting.artifacts import canonicalize
from sleeper_manager.backtesting.development_checkpoint_contract import (
    DEVELOPMENT_CHECKPOINT_VERSION,
    CheckpointModelIdentity,
    DevelopmentCheckpoint,
    DevelopmentCheckpointError,
    DevelopmentFoldCheckpoint,
    NamedCalibratedContinuation,
)
from sleeper_manager.backtesting.performance import manifest_fingerprint


def _checkpoint() -> DevelopmentCheckpoint:
    """Build a valid deterministic envelope without checkpoint orchestration helpers."""
    _, suite, development, summary = development_evidence()
    exact_manifest = canonicalize(manifest(suite))
    assert isinstance(exact_manifest, dict)
    calibrated = suite[0].projector.projector
    provisional = DevelopmentCheckpoint(
        checkpoint_version=DEVELOPMENT_CHECKPOINT_VERSION,
        manifest=exact_manifest,
        manifest_fingerprint=manifest_fingerprint(exact_manifest),
        source_revision="fixture-revision",
        dataset_version="checkpoint-fixture-v1",
        feature_schema_version="checkpoint-schema-v1",
        scoring_policy_version=exact_manifest["scoring_policy_version"],
        cohort_config_version="cohort-fixture-v1",
        backtest_config_version=exact_manifest["backtest_config_version"],
        development_folds=(
            DevelopmentFoldCheckpoint(
                name=development.name,
                season_start=development.season_start,
                phase=development.phase,
                start_at=development.start_at,
                end_at=development.end_at,
                holdout=development.holdout,
                summary=summary,
            ),
        ),
        models=(CheckpointModelIdentity(suite[0].name, suite[0].projector.model_version),),
        calibrated_continuations=(
            NamedCalibratedContinuation(
                suite[0].name,
                calibrated.export_continuation(),
            ),
        ),
        content_hash="pending",
    )
    return replace(
        provisional,
        content_hash=checkpoint_content_hash(provisional.payload()),
    )


def _rehash(payload: dict[str, object]) -> dict[str, object]:
    """Restore a valid content hash after one decoder-focused mutation."""
    content = dict(payload)
    content.pop("content_hash", None)
    return {**content, "content_hash": checkpoint_content_hash(content)}


def test_checkpoint_envelope_round_trips_after_hash_verification(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Canonical checkpoint bytes decode into the exact typed envelope."""
    checkpoint = _checkpoint()
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps(checkpoint.to_dict()))

    assert decode_development_checkpoint(load_hash_verified_json(path)) == checkpoint


def test_checkpoint_hash_verification_rejects_corruption_and_duplicate_keys(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Semantic decoding never sees tampered or ambiguous JSON objects."""
    checkpoint = _checkpoint()
    path = tmp_path / "checkpoint.json"
    payload = checkpoint.to_dict()
    payload["dataset_version"] = "tampered"
    path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="hash"):
        load_hash_verified_json(path)

    path.write_text('{"content_hash":"first","content_hash":"second"}')
    with pytest.raises(ValueError, match="duplicate"):
        load_hash_verified_json(path)


def test_checkpoint_decoder_rejects_schema_types_and_naive_timestamps() -> None:
    """Unknown fields, booleans-as-integers, and naive fold times fail closed."""
    checkpoint = _checkpoint()
    payload = checkpoint.to_dict()

    with pytest.raises(DevelopmentCheckpointError, match="fields"):
        decode_development_checkpoint(_rehash({**payload, "unknown": True}))

    fold = dict(payload["development_folds"][0])
    fold["season_start"] = True
    with pytest.raises(DevelopmentCheckpointError, match="integer"):
        decode_development_checkpoint(_rehash({**payload, "development_folds": [fold]}))

    fold["season_start"] = 2025
    fold["start_at"] = "2025-11-01T00:00:00"
    with pytest.raises(DevelopmentCheckpointError, match="timezone-aware"):
        decode_development_checkpoint(_rehash({**payload, "development_folds": [fold]}))
