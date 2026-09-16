"""Config guards, mixture metrics, DNP denominators, and invariant failures."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from sleeper_manager.backtesting import (
    BacktestConfig,
    BacktestError,
    BacktestModel,
    BacktestObservation,
    CohortAssignment,
    NaiveProjectionBaseline,
    run_backtest,
)
from sleeper_manager.backtesting.backtest_metrics import _validate_cohort_invariants
from sleeper_manager.domain.scoring import BoxScoreLine
from sleeper_manager.integrations.nba.historical_feature_dataset import (
    HistoricalFeatureDataset,
)
from sleeper_manager.projections.opportunity_model import InterpretableOpportunityModel
from tests.sleeper_manager.backtesting.backtesting_support import (
    POLICY,
    FailingProjector,
    FixedProjector,
    fixture_dataset,
    row,
)


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
