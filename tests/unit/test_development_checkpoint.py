"""Verify deterministic checkpoint publication and exact locked restoration."""

from __future__ import annotations

from checkpoint_support import POLICY, dataset, development_evidence, folds, manifest, models

from sleeper_manager.backtesting.artifacts import canonicalize
from sleeper_manager.backtesting.development_checkpoint import (
    build_development_checkpoint,
    checkpoint_fold_summaries,
    development_checkpoint_path,
    load_development_checkpoint,
    restore_development_continuations,
    write_development_checkpoint,
)
from sleeper_manager.backtesting.experiments.projection_evaluation_report import (
    fold_summary,
)
from sleeper_manager.backtesting.models import BacktestConfig
from sleeper_manager.backtesting.validation.folds import run_validation_folds


def test_checkpoint_is_deterministic_and_round_trips_strictly(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Identical evidence publishes identical bytes and loads with every identity intact."""
    _, suite, development, summary = development_evidence()
    exact_manifest = manifest(suite)
    checkpoint = build_development_checkpoint(
        manifest=exact_manifest,
        folds=(development,),
        fold_summaries=(summary,),
        models=suite,
    )
    path = development_checkpoint_path(tmp_path, checkpoint.manifest_fingerprint)

    write_development_checkpoint(path, checkpoint)
    first = path.read_bytes()
    write_development_checkpoint(path, checkpoint)

    assert path.read_bytes() == first
    loaded = load_development_checkpoint(
        path,
        expected_manifest=exact_manifest,
        expected_folds=(development,),
        expected_models=suite,
    )
    assert loaded == checkpoint
    assert checkpoint_fold_summaries(loaded) == (canonicalize(summary),)


def test_restored_locked_fold_matches_uninterrupted_execution(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Separate-process restoration preserves locked observations and diagnostics."""
    historical_dataset, continuous_suite, development, summary = development_evidence()
    exact_manifest = manifest(continuous_suite)
    checkpoint = build_development_checkpoint(
        manifest=exact_manifest,
        folds=(development,),
        fold_summaries=(summary,),
        models=continuous_suite,
    )
    path = development_checkpoint_path(tmp_path, checkpoint.manifest_fingerprint)
    write_development_checkpoint(path, checkpoint)
    _, locked = folds()

    continuous = run_validation_folds(
        historical_dataset,
        scoring_policy=POLICY,
        models=continuous_suite,
        folds=(locked,),
        config=BacktestConfig(),
        reference_model="calibrated",
    )[0]
    restored_suite = models()
    loaded = load_development_checkpoint(
        path,
        expected_manifest=exact_manifest,
        expected_folds=(development,),
        expected_models=restored_suite,
    )
    restored_names: list[str] = []
    restore_development_continuations(
        loaded,
        restored_suite,
        on_restored=lambda _position, _total, name: restored_names.append(name),
    )
    restored = run_validation_folds(
        dataset(),
        scoring_policy=POLICY,
        models=restored_suite,
        folds=(locked,),
        config=BacktestConfig(),
        reference_model="calibrated",
    )[0]

    assert restored_names == ["calibrated"]
    assert restored.report.model_results[0].observations == (
        continuous.report.model_results[0].observations
    )
    assert fold_summary(restored) == fold_summary(continuous)
