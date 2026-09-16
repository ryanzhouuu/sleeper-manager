"""Model views, shared cohorts, and predicted-component visibility."""

from datetime import UTC, datetime

from sleeper_manager.backtesting import (
    BacktestConfig,
    BacktestModel,
    BacktestReport,
    CohortAssignment,
    NaiveProjectionBaseline,
    run_backtest,
)
from sleeper_manager.domain.scoring import calculate_fantasy_points
from sleeper_manager.projections.direct_baseline import DirectFantasyPointBaseline
from sleeper_manager.projections.opportunity_model import InterpretableOpportunityModel
from tests.sleeper_manager.backtesting.backtesting_support import (
    POLICY,
    FailingProjector,
    InspectingProjector,
    NoComponentProjector,
    fixture_dataset,
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
