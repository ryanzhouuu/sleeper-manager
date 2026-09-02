"""Verify exact, fail-closed continuation for chronological residual calibration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sleeper_manager.backtesting.calibration import CalibratedProjectionModel
from sleeper_manager.backtesting.models import BacktestError
from sleeper_manager.domain.nba import AvailabilityStatus, SourceMetadata
from sleeper_manager.domain.projection import ProjectionDistribution, ProjectionSnapshot
from sleeper_manager.domain.scoring import BoxScoreLine, ScoringPolicy
from sleeper_manager.integrations.nba.historical_feature_models import (
    AvailabilityObservation,
    HistoricalFeatureDataset,
    HistoricalFeatureRow,
)

POLICY = ScoringPolicy(points=1)
SOURCE = SourceMetadata("fixture", "fixture", datetime(2026, 9, 1, tzinfo=UTC))


class FixedProjectionModel:
    """Return a deterministic point projection one point below the realized target."""

    model_version = "fixed-projection-v1"

    def project(
        self,
        dataset: HistoricalFeatureDataset,
        *,
        player_id: str,
        game_id: str,
        scoring_policy: ScoringPolicy,
        exceed_score: float | None = None,
    ) -> ProjectionSnapshot:
        """Build a stable distribution for the requested fixture target."""
        del scoring_policy
        target = next(
            row for row in dataset.rows if row.player_id == player_id and row.game_id == game_id
        )
        expected = float(target.target_line_points - 1)
        distribution = ProjectionDistribution.from_weighted_observations(((expected, 1.0),))
        if exceed_score is not None:
            distribution = distribution.for_exceedance_score(exceed_score)
        return ProjectionSnapshot(
            player_id=player_id,
            game_id=game_id,
            available_as_of=target.available_as_of,
            model_version=self.model_version,
            input_version=f"fixed-input-{game_id}",
            scoring_policy_version=POLICY.version,
            distribution=distribution,
            reasons=(),
        )


def _row(position: int, start: datetime, *, player_id: str | None = None) -> HistoricalFeatureRow:
    """Build one finalized feature row for continuation tests."""
    resolved_player = player_id or f"player-{position}"
    return HistoricalFeatureRow(
        dataset_version="calibration-continuation-fixture",
        available_as_of=start - timedelta(minutes=30),
        player_id=resolved_player,
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
        prior_minutes_mean=30.0,
        prior_minutes_last=30.0,
        prior_start_rate=1.0,
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


def _dataset() -> HistoricalFeatureDataset:
    """Return rows with a two-target same-tipoff boundary before locked continuation."""
    base = datetime(2025, 11, 1, tzinfo=UTC)
    starts = (
        base,
        base + timedelta(days=1),
        base + timedelta(days=2),
        base + timedelta(days=3),
        base + timedelta(days=4),
        base + timedelta(days=4),
        base + timedelta(days=5),
        base + timedelta(days=6),
    )
    rows = tuple(_row(position, start) for position, start in enumerate(starts))
    return HistoricalFeatureDataset(
        dataset_version="calibration-continuation-fixture",
        feature_schema_version="fixture-schema-v1",
        generated_at=datetime(2026, 9, 1, tzinfo=UTC),
        source_versions=(),
        rows=rows,
    )


def _project(
    model: CalibratedProjectionModel,
    dataset: HistoricalFeatureDataset,
    positions: range,
) -> tuple[ProjectionSnapshot, ...]:
    """Project the selected chronological fixture positions."""
    return tuple(
        model.project(
            dataset,
            player_id=dataset.rows[position].player_id,
            game_id=dataset.rows[position].game_id,
            scoring_policy=POLICY,
        )
        for position in positions
    )


def _model() -> CalibratedProjectionModel:
    """Return a small-capacity model that crosses refresh and eviction boundaries."""
    return CalibratedProjectionModel(
        FixedProjectionModel(),
        min_samples=2,
        max_samples=3,
        refresh_interval=2,
    )


def test_export_restore_matches_uninterrupted_projection_after_same_tipoff() -> None:
    """Restored projections preserve pending order, refreshes, and bounded residuals exactly."""
    dataset = _dataset()
    uninterrupted = _model()
    _project(uninterrupted, dataset, range(6))
    continuation = uninterrupted.export_continuation()

    assert continuation.total_residual_count == 4
    assert len(continuation.residuals) == 3
    assert tuple(item.game_id for item in continuation.pending) == ("game-4", "game-5")

    restored = _model()
    restored.restore_continuation(
        continuation,
        dataset_version=dataset.dataset_version,
        scoring_policy_version=POLICY.version,
    )

    assert _project(restored, dataset, range(6, 8)) == _project(uninterrupted, dataset, range(6, 8))
    assert restored.export_continuation() == uninterrupted.export_continuation()


def test_restore_rejects_identity_drift_and_used_models() -> None:
    """Continuation cannot cross scoring identities or overwrite accumulated model state."""
    dataset = _dataset()
    source = _model()
    _project(source, dataset, range(3))
    continuation = source.export_continuation()

    with pytest.raises(BacktestError, match="scoring policy"):
        _model().restore_continuation(
            continuation,
            dataset_version=dataset.dataset_version,
            scoring_policy_version="wrong-policy",
        )

    used = _model()
    _project(used, dataset, range(1))
    with pytest.raises(BacktestError, match="fresh"):
        used.restore_continuation(
            continuation,
            dataset_version=dataset.dataset_version,
            scoring_policy_version=POLICY.version,
        )
