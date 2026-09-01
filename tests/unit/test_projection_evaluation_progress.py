"""Verify projection-evaluation stage orchestration without running the real dataset."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sleeper_manager.backtesting.experiments import projection_evaluation as evaluation
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
        (ProgressState.STARTED, 0, 2),
        (ProgressState.ADVANCED, 1, 2),
        (ProgressState.ADVANCED, 2, 2),
        (ProgressState.COMPLETED, 2, 2),
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

    assert ProgressStage.RUN_DEVELOPMENT_FOLD in _started_stages(events)
    assert ProgressStage.RUN_LOCKED_FOLD in _started_stages(events)
    locked_start = next(
        item
        for item in events
        if item.stage is ProgressStage.RUN_LOCKED_FOLD and item.state is ProgressState.STARTED
    )
    assert locked_start.fold_name == "locked-fold"
    writes = [
        item
        for item in events
        if item.stage is ProgressStage.WRITE_ARTIFACTS and item.state is ProgressState.ADVANCED
    ]
    assert [(item.completed, item.total, item.detail) for item in writes] == [
        (1, 3, "development report"),
        (2, 3, "JSON report"),
        (3, 3, "Markdown report"),
    ]
    assert output.selected_model == "direct_baseline"
