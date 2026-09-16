"""Shared projectors and historical rows for backtest engine tests."""

from datetime import UTC, datetime, timedelta

from sleeper_manager.backtesting import (
    BacktestError,
    NaiveProjectionBaseline,
)
from sleeper_manager.domain.nba import AvailabilityStatus, SourceMetadata
from sleeper_manager.domain.projection import ProjectionDistribution, ProjectionSnapshot
from sleeper_manager.domain.scoring import BoxScoreLine, ScoringPolicy
from sleeper_manager.integrations.nba.historical_feature_dataset import (
    HistoricalFeatureDataset,
    HistoricalFeatureRow,
)
from sleeper_manager.integrations.nba.historical_feature_models import AvailabilityObservation

SOURCE = SourceMetadata("fixture", "fixture", datetime(2026, 8, 10, tzinfo=UTC))
POLICY = ScoringPolicy(points=1)


class RecordingProgress:
    def __init__(self) -> None:
        self.events: list[tuple[int, int, str | None]] = []

    def advance(self, completed: int, total: int, *, detail: str | None = None) -> None:
        self.events.append((completed, total, detail))


def row(
    game_id: str,
    player_id: str,
    start: datetime,
    points: int,
) -> HistoricalFeatureRow:
    line = BoxScoreLine(points=points)
    return HistoricalFeatureRow(
        dataset_version="features-v1",
        available_as_of=start - timedelta(minutes=30),
        player_id=player_id,
        sleeper_id=None,
        game_id=game_id,
        game_start=start,
        outcome_finalized_at=start + timedelta(hours=2),
        team_id="CHI",
        opponent_team_id="WAS",
        opponent_abbreviation="was",
        is_home=True,
        days_rest=1,
        is_back_to_back=False,
        availability_status=AvailabilityStatus.UNKNOWN,
        availability_observation=AvailabilityObservation.MISSING_REPORT,
        availability_detail=None,
        availability_observed_at=None,
        prior_games=0,
        prior_minutes_mean=None,
        prior_minutes_last=None,
        prior_start_rate=None,
        target_minutes=30,
        target_started=False,
        target_did_play=True,
        target_box_score=line,
        target_line_points=points,
        target_line_rebounds=0,
        target_line_assists=0,
        target_line_steals=0,
        target_line_blocks=0,
        target_line_turnovers=0,
        source_lineage=(SOURCE,),
    )


def fixture_dataset() -> HistoricalFeatureDataset:
    rows = (
        row("p1-g1", "p1", datetime(2025, 1, 1, tzinfo=UTC), 10),
        row("p2-g1", "p2", datetime(2025, 1, 1, tzinfo=UTC), 5),
        row("p1-g2", "p1", datetime(2025, 1, 2, tzinfo=UTC), 20),
        row("p1-g3", "p1", datetime(2025, 1, 3, tzinfo=UTC), 30),
        row("p2-g2", "p2", datetime(2025, 1, 3, tzinfo=UTC), 15),
    )
    return HistoricalFeatureDataset(
        dataset_version="features-v1",
        feature_schema_version="1",
        generated_at=datetime(2026, 8, 10, tzinfo=UTC),
        source_versions=(),
        rows=rows,
    )


class FixedProjector:
    def project(
        self,
        dataset: HistoricalFeatureDataset,
        *,
        player_id: str,
        game_id: str,
        scoring_policy: ScoringPolicy,
        exceed_score: float | None = None,
    ) -> ProjectionSnapshot:
        target = next(
            row for row in dataset.rows if row.player_id == player_id and row.game_id == game_id
        )
        distribution = ProjectionDistribution.from_weighted_observations(((-5, 1), (0, 1), (5, 1)))
        return ProjectionSnapshot(
            player_id,
            game_id,
            target.available_as_of,
            "fixed-v1",
            f"fixed-input-{game_id}-{player_id}",
            scoring_policy.version,
            distribution,
            (),
        )


class InspectingProjector:
    def __init__(self) -> None:
        self.target_rows: list[HistoricalFeatureRow] = []

    def project(
        self,
        dataset: HistoricalFeatureDataset,
        *,
        player_id: str,
        game_id: str,
        scoring_policy: ScoringPolicy,
        exceed_score: float | None = None,
    ) -> ProjectionSnapshot:
        assert all(row.game_start <= datetime(2025, 1, 3, tzinfo=UTC) for row in dataset.rows)
        target = next(row for row in dataset.rows if row.game_id == game_id)
        self.target_rows.append(target)
        assert target.target_box_score == BoxScoreLine()
        assert target.target_minutes is None
        return NaiveProjectionBaseline("season_average").project(
            dataset,
            player_id=player_id,
            game_id=game_id,
            scoring_policy=scoring_policy,
            exceed_score=exceed_score,
        )


class FailingProjector:
    def project(
        self,
        dataset: HistoricalFeatureDataset,
        *,
        player_id: str,
        game_id: str,
        scoring_policy: ScoringPolicy,
        exceed_score: float | None = None,
    ) -> ProjectionSnapshot:
        if game_id == "p1-g3":
            raise BacktestError("candidate intentionally unavailable")
        return NaiveProjectionBaseline("last_game").project(
            dataset,
            player_id=player_id,
            game_id=game_id,
            scoring_policy=scoring_policy,
            exceed_score=exceed_score,
        )


class NoComponentProjector:
    def project(
        self,
        dataset: HistoricalFeatureDataset,
        *,
        player_id: str,
        game_id: str,
        scoring_policy: ScoringPolicy,
        exceed_score: float | None = None,
    ) -> ProjectionSnapshot:
        return NaiveProjectionBaseline("last_game").project(
            dataset,
            player_id=player_id,
            game_id=game_id,
            scoring_policy=scoring_policy,
            exceed_score=exceed_score,
        )
