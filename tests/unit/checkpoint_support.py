"""Shared deterministic fixtures for development-checkpoint unit tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sleeper_manager.backtesting.artifacts import canonicalize
from sleeper_manager.backtesting.calibration import CalibratedProjectionModel
from sleeper_manager.backtesting.controls import NaiveProjectionBaseline
from sleeper_manager.backtesting.experiments.projection_evaluation_report import fold_summary
from sleeper_manager.backtesting.models import BacktestConfig, BacktestModel
from sleeper_manager.backtesting.validation.folds import run_validation_folds
from sleeper_manager.backtesting.validation.models import ChronologicalFold
from sleeper_manager.domain.nba import AvailabilityStatus, SourceMetadata
from sleeper_manager.domain.scoring import BoxScoreLine, ScoringPolicy
from sleeper_manager.integrations.nba.historical_feature_models import (
    AvailabilityObservation,
    HistoricalFeatureDataset,
    HistoricalFeatureRow,
)
from sleeper_manager.projections.residual_candidates import CachingProjectionModel

POLICY = ScoringPolicy(points=1)
SOURCE = SourceMetadata("fixture", "fixture", datetime(2026, 9, 1, tzinfo=UTC))


def row(position: int, start: datetime) -> HistoricalFeatureRow:
    """Build one same-player game with chronological historical context."""
    return HistoricalFeatureRow(
        dataset_version="checkpoint-fixture-v1",
        available_as_of=start - timedelta(minutes=30),
        player_id="player-1",
        sleeper_id=None,
        game_id=f"game-{position}",
        game_start=start,
        outcome_finalized_at=start + timedelta(hours=2),
        team_id="CHI",
        opponent_team_id="WAS",
        opponent_abbreviation="was",
        is_home=True,
        days_rest=1,
        is_back_to_back=False,
        availability_status=AvailabilityStatus.AVAILABLE,
        availability_observation=AvailabilityObservation.MISSING_REPORT,
        availability_detail=None,
        availability_observed_at=None,
        prior_games=position,
        prior_minutes_mean=30.0 if position else None,
        prior_minutes_last=30.0 if position else None,
        prior_start_rate=1.0 if position else None,
        target_minutes=30,
        target_started=True,
        target_did_play=True,
        target_box_score=BoxScoreLine(points=20 + position),
        target_line_points=20 + position,
        target_line_rebounds=0,
        target_line_assists=0,
        target_line_steals=0,
        target_line_blocks=0,
        target_line_turnovers=0,
        source_lineage=(SOURCE,),
    )


def dataset() -> HistoricalFeatureDataset:
    """Return eight chronological games spanning development and locked folds."""
    base = datetime(2025, 11, 1, tzinfo=UTC)
    return HistoricalFeatureDataset(
        dataset_version="checkpoint-fixture-v1",
        feature_schema_version="checkpoint-schema-v1",
        generated_at=datetime(2026, 9, 1, tzinfo=UTC),
        source_versions=(),
        rows=tuple(row(position, base + timedelta(days=position)) for position in range(8)),
    )


def models() -> tuple[BacktestModel, ...]:
    """Return the supported cache-then-calibration wrapper shape."""
    return (
        BacktestModel(
            "calibrated",
            CachingProjectionModel(
                CalibratedProjectionModel(
                    NaiveProjectionBaseline("season_average"),
                    min_samples=2,
                    max_samples=3,
                    refresh_interval=2,
                ),
                max_entries=16,
            ),
        ),
    )


def folds() -> tuple[ChronologicalFold, ChronologicalFold]:
    """Return adjacent development and locked windows for the fixture dataset."""
    base = datetime(2025, 11, 1, tzinfo=UTC)
    return (
        ChronologicalFold(
            "fixture-development",
            2025,
            "development",
            base,
            base + timedelta(days=5, hours=23),
        ),
        ChronologicalFold(
            "fixture-locked",
            2025,
            "locked",
            base + timedelta(days=6),
            base + timedelta(days=8),
            holdout=True,
        ),
    )


def manifest(suite: tuple[BacktestModel, ...]) -> dict[str, object]:
    """Build the exact identities required by the checkpoint contract."""
    return {
        "manifest_version": "projection-evaluation-v1",
        "source_revision": "fixture-revision",
        "dataset_version": "checkpoint-fixture-v1",
        "feature_schema_version": "checkpoint-schema-v1",
        "scoring_policy_version": POLICY.version,
        "cohort_config_version": "cohort-fixture-v1",
        "backtest_config_version": BacktestConfig().version,
        "component_gate": {},
        "raw_suite": (),
        "secondary_suite": tuple((model.name, model.projector.model_version) for model in suite),
        "selection_rule": {},
    }


def development_evidence() -> tuple[
    HistoricalFeatureDataset,
    tuple[BacktestModel, ...],
    ChronologicalFold,
    dict[str, object],
]:
    """Run development and return its initialized suite and canonical summary."""
    historical_dataset = dataset()
    suite = models()
    development, _ = folds()
    result = run_validation_folds(
        historical_dataset,
        scoring_policy=POLICY,
        models=suite,
        folds=(development,),
        config=BacktestConfig(),
        reference_model="calibrated",
    )[0]
    summary = canonicalize(fold_summary(result))
    assert isinstance(summary, dict)
    return historical_dataset, suite, development, summary


__all__ = (
    "POLICY",
    "dataset",
    "development_evidence",
    "folds",
    "manifest",
    "models",
    "row",
)
