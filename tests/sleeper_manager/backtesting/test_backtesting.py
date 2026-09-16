"""Walk-forward calibration and injury-mapping diagnostics."""

from dataclasses import replace

from sleeper_manager.backtesting import (
    BacktestConfig,
    BacktestModel,
    NaiveProjectionBaseline,
    run_backtest,
)
from sleeper_manager.backtesting.controls import CalibratedProjectionModel
from sleeper_manager.backtesting.experiments.feature_validation import (
    _injury_mapping_diagnostics,
    _isolated_suite,
)
from sleeper_manager.integrations.nba.official_injury_mapping import (
    InjuryMappingCategory,
    InjuryMappingDiagnostic,
)
from sleeper_manager.projections.direct_baseline import DirectFantasyPointBaseline
from tests.sleeper_manager.backtesting.backtesting_support import (
    POLICY,
    FixedProjector,
    RecordingProgress,
    fixture_dataset,
)


def test_backtest_reports_walk_forward_metrics_and_warmup_skips() -> None:
    progress = RecordingProgress()
    report = run_backtest(
        fixture_dataset(),
        scoring_policy=POLICY,
        models=(
            BacktestModel("direct", DirectFantasyPointBaseline()),
            BacktestModel("last_game", NaiveProjectionBaseline("last_game")),
            BacktestModel("season_average", NaiveProjectionBaseline("season_average")),
        ),
        progress=progress,
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
    assert progress.events == [(1, 3, None), (2, 3, None), (3, 3, None)]
    without_progress = run_backtest(
        fixture_dataset(),
        scoring_policy=POLICY,
        models=(
            BacktestModel("direct", DirectFantasyPointBaseline()),
            BacktestModel("last_game", NaiveProjectionBaseline("last_game")),
            BacktestModel("season_average", NaiveProjectionBaseline("season_average")),
        ),
    )
    assert report.target_skips == without_progress.target_skips
    assert report.comparisons == without_progress.comparisons
    assert (
        tuple(
            replace(result, model=without_progress.model_results[index].model)
            for index, result in enumerate(report.model_results)
        )
        == without_progress.model_results
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
