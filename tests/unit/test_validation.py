from datetime import UTC, datetime, timedelta

from sleeper_manager.backtesting import (
    BacktestModel,
    ChronologicalFold,
    PromotionGateConfig,
    block_bootstrap_mae_delta,
    evaluate_development_candidate,
    evaluate_promotion,
    regular_season_folds,
    run_validation_folds,
    segment_comparisons,
)
from sleeper_manager.domain.nba import AvailabilityStatus, SourceMetadata
from sleeper_manager.domain.projection import ProjectionDistribution, ProjectionSnapshot
from sleeper_manager.domain.scoring import BoxScoreLine, ScoringPolicy
from sleeper_manager.integrations.nba.historical_features import (
    AvailabilityObservation,
    HistoricalFeatureDataset,
    HistoricalFeatureRow,
)

SOURCE = SourceMetadata("fixture", "fixture", datetime(2026, 8, 13, tzinfo=UTC))
POLICY = ScoringPolicy(points=1)


def row(game_id: str, player_id: str, start: datetime, points: int) -> HistoricalFeatureRow:
    return HistoricalFeatureRow(
        dataset_version="features-v2",
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
        prior_games=1,
        prior_minutes_mean=30,
        prior_minutes_last=30,
        prior_start_rate=1,
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
        opponent_pace_band="medium",
    )


def fixture_dataset() -> HistoricalFeatureDataset:
    rows = tuple(
        row(game_id, player_id, start, points)
        for game_id, start, points in (
            ("warmup", datetime(2025, 1, 1, tzinfo=UTC), 10),
            ("game-1", datetime(2025, 1, 2, tzinfo=UTC), 10),
            ("game-2", datetime(2025, 1, 3, tzinfo=UTC), 10),
        )
        for player_id in ("player-1", "player-2")
    )
    return HistoricalFeatureDataset(
        "features-v2",
        "2",
        datetime(2026, 8, 13, tzinfo=UTC),
        (),
        rows,
    )


class ConstantProjector:
    def __init__(self, expected: float) -> None:
        self.expected = expected

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
        distribution = ProjectionDistribution.from_weighted_observations(
            ((self.expected - 5, 1), (self.expected, 1), (self.expected + 5, 1))
        )
        return ProjectionSnapshot(
            player_id,
            game_id,
            target.available_as_of,
            f"constant-{self.expected}",
            f"constant-input-{game_id}-{player_id}",
            scoring_policy.version,
            distribution,
            (),
        )


def test_regular_season_folds_seal_three_holdout_windows() -> None:
    folds = regular_season_folds()

    assert len(folds) == 12
    assert sum(fold.holdout for fold in folds) == 3
    assert [fold.phase for fold in folds[-3:]] == ["early", "middle", "late"]
    assert all(fold.season_start == 2025 for fold in folds[-3:])


def test_validation_bootstrap_and_segments_use_paired_game_blocks() -> None:
    fold = ChronologicalFold(
        "fixture-fold",
        2024,
        "middle",
        datetime(2025, 1, 2, tzinfo=UTC),
        datetime(2025, 1, 4, tzinfo=UTC),
    )
    results = run_validation_folds(
        fixture_dataset(),
        scoring_policy=POLICY,
        models=(
            BacktestModel("reference", ConstantProjector(0)),
            BacktestModel("candidate", ConstantProjector(10)),
        ),
        folds=(fold,),
        reference_model="reference",
    )

    interval = block_bootstrap_mae_delta(
        results,
        reference_model="reference",
        candidate_model="candidate",
        samples=100,
        seed=7,
    )
    segments = segment_comparisons(
        results,
        dataset=fixture_dataset(),
        reference_model="reference",
        candidate_model="candidate",
        min_player_games=1,
        min_games=1,
    )

    assert interval.lower == -10
    assert interval.upper == -10
    assert interval.sample_count == 100
    starter = next(
        segment for segment in segments if segment.segment == "role" and segment.value == "starter"
    )
    assert starter.conclusive
    assert starter.mae_delta == -10

    development = evaluate_development_candidate(
        results,
        reference_model="reference",
        candidate_model="candidate",
        bootstrap_samples=100,
        bootstrap_seed=7,
    )
    assert development.selected
    assert development.improved_folds == 1

    decision = evaluate_promotion(
        development_results=results,
        holdout_results=results,
        dataset=fixture_dataset(),
        reference_model="reference",
        candidate_model="candidate",
        audit_passed=True,
        config=PromotionGateConfig(interval_coverage_tolerance=1),
    )
    assert decision.recommendation == "promote"
    assert all(gate.passed for gate in decision.gates)


def test_opponent_identity_cannot_be_promoted() -> None:
    fold = ChronologicalFold(
        "fixture-fold",
        2024,
        "middle",
        datetime(2025, 1, 2, tzinfo=UTC),
        datetime(2025, 1, 4, tzinfo=UTC),
    )
    results = run_validation_folds(
        fixture_dataset(),
        scoring_policy=POLICY,
        models=(
            BacktestModel("reference", ConstantProjector(0)),
            BacktestModel("opponent_identity", ConstantProjector(10)),
        ),
        folds=(fold,),
        reference_model="reference",
    )

    decision = evaluate_promotion(
        development_results=results,
        holdout_results=results,
        dataset=fixture_dataset(),
        reference_model="reference",
        candidate_model="opponent_identity",
        promotable=False,
        audit_passed=True,
        config=PromotionGateConfig(interval_coverage_tolerance=1),
    )

    assert decision.recommendation == "retain_experimental"
    assert not decision.gates[-1].passed
