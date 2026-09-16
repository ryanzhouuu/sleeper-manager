"""Shared historical-row factories for opportunity-model tests."""

from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sleeper_manager.domain.nba import AvailabilityStatus
from sleeper_manager.domain.scoring import BoxScoreLine, ScoringPolicy
from sleeper_manager.integrations.nba.historical_feature_dataset import (
    HistoricalFeatureDataset,
    HistoricalFeatureRow,
)
from sleeper_manager.integrations.nba.historical_feature_models import (
    AvailabilityObservation,
    OpponentStatsFallback,
    PaceStatsFallback,
)

NOW = datetime(2026, 1, 10, 18, tzinfo=UTC)


class _GrowingPrefix(Sequence[HistoricalFeatureRow]):
    """Minimal stand-in for the backtest execution module's point-in-time row prefix.

    Wraps a fixed chronological tuple and exposes only the first ``prior_count`` rows
    plus one appended ``target`` row, mirroring ``_PointInTimeRows`` in
    ``backtesting/backtest_execution.py`` closely enough to exercise the same duck-typed
    ``prior_count`` contract the opportunity model's history index relies on, without
    depending on that private class directly.
    """

    def __init__(
        self,
        rows: tuple[HistoricalFeatureRow, ...],
        prior_count: int,
        target: HistoricalFeatureRow,
    ) -> None:
        self._rows = rows
        self._prior_count = prior_count
        self._target = target

    def __len__(self) -> int:
        return self._prior_count + 1

    @property
    def prior_count(self) -> int:
        return self._prior_count

    def __getitem__(self, index: int) -> HistoricalFeatureRow:  # type: ignore[override]
        normalized = index if index >= 0 else len(self) + index
        if normalized == self._prior_count:
            return self._target
        return self._rows[normalized]


def _sanitize(target: HistoricalFeatureRow) -> HistoricalFeatureRow:
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


def _growing_dataset(
    rows: tuple[HistoricalFeatureRow, ...],
    game_starts: tuple[datetime, ...],
    target: HistoricalFeatureRow,
) -> HistoricalFeatureDataset:
    prior_count = bisect_left(game_starts, target.game_start)
    return HistoricalFeatureDataset(
        "fixture", "5", NOW, (), _GrowingPrefix(rows, prior_count, _sanitize(target))
    )


def row(
    game_id: str,
    game_start: datetime,
    *,
    did_play: bool,
    minutes: float | None,
    points: int,
    pace_factor: float | None = None,
    player_id: str = "player-1",
    opponent_defensive_rating: float | None = 100,
    league_defensive_rating: float | None = None,
    opponent_stats_fallback: OpponentStatsFallback = OpponentStatsFallback.OBSERVED,
) -> HistoricalFeatureRow:
    return HistoricalFeatureRow(
        dataset_version="fixture",
        available_as_of=game_start - timedelta(minutes=30),
        player_id=player_id,
        sleeper_id=player_id,
        game_id=game_id,
        game_start=game_start,
        team_id="team-1",
        opponent_team_id="team-2",
        opponent_abbreviation="t2",
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
        target_minutes=minutes,
        target_started=did_play,
        target_did_play=did_play,
        target_box_score=BoxScoreLine(points=points),
        target_line_points=points,
        target_line_rebounds=0,
        target_line_assists=0,
        target_line_steals=0,
        target_line_blocks=0,
        target_line_turnovers=0,
        source_lineage=(),
        opponent_defensive_rating=opponent_defensive_rating,
        league_defensive_rating=league_defensive_rating,
        opponent_sample_size=5,
        opponent_stats_fallback=opponent_stats_fallback,
        own_team_pace=100,
        own_team_pace_fallback=PaceStatsFallback.OBSERVED,
        expected_matchup_pace=100,
        baseline_exposure_pace=100,
        pace_factor=pace_factor,
    )


def _ablation_fixture() -> tuple[HistoricalFeatureDataset, ScoringPolicy]:
    prior = row("game-1", NOW - timedelta(days=3), did_play=True, minutes=30, points=20)
    target = row(
        "target",
        NOW,
        did_play=False,
        minutes=None,
        points=0,
        pace_factor=1.05,
        opponent_defensive_rating=95,
        league_defensive_rating=100,
    )
    return HistoricalFeatureDataset("fixture", "3", NOW, (), (prior, target)), ScoringPolicy(
        points=1
    )
