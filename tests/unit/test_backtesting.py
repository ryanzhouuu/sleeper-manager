from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from sleeper_manager.backtesting import (
    BacktestConfig,
    BacktestError,
    BacktestModel,
    BacktestObservation,
    BacktestReport,
    CohortAssignment,
    NaiveProjectionBaseline,
    run_backtest,
)
from sleeper_manager.backtesting.backtest_metrics import _validate_cohort_invariants
from sleeper_manager.backtesting.controls import CalibratedProjectionModel
from sleeper_manager.backtesting.experiments.feature_validation import (
    _injury_mapping_diagnostics,
    _isolated_suite,
)
from sleeper_manager.domain.nba import AvailabilityStatus, SourceMetadata
from sleeper_manager.domain.projection import ProjectionDistribution, ProjectionSnapshot
from sleeper_manager.domain.scoring import BoxScoreLine, ScoringPolicy, calculate_fantasy_points
from sleeper_manager.integrations.nba.historical_feature_dataset import (
    HistoricalFeatureDataset,
    HistoricalFeatureRow,
)
from sleeper_manager.integrations.nba.historical_feature_models import AvailabilityObservation
from sleeper_manager.integrations.nba.official_injury_mapping import (
    InjuryMappingCategory,
    InjuryMappingDiagnostic,
)
from sleeper_manager.projections.direct_baseline import DirectFantasyPointBaseline
from sleeper_manager.projections.opportunity_model import InterpretableOpportunityModel

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


def test_all_models_receive_identical_cohort_assignments_regardless_of_order() -> None:
    models_forward = (
        BacktestModel("direct", DirectFantasyPointBaseline()),
        BacktestModel("last_game", NaiveProjectionBaseline("last_game")),
        BacktestModel("season_average", NaiveProjectionBaseline("season_average")),
    )
    models_reversed = tuple(reversed(models_forward))

    report_forward = run_backtest(fixture_dataset(), scoring_policy=POLICY, models=models_forward)
    report_reversed = run_backtest(fixture_dataset(), scoring_policy=POLICY, models=models_reversed)

    def cohorts_by_target(report: BacktestReport) -> dict[tuple[str, str], CohortAssignment]:
        return {
            (observation.player_id, observation.game_id): observation.cohort
            for result in report.model_results
            for observation in result.observations
        }

    forward_cohorts = cohorts_by_target(report_forward)
    reversed_cohorts = cohorts_by_target(report_reversed)
    assert forward_cohorts == reversed_cohorts

    # Every model at a given target sees the exact same cohort assignment.
    for result in report_forward.model_results:
        for observation in result.observations:
            key = (observation.player_id, observation.game_id)
            assert observation.cohort == forward_cohorts[key]


def test_realized_component_targets_come_from_the_original_unsanitized_row() -> None:
    report = run_backtest(
        fixture_dataset(),
        scoring_policy=POLICY,
        models=(BacktestModel("last_game", NaiveProjectionBaseline("last_game")),),
    )
    rows_by_key = {(r.player_id, r.game_id): r for r in fixture_dataset().rows}

    for observation in report.result_for("last_game").observations:
        original = rows_by_key[(observation.player_id, observation.game_id)]
        assert observation.realized_participation == original.target_did_play
        assert observation.realized_minutes == original.target_minutes
        expected_rate = (
            calculate_fantasy_points(original.target_box_score, POLICY) / original.target_minutes
            if original.target_did_play and original.target_minutes
            else None
        )
        assert observation.realized_rate == expected_rate


def test_skips_retain_cohort_denominators_and_stable_reasons() -> None:
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
    skip = candidate.skips[0]
    assert skip.game_id == "p1-g3"
    assert "candidate intentionally unavailable" in skip.reason

    reference_cohort = next(
        observation.cohort
        for observation in report.result_for("reference").observations
        if observation.game_id == "p1-g3"
    )
    assert skip.cohort == reference_cohort
    assert skip.cohort.rank >= 1


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


def test_missing_candidate_components_are_visible_rather_than_imputed() -> None:
    report = run_backtest(
        fixture_dataset(),
        scoring_policy=POLICY,
        models=(BacktestModel("no-component", NoComponentProjector()),),
    )

    for observation in report.result_for("no-component").observations:
        assert observation.components == ()
        assert observation.predicted_component.participation is None
        assert observation.predicted_component.minutes is None
        assert observation.predicted_component.rate is None
        assert observation.predicted_component.missing_reasons == (
            "missing_availability_component",
            "missing_minutes_component",
            "missing_production_rate_component",
        )


def test_opportunity_model_predicted_components_are_populated_without_missing_reasons() -> None:
    report = run_backtest(
        fixture_dataset(),
        scoring_policy=POLICY,
        models=(BacktestModel("opportunity", InterpretableOpportunityModel()),),
    )

    observations = report.result_for("opportunity").observations
    assert observations
    for observation in observations:
        assert observation.components
        assert observation.predicted_component.participation is not None
        assert observation.predicted_component.minutes is not None
        assert observation.predicted_component.rate is not None
        assert observation.predicted_component.missing_reasons == ()


def test_backtest_config_rejects_unordered_or_naive_inputs() -> None:
    with pytest.raises(BacktestError):
        BacktestConfig(thresholds=(20, 10))
    with pytest.raises(BacktestError):
        NaiveProjectionBaseline("unknown")  # type: ignore[arg-type]


def test_hand_calculated_tiered_full_mixture_metrics_and_top_180_union() -> None:
    day_minus_one = datetime(2025, 11, 4, tzinfo=UTC)
    day_zero = datetime(2025, 11, 5, tzinfo=UTC)
    # 181 single-row players establish a deterministic rank 1..181 by strictly decreasing points.
    rows = [row(f"rank-anchor-{i}", f"player-{i}", day_minus_one, 1000 - i) for i in range(181)]
    # Ranks 1 (top_108), 150 (ranks_109_180), and 181 (below_180) also get an evaluated target
    # the next day, each with its own known actual score for a hand-calculable MAE.
    target_actuals = {0: 10, 149: 30, 180: 50}
    for index, points in target_actuals.items():
        rows.append(row(f"target-{index}", f"player-{index}", day_zero, points))

    dataset = HistoricalFeatureDataset(
        dataset_version="tiered-fixture",
        feature_schema_version="1",
        generated_at=datetime(2026, 8, 10, tzinfo=UTC),
        source_versions=(),
        rows=tuple(rows),
    )
    report = run_backtest(
        dataset,
        scoring_policy=POLICY,
        models=(BacktestModel("fixed", FixedProjector()),),
    )
    result = report.result_for("fixed")
    assert result.metrics.sample_count == 3

    diagnostics = {d.cohort: d for d in result.cohort_diagnostics}
    assert diagnostics["top_108"].target_count == 1
    assert diagnostics["top_108"].successful_count == 1
    assert diagnostics["top_108"].full_mixture.mae == 10.0
    assert diagnostics["ranks_109_180"].target_count == 1
    assert diagnostics["ranks_109_180"].full_mixture.mae == 30.0
    assert diagnostics["below_180"].target_count == 1
    assert diagnostics["below_180"].full_mixture.mae == 50.0

    # top_180 equals the union of its two exclusive tiers, with a hand-calculable combined MAE.
    top_180 = diagnostics["top_180"]
    assert top_180.target_count == (
        diagnostics["top_108"].target_count + diagnostics["ranks_109_180"].target_count
    )
    assert top_180.successful_count == (
        diagnostics["top_108"].successful_count + diagnostics["ranks_109_180"].successful_count
    )
    assert top_180.full_mixture.mae == 20.0  # (10 + 30) / 2

    # Intervals and exceedance (Brier) metrics remain present and computed per cohort.
    assert top_180.full_mixture.intervals[0].observed_coverage is not None
    assert all(score is not None for _, score in top_180.full_mixture.brier_scores)


def test_dnp_participation_and_conditional_denominator_behavior() -> None:
    base = datetime(2025, 11, 1, tzinfo=UTC)
    rows = [
        row(f"p-dnp-prior-{day}", "p-dnp", base + timedelta(days=day), 10 + day) for day in range(3)
    ]
    dnp_target = replace(
        row("p-dnp-target", "p-dnp", base + timedelta(days=3), 0),
        target_did_play=False,
        target_minutes=None,
        target_box_score=BoxScoreLine(),
        target_line_points=0,
    )
    rows.append(dnp_target)
    rows.extend(
        row(f"p-play-prior-{day}", "p-play", base + timedelta(days=day), 20 + day)
        for day in range(3)
    )
    rows.append(row("p-play-target", "p-play", base + timedelta(days=3), 25))

    dataset = HistoricalFeatureDataset(
        dataset_version="dnp-fixture",
        feature_schema_version="1",
        generated_at=datetime(2026, 8, 10, tzinfo=UTC),
        source_versions=(),
        rows=tuple(rows),
    )
    report = run_backtest(
        dataset,
        scoring_policy=POLICY,
        models=(BacktestModel("opportunity", InterpretableOpportunityModel()),),
        config=BacktestConfig(min_prior_games=3),
    )
    result = report.result_for("opportunity")

    dnp_observation = next(
        o for o in result.observations if o.player_id == "p-dnp" and o.game_id == "p-dnp-target"
    )
    assert dnp_observation.realized_participation is False
    assert dnp_observation.realized_minutes is None
    assert dnp_observation.realized_rate is None

    top_180 = next(d for d in result.cohort_diagnostics if d.cohort == "top_180")
    assert top_180.full_mixture.sample_count == 2
    # Participation is scored for both targets regardless of outcome...
    assert top_180.participation_sample_count == 2
    # ...but the conditional minutes/rate denominators exclude the DNP target.
    assert top_180.minutes_sample_count == 1
    assert top_180.rate_sample_count == 1


def test_unequal_model_skips_do_not_change_cohort_assignment_or_original_denominators() -> None:
    report = run_backtest(
        fixture_dataset(),
        scoring_policy=POLICY,
        models=(
            BacktestModel("reference", NaiveProjectionBaseline("last_game")),
            BacktestModel("candidate", FailingProjector()),
        ),
        reference_model="reference",
    )
    reference_diagnostics = {d.cohort: d for d in report.result_for("reference").cohort_diagnostics}
    candidate_diagnostics = {d.cohort: d for d in report.result_for("candidate").cohort_diagnostics}

    for name in reference_diagnostics:
        # The pre-skip target_count denominator is identical across models even though the
        # candidate skipped one target the reference did not.
        assert reference_diagnostics[name].target_count == candidate_diagnostics[name].target_count
    assert reference_diagnostics["top_180"].successful_count == 3
    assert candidate_diagnostics["top_180"].successful_count == 2

    reference_cohorts = {
        (o.player_id, o.game_id): o.cohort for o in report.result_for("reference").observations
    }
    candidate_cohorts = {
        (o.player_id, o.game_id): o.cohort for o in report.result_for("candidate").observations
    }
    for key, cohort in candidate_cohorts.items():
        assert reference_cohorts[key] == cohort
    skip = report.result_for("candidate").skips[0]
    assert reference_cohorts[(skip.player_id, skip.game_id)] == skip.cohort


def test_invariant_violation_fails_closed() -> None:
    target = row("p1-g1", "p1", datetime(2025, 1, 1, tzinfo=UTC), 10)
    correct_cohort = CohortAssignment(rank=1, tier="top_108", top_180=True)
    wrong_cohort = CohortAssignment(rank=200, tier="below_180", top_180=False)
    bad_observation = BacktestObservation(
        player_id="p1",
        game_id="p1-g1",
        game_start=target.game_start,
        available_as_of=target.available_as_of,
        actual_score=10.0,
        model_version="v1",
        input_version="v1",
        expected_value=10.0,
        percentiles=((50, 10.0),),
        exceedance_probabilities=(),
        cohort=wrong_cohort,
    )
    with pytest.raises(BacktestError):
        _validate_cohort_invariants(
            target_cohorts={("p1", "p1-g1"): correct_cohort},
            targets=(target,),
            observations_by_model={"m": (bad_observation,)},
            skips_by_model={"m": ()},
        )
