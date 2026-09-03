"""Verify projection-evaluation stage orchestration without running the real dataset."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sleeper_manager.backtesting.development_checkpoint import DevelopmentCheckpointError
from sleeper_manager.backtesting.experiments import projection_evaluation as evaluation
from sleeper_manager.backtesting.experiments.feature_dataset_cache import (
    compute_cache_key,
    dataset_cache_path,
    read_dataset_cache,
    write_dataset_cache,
)
from sleeper_manager.backtesting.experiments.projection_evaluation import (
    ProjectionSelectionDecision,
)
from sleeper_manager.backtesting.progress import (
    ProgressEvent,
    ProgressStage,
    ProgressState,
)
from sleeper_manager.backtesting.validation.models import ChronologicalFold
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.integrations.nba.historical_feature_models import HistoricalFeatureDataset


def _install_execution_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    generated_at = datetime(2026, 8, 31, 12, tzinfo=UTC)
    dataset = HistoricalFeatureDataset("dataset-v1", "schema-v1", generated_at, (), ())
    inputs = SimpleNamespace(games=(), provider_players=(), player_box_scores=(object(), object()))

    def load_inputs(*_: object, progress: Any, **__: object) -> object:
        progress.advance(1, 1, detail="2025 schedule")
        return inputs

    def load_injuries(*_: object, progress: Any, **__: object) -> object:
        progress.advance(1, 1)
        return SimpleNamespace()

    def build_dataset(*_: object, progress: Any, **__: object) -> HistoricalFeatureDataset:
        progress.advance(1, 2)
        progress.advance(2, 2)
        return dataset

    folds = (
        ChronologicalFold(
            "development-fold",
            2024,
            "late",
            datetime(2025, 3, 1, tzinfo=UTC),
            datetime(2025, 5, 1, tzinfo=UTC),
        ),
        ChronologicalFold(
            "locked-fold",
            2025,
            "early",
            datetime(2025, 10, 1, tzinfo=UTC),
            datetime(2026, 1, 1, tzinfo=UTC),
            holdout=True,
        ),
    )

    def run_folds(
        *_: object,
        folds: object,
        progress: Any,
        progress_stage: Any,
        **__: object,
    ) -> tuple:
        fold_records = tuple(folds)  # type: ignore[arg-type]
        for position, fold in enumerate(fold_records, start=1):
            with progress.stage(
                progress_stage,
                fold_name=fold.name,
                fold_position=position,
                fold_count=len(fold_records),
                unit="targets",
            ) as counter:
                counter.advance(1, 1)
        return ()

    selection = ProjectionSelectionDecision(
        selected_model="direct_baseline",
        reference_model="direct_baseline",
        candidate_model="opportunity_full",
        cohort="top_108",
        mae_delta=0,
        common_sample_count=1,
        rule_passed=True,
        coverage_gate_passed=True,
        interval_gate_passed=True,
        component_gate_passed=True,
        invariants_passed=True,
        provisional=True,
        evidence="fixture",
    )

    monkeypatch.setattr(evaluation, "_git_source_revision", lambda: "revision")
    monkeypatch.setattr(
        evaluation,
        "scoring_policy_from_league_fixture",
        lambda _: ScoringPolicy(points=1),
    )
    monkeypatch.setattr(evaluation, "load_historical_experiment_inputs", load_inputs)
    monkeypatch.setattr(evaluation, "acquire_injury_archive", load_injuries)
    monkeypatch.setattr(evaluation, "_historical_player_ids_by_date_team", lambda _: {})
    monkeypatch.setattr(evaluation, "_build_dataset", build_dataset)
    monkeypatch.setattr(evaluation, "raw_suite", lambda: ())
    monkeypatch.setattr(evaluation, "secondary_calibrated_suite", lambda: ())
    monkeypatch.setattr(evaluation, "frozen_manifest", lambda **_: {"source_revision": "revision"})
    monkeypatch.setattr(evaluation, "assert_manifest_frozen", lambda *_: None)
    monkeypatch.setattr(evaluation, "regular_season_folds", lambda: folds)
    monkeypatch.setattr(evaluation, "run_validation_folds", run_folds)
    checkpoint = SimpleNamespace(calibrated_continuations=())
    monkeypatch.setattr(evaluation, "build_development_checkpoint", lambda **_: checkpoint)
    monkeypatch.setattr(evaluation, "checkpoint_fold_summaries", lambda _: ())
    monkeypatch.setattr(
        evaluation,
        "development_checkpoint_path",
        lambda workspace, _: workspace / "checkpoints" / "checkpoint.json",
    )
    monkeypatch.setattr(
        evaluation,
        "load_development_checkpoint",
        lambda *_args, **_kwargs: checkpoint,
    )
    monkeypatch.setattr(
        evaluation,
        "calibrated_projectors",
        lambda _: (("calibrated-a", object()), ("calibrated-b", object())),
    )

    def restore(*_: object, on_restored: Any, **__: object) -> None:
        on_restored(1, 2, "calibrated-a")
        on_restored(2, 2, "calibrated-b")

    monkeypatch.setattr(
        evaluation,
        "restore_development_continuations",
        restore,
    )
    monkeypatch.setattr(evaluation, "development_report", lambda **_: {"kind": "development"})
    monkeypatch.setattr(
        evaluation,
        "report",
        lambda **_: {"modeled": {"selection": selection}},
    )
    monkeypatch.setattr(evaluation, "markdown_report", lambda _: "# fixture\n")

    def write_json(path: Path, payload: object) -> None:
        path.write_text(json.dumps(payload, default=str))

    monkeypatch.setattr(evaluation, "_write_json", write_json)
    monkeypatch.setattr(evaluation, "freeze_manifest", write_json)

    def write_checkpoint(path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, payload)

    monkeypatch.setattr(evaluation, "write_development_checkpoint", write_checkpoint)


def _started_stages(events: list[ProgressEvent]) -> list[ProgressStage]:
    return [item.stage for item in events if item.state is ProgressState.STARTED]


def test_development_mode_emits_ordered_stages_and_artifact_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_execution_fakes(monkeypatch)
    events: list[ProgressEvent] = []

    output = evaluation.run_projection_evaluation(
        tmp_path,
        league_fixture=tmp_path / "league.json",
        mode="development",
        now=datetime(2026, 8, 31, 12, tzinfo=UTC),
        progress=events.append,
    )

    assert _started_stages(events) == [
        ProgressStage.RESOLVE_SOURCE_REVISION,
        ProgressStage.LOAD_RAW_INPUTS,
        ProgressStage.LOAD_INJURY_ARCHIVE,
        ProgressStage.BUILD_HISTORICAL_FEATURES,
        ProgressStage.RUN_DEVELOPMENT_FOLD,
        ProgressStage.BUILD_REPORTS,
        ProgressStage.WRITE_ARTIFACTS,
    ]
    writes = [item for item in events if item.stage is ProgressStage.WRITE_ARTIFACTS]
    assert [(item.state, item.completed, item.total) for item in writes] == [
        (ProgressState.STARTED, 0, 3),
        (ProgressState.ADVANCED, 1, 3),
        (ProgressState.ADVANCED, 2, 3),
        (ProgressState.ADVANCED, 3, 3),
        (ProgressState.COMPLETED, 3, 3),
    ]
    assert output.report_json_path is None


def test_locked_mode_labels_development_and_locked_folds_separately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_execution_fakes(monkeypatch)
    events: list[ProgressEvent] = []

    output = evaluation.run_projection_evaluation(
        tmp_path,
        league_fixture=tmp_path / "league.json",
        mode="locked_retrospective",
        now=datetime(2026, 8, 31, 12, tzinfo=UTC),
        progress=events.append,
    )

    assert ProgressStage.RUN_DEVELOPMENT_FOLD not in _started_stages(events)
    assert ProgressStage.VALIDATE_DEVELOPMENT_CHECKPOINT in _started_stages(events)
    assert ProgressStage.RESTORE_CALIBRATED_CONTINUATION in _started_stages(events)
    assert ProgressStage.RUN_LOCKED_FOLD in _started_stages(events)
    locked_start = next(
        item
        for item in events
        if item.stage is ProgressStage.RUN_LOCKED_FOLD and item.state is ProgressState.STARTED
    )
    assert locked_start.fold_name == "locked-fold"
    restored = [
        item
        for item in events
        if item.stage is ProgressStage.RESTORE_CALIBRATED_CONTINUATION
        and item.state is ProgressState.ADVANCED
    ]
    assert [(item.completed, item.total, item.detail) for item in restored] == [
        (1, 2, "calibrated-a"),
        (2, 2, "calibrated-b"),
    ]
    writes = [
        item
        for item in events
        if item.stage is ProgressStage.WRITE_ARTIFACTS and item.state is ProgressState.ADVANCED
    ]
    assert [(item.completed, item.total, item.detail) for item in writes] == [
        (1, 2, "JSON report"),
        (2, 2, "Markdown report"),
    ]
    assert output.selected_model == "direct_baseline"
    assert not output.development_report_path.exists()


def test_development_failure_never_replaces_a_prior_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fold failure occurs before checkpoint publication and preserves prior durable evidence."""
    _install_execution_fakes(monkeypatch)
    checkpoint_path = tmp_path / "checkpoints" / "checkpoint.json"
    checkpoint_path.parent.mkdir(parents=True)
    checkpoint_path.write_text("prior-checkpoint")

    def fail_folds(*_: object, **__: object) -> tuple:
        raise RuntimeError("development failed")

    monkeypatch.setattr(evaluation, "run_validation_folds", fail_folds)

    with pytest.raises(RuntimeError, match="development failed"):
        evaluation.run_projection_evaluation(
            tmp_path,
            league_fixture=tmp_path / "league.json",
            mode="development",
            now=datetime(2026, 8, 31, 12, tzinfo=UTC),
        )

    assert checkpoint_path.read_text() == "prior-checkpoint"


def test_missing_checkpoint_fails_before_any_locked_fold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Locked mode has no development fallback when checkpoint validation fails."""
    _install_execution_fakes(monkeypatch)
    fold_calls = 0

    def count_folds(*_: object, **__: object) -> tuple:
        nonlocal fold_calls
        fold_calls += 1
        return ()

    def reject_checkpoint(*_: object, **__: object) -> object:
        raise DevelopmentCheckpointError("rerun development evaluation")

    monkeypatch.setattr(evaluation, "run_validation_folds", count_folds)
    monkeypatch.setattr(evaluation, "load_development_checkpoint", reject_checkpoint)

    with pytest.raises(DevelopmentCheckpointError, match="rerun development"):
        evaluation.run_projection_evaluation(
            tmp_path,
            league_fixture=tmp_path / "league.json",
            mode="locked_retrospective",
            now=datetime(2026, 8, 31, 12, tzinfo=UTC),
        )

    assert fold_calls == 0


def _seed_dataset_cache(tmp_path: Path) -> None:
    """Persist the fake execution dataset under the key the runner will compute."""
    (tmp_path / "raw").mkdir(exist_ok=True)
    (tmp_path / "injuries").mkdir(exist_ok=True)
    key = compute_cache_key(
        tmp_path / "raw",
        tmp_path / "injuries",
        scoring_policy=ScoringPolicy(points=1),
        source_revision="revision",
    )
    dataset = HistoricalFeatureDataset(
        "dataset-v1", "schema-v1", datetime(2026, 8, 31, 12, tzinfo=UTC), (), ()
    )
    write_dataset_cache(dataset_cache_path(tmp_path), key, dataset)


def _forbid_setup_rebuild(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace setup loaders with raisers so any rebuild attempt fails loudly."""

    def forbidden(*_: object, **__: object) -> object:
        raise AssertionError("cached setup must not reload inputs")

    monkeypatch.setattr(evaluation, "load_historical_experiment_inputs", forbidden)
    monkeypatch.setattr(evaluation, "acquire_injury_archive", forbidden)
    monkeypatch.setattr(evaluation, "_build_dataset", forbidden)


def test_cached_setup_skips_rebuild_and_reports_cache_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reuse a matching dataset cache instead of replaying the slow setup stages."""
    _install_execution_fakes(monkeypatch)
    _seed_dataset_cache(tmp_path)
    _forbid_setup_rebuild(monkeypatch)
    events: list[ProgressEvent] = []

    output = evaluation.run_projection_evaluation(
        tmp_path,
        league_fixture=tmp_path / "league.json",
        mode="development",
        now=datetime(2026, 8, 31, 12, tzinfo=UTC),
        progress=events.append,
    )

    assert output.dataset_version == "dataset-v1"
    assert _started_stages(events) == [
        ProgressStage.RESOLVE_SOURCE_REVISION,
        ProgressStage.LOAD_CACHED_DATASET,
        ProgressStage.RUN_DEVELOPMENT_FOLD,
        ProgressStage.BUILD_REPORTS,
        ProgressStage.WRITE_ARTIFACTS,
    ]


def test_uncached_setup_rebuilds_and_publishes_dataset_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Publish the cache after a miss so the next identical run restores it."""
    _install_execution_fakes(monkeypatch)
    (tmp_path / "raw").mkdir()
    (tmp_path / "injuries").mkdir()
    events: list[ProgressEvent] = []

    output = evaluation.run_projection_evaluation(
        tmp_path,
        league_fixture=tmp_path / "league.json",
        mode="development",
        now=datetime(2026, 8, 31, 12, tzinfo=UTC),
        progress=events.append,
    )

    assert output.dataset_version == "dataset-v1"
    assert _started_stages(events) == [
        ProgressStage.RESOLVE_SOURCE_REVISION,
        ProgressStage.LOAD_RAW_INPUTS,
        ProgressStage.LOAD_INJURY_ARCHIVE,
        ProgressStage.BUILD_HISTORICAL_FEATURES,
        ProgressStage.RUN_DEVELOPMENT_FOLD,
        ProgressStage.BUILD_REPORTS,
        ProgressStage.WRITE_ARTIFACTS,
    ]
    key = compute_cache_key(
        tmp_path / "raw",
        tmp_path / "injuries",
        scoring_policy=ScoringPolicy(points=1),
        source_revision="revision",
    )
    assert read_dataset_cache(dataset_cache_path(tmp_path), key).dataset_version == "dataset-v1"


def test_unwritable_dataset_cache_leaves_evaluation_on_uncached_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never fail an evaluation because the best-effort cache cannot persist."""
    _install_execution_fakes(monkeypatch)
    (tmp_path / "raw").mkdir()
    (tmp_path / "injuries").mkdir()
    dataset_cache_path(tmp_path).mkdir()

    output = evaluation.run_projection_evaluation(
        tmp_path,
        league_fixture=tmp_path / "league.json",
        mode="development",
        now=datetime(2026, 8, 31, 12, tzinfo=UTC),
    )

    assert output.dataset_version == "dataset-v1"
