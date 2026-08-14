from datetime import UTC, datetime, timedelta

import pytest

from sleeper_manager.domain.nba import AvailabilityStatus, SourceMetadata
from sleeper_manager.domain.projection import ProjectionDistribution, ProjectionSnapshot
from sleeper_manager.domain.scoring import BoxScoreLine, ScoringPolicy
from sleeper_manager.integrations.nba.historical_features import (
    AvailabilityObservation,
    HistoricalFeatureDataset,
    HistoricalFeatureRow,
)
from sleeper_manager.projections.residual_candidates import (
    CachingProjectionModel,
    ResidualCandidateConfig,
    ResidualCandidateError,
    ResidualFeature,
    ShrunkenResidualCandidate,
)

SOURCE = SourceMetadata("fixture", "fixture", datetime(2026, 8, 13, tzinfo=UTC))
POLICY = ScoringPolicy(points=1)


def row(
    game_id: str,
    start: datetime,
    points: int,
    *,
    opponent: str = "WAS",
    days_rest: int | None = 1,
) -> HistoricalFeatureRow:
    return HistoricalFeatureRow(
        dataset_version="features-v2",
        available_as_of=start - timedelta(minutes=30),
        player_id=f"player-{game_id}",
        sleeper_id=None,
        game_id=game_id,
        game_start=start,
        team_id="CHI",
        opponent_team_id=opponent,
        opponent_abbreviation=opponent.casefold(),
        is_home=True,
        days_rest=days_rest,
        is_back_to_back=days_rest == 0,
        availability_status=AvailabilityStatus.UNKNOWN,
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
        source_lineage=(SOURCE,),
    )


def dataset(rows: tuple[HistoricalFeatureRow, ...]) -> HistoricalFeatureDataset:
    return HistoricalFeatureDataset(
        dataset_version="features-v2",
        feature_schema_version="2",
        generated_at=datetime(2026, 8, 13, tzinfo=UTC),
        source_versions=(),
        rows=rows,
    )


class FixedReference:
    def __init__(self) -> None:
        self.calls = 0

    def project(
        self,
        dataset: HistoricalFeatureDataset,
        *,
        player_id: str,
        game_id: str,
        scoring_policy: ScoringPolicy,
        exceed_score: float | None = None,
    ) -> ProjectionSnapshot:
        self.calls += 1
        target = next(
            row for row in dataset.rows if row.player_id == player_id and row.game_id == game_id
        )
        distribution = ProjectionDistribution.from_weighted_observations(((5, 1), (10, 1), (15, 1)))
        if exceed_score is not None:
            distribution = distribution.for_exceedance_score(exceed_score)
        return ProjectionSnapshot(
            player_id,
            game_id,
            target.available_as_of,
            "fixed-v1",
            f"fixed-input-{game_id}",
            scoring_policy.version,
            distribution,
            (),
        )


def test_residual_candidate_uses_matching_prior_group_and_excludes_future() -> None:
    records = (
        row("matching", datetime(2025, 1, 1, tzinfo=UTC), 20),
        row("other", datetime(2025, 1, 2, tzinfo=UTC), 0, opponent="BOS"),
        row("target", datetime(2025, 1, 3, tzinfo=UTC), 0),
        row("future", datetime(2025, 1, 4, tzinfo=UTC), 100),
    )
    candidate = ShrunkenResidualCandidate(
        ResidualCandidateConfig(
            (ResidualFeature.OPPONENT_IDENTITY,),
            shrinkage_games=0,
        ),
        reference=FixedReference(),
    )

    snapshot = candidate.project(
        dataset(records),
        player_id="player-target",
        game_id="target",
        scoring_policy=POLICY,
        exceed_score=15,
    )

    assert snapshot.distribution.expected_value == 20
    assert snapshot.distribution.probability_exceeding_score == pytest.approx(2 / 3)
    assert snapshot.input_version.startswith("residual-input-v1-")
    assert snapshot.model_version.startswith("residual-candidate-v1-")
    assert snapshot.reasons[-1].adjustment == 10
    assert snapshot.reasons[-1].applied


def test_residual_candidate_sparse_group_falls_back_to_zero() -> None:
    records = (
        row("prior", datetime(2025, 1, 1, tzinfo=UTC), 20),
        row("target", datetime(2025, 1, 3, tzinfo=UTC), 0, opponent="BOS"),
    )
    candidate = ShrunkenResidualCandidate(
        ResidualCandidateConfig((ResidualFeature.OPPONENT_IDENTITY,)),
        reference=FixedReference(),
    )

    snapshot = candidate.project(
        dataset(records),
        player_id="player-target",
        game_id="target",
        scoring_policy=POLICY,
    )

    assert snapshot.distribution.expected_value == 10
    assert snapshot.reasons[-1].adjustment == 0
    assert not snapshot.reasons[-1].applied


def test_residual_candidate_rejects_invalid_config() -> None:
    with pytest.raises(ResidualCandidateError):
        ResidualCandidateConfig(())
    with pytest.raises(ResidualCandidateError):
        ResidualCandidateConfig((ResidualFeature.REST,), shrinkage_games=-1)
    with pytest.raises(ResidualCandidateError):
        ResidualCandidateConfig((ResidualFeature.REST,), lookback_days=0)


def test_projection_cache_reuses_point_in_time_reference_prediction() -> None:
    records = (
        row("prior", datetime(2025, 1, 1, tzinfo=UTC), 20),
        row("target", datetime(2025, 1, 3, tzinfo=UTC), 0),
    )
    reference = FixedReference()
    cached = CachingProjectionModel(reference)

    first = cached.project(
        dataset(records),
        player_id="player-target",
        game_id="target",
        scoring_policy=POLICY,
    )
    second = cached.project(
        dataset(records),
        player_id="player-target",
        game_id="target",
        scoring_policy=POLICY,
    )

    assert first is second
    assert reference.calls == 1
