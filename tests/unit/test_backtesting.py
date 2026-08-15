from datetime import UTC, datetime, timedelta

import pytest

from sleeper_manager.backtesting import (
    BacktestConfig,
    BacktestError,
    BacktestModel,
    NaiveProjectionBaseline,
    run_backtest,
)
from sleeper_manager.backtesting.controls import CalibratedProjectionModel
from sleeper_manager.backtesting.experiment import _injury_mapping_diagnostics, _isolated_suite
from sleeper_manager.domain.nba import AvailabilityStatus, SourceMetadata
from sleeper_manager.domain.projection import ProjectionDistribution, ProjectionSnapshot
from sleeper_manager.domain.scoring import BoxScoreLine, ScoringPolicy
from sleeper_manager.integrations.nba.historical_features import (
    AvailabilityObservation,
    HistoricalFeatureDataset,
    HistoricalFeatureRow,
)
from sleeper_manager.integrations.nba.official_injury_mapping import (
    InjuryMappingCategory,
    InjuryMappingDiagnostic,
)
from sleeper_manager.projections.direct_baseline import DirectFantasyPointBaseline

SOURCE = SourceMetadata("fixture", "fixture", datetime(2026, 8, 10, tzinfo=UTC))
POLICY = ScoringPolicy(points=1)


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


def test_backtest_reports_walk_forward_metrics_and_warmup_skips() -> None:
    report = run_backtest(
        fixture_dataset(),
        scoring_policy=POLICY,
        models=(
            BacktestModel("direct", DirectFantasyPointBaseline()),
            BacktestModel("last_game", NaiveProjectionBaseline("last_game")),
            BacktestModel("season_average", NaiveProjectionBaseline("season_average")),
        ),
    )

    assert report.target_count == 3
    assert len(report.target_skips) == 2
    direct = report.result_for("direct")
    assert direct.metrics.sample_count == 3
    assert direct.metrics.coverage == 1
    assert direct.metrics.mae is not None
    assert direct.metrics.rmse is not None
    assert direct.metrics.median_absolute_error is not None
    assert direct.metrics.intervals[0].observed_coverage is not None
    assert direct.metrics.intervals[0].mean_width is not None
    assert tuple(threshold for threshold, _ in direct.metrics.brier_scores) == (
        10.0,
        20.0,
        30.0,
        40.0,
    )
    assert len(report.comparisons) == 2
    assert all(comparison.common_sample_count == 3 for comparison in report.comparisons)


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


def test_calibrated_projection_uses_only_prior_residuals() -> None:
    report = run_backtest(
        fixture_dataset(),
        scoring_policy=POLICY,
        models=(
            BacktestModel(
                "calibrated",
                CalibratedProjectionModel(
                    FixedProjector(),
                    min_samples=3,
                    max_samples=10,
                    refresh_interval=1,
                ),
            ),
        ),
        config=BacktestConfig(min_prior_games=0),
    )

    observations = {
        observation.game_id: observation
        for observation in report.result_for("calibrated").observations
    }
    assert observations["p1-g1"].percentiles == (
        (10, -5.0),
        (25, -5.0),
        (50, 0.0),
        (75, 5.0),
        (90, 5.0),
    )
    assert observations["p1-g3"].percentiles[0][1] < observations["p1-g1"].percentiles[0][1]
    assert observations["p1-g3"].percentiles[-1][1] > observations["p1-g1"].percentiles[-1][1]


def test_validation_suite_uses_season_average_reference_and_keeps_direct_control() -> None:
    suite = _isolated_suite()

    assert tuple(model.name for model in suite.models[:3]) == (
        "reference",
        "direct_baseline",
        "last_game",
    )
    reference = suite.models[0].projector.projector.projector.projector
    assert isinstance(reference, NaiveProjectionBaseline)
    assert reference.kind == "season_average"


def test_calibrated_validation_suite_runs_walk_forward() -> None:
    suite = _isolated_suite()

    report = run_backtest(
        fixture_dataset(),
        scoring_policy=POLICY,
        models=suite.models,
        reference_model="reference",
    )

    assert report.result_for("reference").metrics.sample_count > 0
    assert {comparison.candidate_model for comparison in report.comparisons} >= {
        "direct_baseline",
        "opponent_strength",
    }


def test_injury_mapping_diagnostics_are_grouped_by_season_and_team() -> None:
    report = _injury_mapping_diagnostics(
        (
            InjuryMappingDiagnostic(InjuryMappingCategory.RESOLVED, 2022, "chi", "a", 2),
            InjuryMappingDiagnostic(
                InjuryMappingCategory.NO_NAME_TEAM_MATCH, 2022, "chi", "missing", 3
            ),
            InjuryMappingDiagnostic(
                InjuryMappingCategory.RESOLVED_SUBSET_NAME_TEAM, 2022, "chi", "alias", 4
            ),
            InjuryMappingDiagnostic(
                InjuryMappingCategory.RESOLVED_HISTORICAL_NAME_TEAM,
                2022,
                "chi",
                "historical",
                5,
            ),
            InjuryMappingDiagnostic(
                InjuryMappingCategory.AMBIGUOUS_NAME_TEAM_MATCH, 2023, "bos", "duplicate", 1
            ),
        )
    )

    assert report["mapping_category_counts"] == {
        "ambiguous_name_team_match": 1,
        "no_name_team_match": 3,
        "resolved": 2,
        "resolved_historical_name_team": 5,
        "resolved_subset_name_team": 4,
    }
    assert report["mapping_coverage_by_season"]["2022-23"] == {
        "no_name_team_match": 3,
        "resolved_historical_name_team": 5,
        "resolved": 2,
        "resolved_subset_name_team": 4,
    }
    assert report["mapping_coverage_by_season_team"]["2023-24"]["bos"] == {
        "ambiguous_name_team_match": 1
    }
    assert report["unresolved_name_team_examples"][0]["normalized_name"] == "missing"


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


def test_backtest_model_view_contains_only_prior_games_and_sanitized_target() -> None:
    projector = InspectingProjector()

    report = run_backtest(
        fixture_dataset(),
        scoring_policy=POLICY,
        models=(BacktestModel("inspector", projector),),
        config=BacktestConfig(start_at=datetime(2025, 1, 3, tzinfo=UTC)),
    )

    assert report.target_count == 2
    assert [target.game_id for target in projector.target_rows] == ["p1-g3", "p2-g2"]


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


def test_comparisons_use_common_successful_targets_and_preserve_skip_reason() -> None:
    report = run_backtest(
        fixture_dataset(),
        scoring_policy=POLICY,
        models=(
            BacktestModel("reference", NaiveProjectionBaseline("last_game")),
            BacktestModel("candidate", FailingProjector()),
        ),
        reference_model="reference",
    )

    candidate = report.result_for("candidate")
    assert len(candidate.observations) == 2
    assert len(candidate.skips) == 1
    assert candidate.skips[0].game_id == "p1-g3"
    assert "candidate intentionally unavailable" in candidate.skips[0].reason
    assert report.comparisons[0].common_sample_count == 2


def test_backtest_config_rejects_unordered_or_naive_inputs() -> None:
    with pytest.raises(BacktestError):
        BacktestConfig(thresholds=(20, 10))
    with pytest.raises(BacktestError):
        NaiveProjectionBaseline("unknown")  # type: ignore[arg-type]
