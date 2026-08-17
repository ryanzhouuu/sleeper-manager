from datetime import UTC, datetime, timedelta

from sleeper_manager.backtesting import (
    BacktestMetrics,
    BacktestModel,
    ChronologicalFold,
    CohortDiagnostics,
    ParticipationCalibrationBin,
    PromotionGateConfig,
    block_bootstrap_mae_delta,
    evaluate_component_gates,
    evaluate_development_candidate,
    evaluate_promotion,
    regular_season_folds,
    run_validation_folds,
    segment_comparisons,
)
from sleeper_manager.domain.nba import AvailabilityStatus, SourceMetadata
from sleeper_manager.domain.projection import ProjectionDistribution, ProjectionSnapshot
from sleeper_manager.domain.scoring import BoxScoreLine, ScoringPolicy
from sleeper_manager.integrations.nba.historical_feature_dataset import (
    HistoricalFeatureDataset,
    HistoricalFeatureRow,
)
from sleeper_manager.integrations.nba.historical_feature_models import AvailabilityObservation

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


def _cohort_diagnostics(
    *,
    minutes_mae: float | None = 1.0,
    control_minutes_mae: float | None = 1.0,
    rate_mae: float | None = 1.0,
    control_rate_mae: float | None = 1.0,
    participation_brier: float | None = 0.1,
    control_participation_brier: float | None = 0.1,
    participation_calibration: tuple[ParticipationCalibrationBin, ...] = (),
) -> CohortDiagnostics:
    empty_metrics = BacktestMetrics(
        target_count=0,
        sample_count=0,
        coverage=0.0,
        mae=None,
        rmse=None,
        median_absolute_error=None,
        intervals=(),
        brier_scores=(),
    )
    return CohortDiagnostics(
        cohort="top_180",
        target_count=0,
        successful_count=0,
        coverage=0.0,
        skip_reasons={},
        full_mixture=empty_metrics,
        participation_brier=participation_brier,
        participation_sample_count=0,
        participation_calibration=participation_calibration,
        minutes_mae=minutes_mae,
        minutes_rmse=None,
        minutes_sample_count=0,
        rate_mae=rate_mae,
        rate_rmse=None,
        rate_sample_count=0,
        control_participation_brier=control_participation_brier,
        control_minutes_mae=control_minutes_mae,
        control_rate_mae=control_rate_mae,
    )


def test_component_gate_passes_within_frozen_two_percent_regression() -> None:
    diagnostics = _cohort_diagnostics(minutes_mae=1.015, control_minutes_mae=1.0)
    gates = {gate.name: gate for gate in evaluate_component_gates(diagnostics)}
    assert gates["minutes_non_regression"].passed


def test_component_gate_fails_beyond_frozen_two_percent_regression() -> None:
    diagnostics = _cohort_diagnostics(minutes_mae=1.05, control_minutes_mae=1.0)
    gates = {gate.name: gate for gate in evaluate_component_gates(diagnostics)}
    assert not gates["minutes_non_regression"].passed


def test_component_gate_hard_fails_on_missing_or_non_finite_candidate_output() -> None:
    diagnostics = _cohort_diagnostics(minutes_mae=None, control_minutes_mae=1.0)
    gates = {gate.name: gate for gate in evaluate_component_gates(diagnostics)}
    assert not gates["minutes_non_regression"].passed
    assert not gates["component_output_present"].passed


def test_participation_calibration_ignores_undersized_bins() -> None:
    undersized_and_miscalibrated = ParticipationCalibrationBin(0.8, 0.9, 50, 0.85, 0.10)
    diagnostics = _cohort_diagnostics(participation_calibration=(undersized_and_miscalibrated,))
    gates = {gate.name: gate for gate in evaluate_component_gates(diagnostics)}
    assert gates["participation_calibration"].passed


def test_participation_calibration_enforces_qualifying_bins() -> None:
    qualifying_and_miscalibrated = ParticipationCalibrationBin(0.8, 0.9, 150, 0.85, 0.10)
    diagnostics = _cohort_diagnostics(participation_calibration=(qualifying_and_miscalibrated,))
    gates = {gate.name: gate for gate in evaluate_component_gates(diagnostics)}
    assert not gates["participation_calibration"].passed
