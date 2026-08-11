from datetime import UTC, datetime, timedelta

import pytest

from sleeper_manager.domain.nba import AvailabilityStatus, SourceMetadata
from sleeper_manager.domain.scoring import BoxScoreLine, ScoringPolicy
from sleeper_manager.integrations.nba.historical_features import (
    AvailabilityObservation,
    HistoricalFeatureDataset,
    HistoricalFeatureRow,
)
from sleeper_manager.projections.direct_baseline import (
    DirectFantasyPointBaseline,
    ProjectionBaselineConfig,
    ProjectionBaselineError,
)

SOURCE = SourceMetadata("fixture", "fixture", datetime(2026, 8, 10, tzinfo=UTC))
POLICY = ScoringPolicy(
    points=1,
    three_pointers_made=1,
    technical_foul=-1,
    flagrant_foul=-2,
)


def row(
    game_id: str,
    player_id: str,
    start: datetime,
    points: int,
    minutes: float,
    *,
    started: bool = False,
    did_play: bool = True,
) -> HistoricalFeatureRow:
    line = BoxScoreLine(points=points, three_pointers_made=1)
    return HistoricalFeatureRow(
        dataset_version="features-v1",
        available_as_of=start - timedelta(minutes=30),
        player_id=player_id,
        sleeper_id=None,
        game_id=game_id,
        game_start=start,
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
        target_minutes=minutes,
        target_started=started,
        target_did_play=did_play,
        target_box_score=line,
        target_line_points=points,
        target_line_rebounds=0,
        target_line_assists=0,
        target_line_steals=0,
        target_line_blocks=0,
        target_line_turnovers=0,
        source_lineage=(SOURCE,),
    )


def dataset(rows: tuple[HistoricalFeatureRow, ...]) -> HistoricalFeatureDataset:
    return HistoricalFeatureDataset(
        dataset_version="features-v1",
        feature_schema_version="1",
        generated_at=datetime(2026, 8, 10, tzinfo=UTC),
        source_versions=(),
        rows=rows,
    )


def test_direct_baseline_projects_sleeper_points_with_versions_reasons_and_exceedance() -> None:
    target = row("target", "player-1", datetime(2025, 1, 10, tzinfo=UTC), 0, 0, did_play=False)
    history = (
        row("old", "player-1", datetime(2025, 1, 1, tzinfo=UTC), 20, 20, started=True),
        row("recent", "player-1", datetime(2025, 1, 9, tzinfo=UTC), 10, 10),
        row("other", "player-2", datetime(2025, 1, 9, tzinfo=UTC), 4, 10),
    )
    snapshot = DirectFantasyPointBaseline().project(
        dataset(history + (target,)),
        player_id="player-1",
        game_id="target",
        scoring_policy=POLICY,
        exceed_score=12,
    )

    assert snapshot.distribution.expected_value > 10
    assert snapshot.distribution.probability_exceeding_score is not None
    assert snapshot.distribution.probability_exceeding_score == (
        snapshot.distribution.probability_of_exceeding(12)
    )
    assert snapshot.model_version.startswith("projection-baseline-v1-")
    assert snapshot.input_version.startswith("projection-input-v1-")
    assert snapshot.scoring_policy_version == POLICY.version
    assert any(reason.code == "minutes_role" for reason in snapshot.reasons)
    assert any(reason.code == "season_shrinkage" for reason in snapshot.reasons)
    deferred = [reason for reason in snapshot.reasons if reason.code.startswith("deferred_")]
    assert len(deferred) == 5
    assert all(not reason.applied for reason in deferred)


def test_direct_baseline_excludes_future_rows_from_projection() -> None:
    target = row("target", "player-1", datetime(2025, 1, 10, tzinfo=UTC), 0, 0, did_play=False)
    prior = row("prior", "player-1", datetime(2025, 1, 9, tzinfo=UTC), 10, 10)
    future = row("future", "player-1", datetime(2025, 1, 11, tzinfo=UTC), 100, 40)

    baseline = DirectFantasyPointBaseline()
    without_future = baseline.project(
        dataset((prior, target)), player_id="player-1", game_id="target", scoring_policy=POLICY
    )
    with_future = baseline.project(
        dataset((prior, target, future)),
        player_id="player-1",
        game_id="target",
        scoring_policy=POLICY,
    )

    assert with_future.distribution.expected_value == without_future.distribution.expected_value


def test_direct_baseline_requires_prior_same_season_observations() -> None:
    target = row("target", "player-1", datetime(2025, 1, 10, tzinfo=UTC), 0, 0, did_play=False)
    with pytest.raises(ProjectionBaselineError, match="No prior same-season"):
        DirectFantasyPointBaseline().project(
            dataset((target,)),
            player_id="player-1",
            game_id="target",
            scoring_policy=POLICY,
        )


def test_baseline_config_rejects_invalid_or_unknown_adjustments() -> None:
    with pytest.raises(ProjectionBaselineError):
        ProjectionBaselineConfig(recency_half_life_days=0)
    with pytest.raises(ProjectionBaselineError):
        ProjectionBaselineConfig(disabled_adjustments=("weather",))
