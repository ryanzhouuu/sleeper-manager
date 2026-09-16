"""Shared incremental-history rows and projectors for direct-baseline tests."""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sleeper_manager.domain.nba import AvailabilityStatus, SourceMetadata
from sleeper_manager.domain.projection import ProjectionSnapshot
from sleeper_manager.domain.scoring import BoxScoreLine, ScoringPolicy
from sleeper_manager.integrations.nba.historical_feature_models import (
    AvailabilityObservation,
    HistoricalFeatureDataset,
    HistoricalFeatureRow,
)
from sleeper_manager.projections.direct_baseline import (
    DirectBaselineObservation,
    DirectFantasyPointBaseline,
    PregameProjectionRequest,
)

BASE = datetime(2025, 1, 1, 18, tzinfo=UTC)
POLICY = ScoringPolicy(points=1)


def row(
    game_id: str,
    player_id: str,
    start: datetime,
    points: int,
    *,
    finalized_at: datetime | None = None,
    omit_finalization: bool = False,
    source_hash: str = "source-v1",
) -> HistoricalFeatureRow:
    """Build one finalized historical feature row with deterministic provenance."""
    source = SourceMetadata(
        "fixture",
        game_id,
        start + timedelta(hours=3),
        content_hash=source_hash,
    )
    outcome_finalized_at = None if omit_finalization else finalized_at or start + timedelta(hours=2)
    return HistoricalFeatureRow(
        dataset_version="incremental-fixture",
        available_as_of=start - timedelta(minutes=30),
        player_id=player_id,
        sleeper_id=player_id,
        game_id=game_id,
        game_start=start,
        outcome_finalized_at=outcome_finalized_at,
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
        prior_games=0,
        prior_minutes_mean=None,
        prior_minutes_last=None,
        prior_start_rate=None,
        target_minutes=30,
        target_started=True,
        target_did_play=True,
        target_box_score=BoxScoreLine(points=points),
        target_line_points=points,
        target_line_rebounds=0,
        target_line_assists=0,
        target_line_steals=0,
        target_line_blocks=0,
        target_line_turnovers=0,
        source_lineage=(source,),
    )


def feature_dataset(
    rows: Sequence[HistoricalFeatureRow],
    *,
    version: str = "incremental-fixture",
) -> HistoricalFeatureDataset:
    """Wrap fixture rows in the immutable historical dataset contract."""
    return HistoricalFeatureDataset(version, "5", BASE, (), rows)


def sanitized(target: HistoricalFeatureRow) -> HistoricalFeatureRow:
    """Remove realized target fields exactly as the backtest runner does."""
    return replace(
        target,
        target_minutes=None,
        target_started=False,
        target_did_play=False,
        target_box_score=BoxScoreLine(),
        target_line_points=0,
        target_line_rebounds=0,
        target_line_assists=0,
        target_line_steals=0,
        target_line_blocks=0,
        target_line_turnovers=0,
    )


class GrowingRows(Sequence[HistoricalFeatureRow]):
    """Expose a chronological prior prefix followed by one sanitized target."""

    def __init__(
        self,
        rows: tuple[HistoricalFeatureRow, ...],
        prior_count: int,
        target: HistoricalFeatureRow,
    ) -> None:
        self._rows = rows
        self.prior_count = prior_count
        self._target = sanitized(target)

    def __len__(self) -> int:
        """Include the uncommitted target after the legitimate prior prefix."""
        return self.prior_count + 1

    def __getitem__(self, index: int) -> HistoricalFeatureRow:
        """Return a prior row or the appended target without copying the prefix."""
        normalized = index if index >= 0 else len(self) + index
        if normalized < 0 or normalized >= len(self):
            raise IndexError(index)
        if normalized == self.prior_count:
            return self._target
        return self._rows[normalized]


def growing_dataset(
    rows: tuple[HistoricalFeatureRow, ...],
    target: HistoricalFeatureRow,
    *,
    version: str = "incremental-fixture",
) -> HistoricalFeatureDataset:
    """Build the backtest runner's growing point-in-time dataset shape."""
    starts = tuple(candidate.game_start for candidate in rows)
    prior_count = bisect_left(starts, target.game_start)
    return feature_dataset(GrowingRows(rows, prior_count, target), version=version)


def explicit_snapshot(
    rows: tuple[HistoricalFeatureRow, ...],
    target: HistoricalFeatureRow,
    *,
    scoring_policy: ScoringPolicy = POLICY,
) -> ProjectionSnapshot:
    """Project one target through the production-neutral explicit-history path."""
    history = tuple(
        DirectBaselineObservation.from_historical_row(candidate)
        for candidate in rows
        if candidate.game_start < target.game_start
    )
    request = PregameProjectionRequest(
        dataset_version="incremental-fixture",
        feature_schema_version="5",
        player_id=target.player_id,
        game_id=target.game_id,
        game_start=target.game_start,
        available_as_of=target.available_as_of,
        history=history,
        history_player_id=target.player_id,
    )
    return DirectFantasyPointBaseline().project_pregame(
        request,
        scoring_policy=scoring_policy,
    )


class ExplicitHistoryProjector:
    """Test adapter that reconstructs compact history for every backtest target."""

    def __init__(self) -> None:
        self.baseline = DirectFantasyPointBaseline()

    @property
    def model_version(self) -> str:
        """Match the wrapped-name behavior used by calibrated direct projections."""
        return "DirectFantasyPointBaseline"

    def project(
        self,
        dataset: HistoricalFeatureDataset,
        *,
        player_id: str,
        game_id: str,
        scoring_policy: ScoringPolicy,
        exceed_score: float | None = None,
    ) -> ProjectionSnapshot:
        """Delegate through an explicit request instead of the incremental row contract."""
        target = dataset.rows[-1]
        history = tuple(
            DirectBaselineObservation.from_historical_row(candidate)
            for candidate in dataset.rows
            if candidate.game_start < target.game_start
        )
        request = PregameProjectionRequest(
            dataset_version=dataset.dataset_version,
            feature_schema_version=dataset.feature_schema_version,
            player_id=player_id,
            game_id=game_id,
            game_start=target.game_start,
            available_as_of=target.available_as_of,
            history=history,
            history_player_id=player_id,
            source_versions=dataset.source_versions,
        )
        return self.baseline.project_pregame(
            request,
            scoring_policy=scoring_policy,
            exceed_score=exceed_score,
        )
