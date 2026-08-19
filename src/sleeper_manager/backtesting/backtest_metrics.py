from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from math import exp, log, sqrt
from statistics import median

from sleeper_manager.backtesting.models import (
    COHORT_NAMES,
    BacktestComparison,
    BacktestConfig,
    BacktestError,
    BacktestMetrics,
    BacktestModelResult,
    BacktestObservation,
    BacktestSkip,
    CohortAssignment,
    CohortDiagnostics,
    ComponentControlEstimate,
    IntervalMetric,
    ParticipationCalibrationBin,
    PredictedComponentDiagnostic,
)
from sleeper_manager.domain.projection import ProjectionComponent
from sleeper_manager.domain.scoring import ScoringPolicy, calculate_fantasy_points
from sleeper_manager.domain.statistics import weighted_mean
from sleeper_manager.integrations.nba.historical_feature_models import HistoricalFeatureRow

_PARTICIPATION_COMPONENT_CODE = "availability"
_MINUTES_COMPONENT_CODE = "minutes"
_RATE_COMPONENT_CODE = "production_rate"

_CONTROL_HALF_LIFE_DAYS = 14.0
_CONTROL_SHRINKAGE_MINUTES = 120.0
_CONTROL_RATE_PRIOR = 0.5

_CALIBRATION_BIN_EDGES: tuple[float, ...] = tuple(i / 10 for i in range(11))


def _predicted_component(
    components: tuple[ProjectionComponent, ...],
) -> PredictedComponentDiagnostic:
    by_code = {component.code: component for component in components}
    missing: list[str] = []
    participation = _component_value(
        by_code, _PARTICIPATION_COMPONENT_CODE, missing, "missing_availability_component"
    )
    minutes = _component_value(
        by_code, _MINUTES_COMPONENT_CODE, missing, "missing_minutes_component"
    )
    rate = _component_value(
        by_code, _RATE_COMPONENT_CODE, missing, "missing_production_rate_component"
    )
    return PredictedComponentDiagnostic(participation, minutes, rate, tuple(missing))


def _component_value(
    by_code: Mapping[str, ProjectionComponent],
    code: str,
    missing: list[str],
    reason: str,
) -> float | None:
    component = by_code.get(code)
    if component is None:
        missing.append(reason)
        return None
    return component.estimate


def _component_control(
    prior_rows: Sequence[HistoricalFeatureRow],
    as_of: datetime,
    scoring_policy: ScoringPolicy,
) -> ComponentControlEstimate:
    if not prior_rows:
        return ComponentControlEstimate(participation=None, minutes=None, rate=None)
    participation = sum(row.target_did_play for row in prior_rows) / len(prior_rows)
    played = tuple(row for row in prior_rows if row.target_did_play and row.target_minutes)
    if not played:
        return ComponentControlEstimate(participation=participation, minutes=None, rate=None)
    weighted_minutes: list[tuple[float, float]] = []
    weighted_rates: list[tuple[float, float]] = []
    for row in played:
        minutes = row.target_minutes
        assert minutes is not None  # narrowed by the `played` filter above
        weight = exp(
            -log(2)
            * max((as_of - row.game_start).total_seconds(), 0)
            / 86400
            / _CONTROL_HALF_LIFE_DAYS
        )
        weighted_minutes.append((minutes, weight))
        weighted_rates.append(
            (
                calculate_fantasy_points(row.target_box_score, scoring_policy) / minutes,
                weight * minutes,
            )
        )
    minutes_estimate = _weighted_mean_or_zero(weighted_minutes)
    raw_rate = _weighted_mean_or_zero(weighted_rates)
    effective = sum(weight for _, weight in weighted_minutes)
    shrinkage = effective / (effective + _CONTROL_SHRINKAGE_MINUTES)
    rate_estimate = _CONTROL_RATE_PRIOR + shrinkage * (raw_rate - _CONTROL_RATE_PRIOR)
    return ComponentControlEstimate(
        participation=participation, minutes=minutes_estimate, rate=rate_estimate
    )


def _weighted_mean_or_zero(values: Iterable[tuple[float, float]]) -> float:
    try:
        return weighted_mean(values)
    except ValueError:
        return 0.0


def cohort_matches(cohort: CohortAssignment, name: str) -> bool:
    if name == "top_180":
        return cohort.top_180
    return cohort.tier == name


def _cohort_diagnostics(
    observations: Sequence[BacktestObservation],
    skips: Sequence[BacktestSkip],
    target_cohorts: Mapping[tuple[str, str], CohortAssignment],
    config: BacktestConfig,
) -> tuple[CohortDiagnostics, ...]:
    diagnostics: list[CohortDiagnostics] = []
    for name in COHORT_NAMES:
        target_count = sum(1 for cohort in target_cohorts.values() if cohort_matches(cohort, name))
        cohort_observations = tuple(
            observation for observation in observations if cohort_matches(observation.cohort, name)
        )
        cohort_skips = tuple(skip for skip in skips if cohort_matches(skip.cohort, name))
        participation_brier, participation_sample, calibration = _participation_diagnostics(
            cohort_observations
        )
        minutes_mae, minutes_rmse, minutes_sample = _minutes_conditional_error(cohort_observations)
        rate_mae, rate_rmse, rate_sample = _rate_conditional_error(cohort_observations)
        diagnostics.append(
            CohortDiagnostics(
                cohort=name,
                target_count=target_count,
                successful_count=len(cohort_observations),
                coverage=round(len(cohort_observations) / target_count, 6) if target_count else 0.0,
                skip_reasons=dict(Counter(skip.reason for skip in cohort_skips)),
                full_mixture=_metrics(cohort_observations, config, target_count=target_count),
                participation_brier=participation_brier,
                participation_sample_count=participation_sample,
                participation_calibration=calibration,
                minutes_mae=minutes_mae,
                minutes_rmse=minutes_rmse,
                minutes_sample_count=minutes_sample,
                rate_mae=rate_mae,
                rate_rmse=rate_rmse,
                rate_sample_count=rate_sample,
                control_participation_brier=_participation_control_brier(cohort_observations),
                control_minutes_mae=_minutes_control_error(cohort_observations),
                control_rate_mae=_rate_control_error(cohort_observations),
            )
        )
    return tuple(diagnostics)


def _participation_diagnostics(
    observations: Sequence[BacktestObservation],
) -> tuple[float | None, int, tuple[ParticipationCalibrationBin, ...]]:
    scored = tuple(
        (observation.predicted_component.participation, observation.realized_participation)
        for observation in observations
        if observation.predicted_component.participation is not None
    )
    if not scored:
        return None, 0, _calibration_bins(())
    brier = sum((predicted - float(actual)) ** 2 for predicted, actual in scored) / len(scored)
    return round(brier, 6), len(scored), _calibration_bins(scored)


def _calibration_bins(
    scored: Sequence[tuple[float, bool]],
) -> tuple[ParticipationCalibrationBin, ...]:
    bins: list[ParticipationCalibrationBin] = []
    for lower, upper in zip(_CALIBRATION_BIN_EDGES, _CALIBRATION_BIN_EDGES[1:], strict=False):
        members = tuple(
            (predicted, actual)
            for predicted, actual in scored
            if (lower <= predicted < upper) or (upper == 1.0 and predicted == 1.0)
        )
        if not members:
            bins.append(ParticipationCalibrationBin(lower, upper, 0, None, None))
            continue
        predicted_mean = sum(predicted for predicted, _ in members) / len(members)
        observed_frequency = sum(actual for _, actual in members) / len(members)
        bins.append(
            ParticipationCalibrationBin(
                lower,
                upper,
                len(members),
                round(predicted_mean, 6),
                round(observed_frequency, 6),
            )
        )
    return tuple(bins)


def _minutes_conditional_error(
    observations: Sequence[BacktestObservation],
) -> tuple[float | None, float | None, int]:
    pairs = tuple(
        (observation.predicted_component.minutes, observation.realized_minutes)
        for observation in observations
        if observation.realized_participation
        and observation.realized_minutes is not None
        and observation.predicted_component.minutes is not None
    )
    return _mae_rmse(pairs)


def _rate_conditional_error(
    observations: Sequence[BacktestObservation],
) -> tuple[float | None, float | None, int]:
    pairs = tuple(
        (observation.predicted_component.rate, observation.realized_rate)
        for observation in observations
        if observation.realized_participation
        and observation.realized_rate is not None
        and observation.predicted_component.rate is not None
    )
    return _mae_rmse(pairs)


def _participation_control_brier(observations: Sequence[BacktestObservation]) -> float | None:
    scored = tuple(
        (observation.component_control.participation, observation.realized_participation)
        for observation in observations
        if observation.component_control.participation is not None
    )
    if not scored:
        return None
    brier = sum((predicted - float(actual)) ** 2 for predicted, actual in scored) / len(scored)
    return round(brier, 6)


def _minutes_control_error(observations: Sequence[BacktestObservation]) -> float | None:
    pairs = tuple(
        (observation.component_control.minutes, observation.realized_minutes)
        for observation in observations
        if observation.realized_participation
        and observation.realized_minutes is not None
        and observation.component_control.minutes is not None
    )
    mae, _, _ = _mae_rmse(pairs)
    return mae


def _rate_control_error(observations: Sequence[BacktestObservation]) -> float | None:
    pairs = tuple(
        (observation.component_control.rate, observation.realized_rate)
        for observation in observations
        if observation.realized_participation
        and observation.realized_rate is not None
        and observation.component_control.rate is not None
    )
    mae, _, _ = _mae_rmse(pairs)
    return mae


def _mae_rmse(
    pairs: Sequence[tuple[float | None, float | None]],
) -> tuple[float | None, float | None, int]:
    complete = tuple(
        (predicted, actual)
        for predicted, actual in pairs
        if predicted is not None and actual is not None
    )
    if not complete:
        return None, None, 0
    errors = tuple(predicted - actual for predicted, actual in complete)
    mae = sum(abs(error) for error in errors) / len(errors)
    rmse = sqrt(sum(error * error for error in errors) / len(errors))
    return round(mae, 6), round(rmse, 6), len(complete)


def _validate_cohort_invariants(
    *,
    target_cohorts: Mapping[tuple[str, str], CohortAssignment],
    targets: Sequence[HistoricalFeatureRow],
    observations_by_model: Mapping[str, Sequence[BacktestObservation]],
    skips_by_model: Mapping[str, Sequence[BacktestSkip]],
) -> None:
    if len(target_cohorts) != len(targets):
        raise BacktestError("Cohort assignment count does not match the eligible target count")
    for model_name, observations in observations_by_model.items():
        skips = skips_by_model[model_name]
        seen_keys: set[tuple[str, str]] = set()
        for observation in observations:
            key = (observation.player_id, observation.game_id)
            if key in seen_keys:
                raise BacktestError(f"Duplicate observation key {key!r} for model {model_name!r}")
            seen_keys.add(key)
            expected = target_cohorts.get(key)
            if expected is None or observation.cohort != expected:
                raise BacktestError(
                    f"Observation cohort for {key!r} does not match the shared rank map"
                )
        for skip in skips:
            key = (skip.player_id, skip.game_id)
            if key in seen_keys:
                raise BacktestError(f"Duplicate skip key {key!r} for model {model_name!r}")
            seen_keys.add(key)
            expected = target_cohorts.get(key)
            if expected is None or skip.cohort != expected:
                raise BacktestError(f"Skip cohort for {key!r} does not match the shared rank map")
        if len(seen_keys) != len(target_cohorts):
            raise BacktestError(
                f"Model {model_name!r} did not process every eligible target exactly once "
                f"({len(seen_keys)} of {len(target_cohorts)})"
            )


def cohort_comparison(
    *,
    reference: BacktestModelResult,
    candidate: BacktestModelResult,
    cohort: str,
    config: BacktestConfig,
) -> BacktestComparison:
    """Compare two models on their common successful observations within one report cohort."""
    reference_observations = tuple(
        observation
        for observation in reference.observations
        if cohort_matches(observation.cohort, cohort)
    )
    candidate_observations = tuple(
        observation
        for observation in candidate.observations
        if cohort_matches(observation.cohort, cohort)
    )
    return compare_observation_sets(
        reference_model=reference.model.name,
        candidate_model=candidate.model.name,
        reference_observations=reference_observations,
        candidate_observations=candidate_observations,
        config=config,
    )


def _metrics(
    observations: Iterable[BacktestObservation],
    config: BacktestConfig,
    *,
    target_count: int,
) -> BacktestMetrics:
    records = tuple(observations)
    sample_count = len(records)
    coverage = sample_count / target_count if target_count else 0.0
    mae: float | None
    rmse: float | None
    median_absolute_error: float | None
    if records:
        absolute_errors = tuple(record.absolute_error for record in records)
        mae = sum(absolute_errors) / sample_count
        rmse = sqrt(sum(record.squared_error for record in records) / sample_count)
        median_absolute_error = median(absolute_errors)
    else:
        mae = rmse = median_absolute_error = None
    intervals = tuple(_interval_metric(records, lower, upper) for lower, upper in config.intervals)
    brier_scores = tuple(
        (threshold, _brier_score(records, threshold)) for threshold in config.thresholds
    )
    return BacktestMetrics(
        target_count=target_count,
        sample_count=sample_count,
        coverage=round(coverage, 6),
        mae=_round_optional(mae),
        rmse=_round_optional(rmse),
        median_absolute_error=_round_optional(median_absolute_error),
        intervals=intervals,
        brier_scores=tuple(
            (threshold, _round_optional(score)) for threshold, score in brier_scores
        ),
    )


def _interval_metric(
    observations: tuple[BacktestObservation, ...], lower: int, upper: int
) -> IntervalMetric:
    nominal = (upper - lower) / 100
    if not observations:
        return IntervalMetric(lower, upper, nominal, None, None)
    observed = 0
    widths: list[float] = []
    for observation in observations:
        values = dict(observation.percentiles)
        lower_value = values[lower]
        upper_value = values[upper]
        observed += lower_value <= observation.actual_score <= upper_value
        widths.append(upper_value - lower_value)
    return IntervalMetric(
        lower,
        upper,
        nominal,
        round(observed / len(observations), 6),
        round(sum(widths) / len(widths), 6),
    )


def _brier_score(observations: tuple[BacktestObservation, ...], threshold: float) -> float | None:
    if not observations:
        return None
    errors = tuple(
        (
            observation.probability_of_exceeding(threshold)
            - float(observation.actual_score > threshold)
        )
        ** 2
        for observation in observations
    )
    return sum(errors) / len(errors)


def _compare_results(
    *,
    results: tuple[BacktestModelResult, ...],
    reference_model: str,
    candidate_model: str,
    config: BacktestConfig,
) -> BacktestComparison:
    reference = next(result for result in results if result.model.name == reference_model)
    candidate = next(result for result in results if result.model.name == candidate_model)
    return compare_observation_sets(
        reference_model=reference_model,
        candidate_model=candidate_model,
        reference_observations=reference.observations,
        candidate_observations=candidate.observations,
        config=config,
    )


def compare_observation_sets(
    *,
    reference_model: str,
    candidate_model: str,
    reference_observations: Sequence[BacktestObservation],
    candidate_observations: Sequence[BacktestObservation],
    config: BacktestConfig,
) -> BacktestComparison:
    reference_by_key = {
        (observation.player_id, observation.game_id): observation
        for observation in reference_observations
    }
    candidate_by_key = {
        (observation.player_id, observation.game_id): observation
        for observation in candidate_observations
    }
    keys = tuple(sorted(reference_by_key.keys() & candidate_by_key.keys()))
    common_reference = tuple(reference_by_key[key] for key in keys)
    common_candidate = tuple(candidate_by_key[key] for key in keys)
    reference_metrics = _metrics(common_reference, config, target_count=len(keys))
    candidate_metrics = _metrics(common_candidate, config, target_count=len(keys))
    return BacktestComparison(
        reference_model=reference_model,
        candidate_model=candidate_model,
        common_sample_count=len(keys),
        mae_delta=_delta(candidate_metrics.mae, reference_metrics.mae),
        rmse_delta=_delta(candidate_metrics.rmse, reference_metrics.rmse),
        median_absolute_error_delta=_delta(
            candidate_metrics.median_absolute_error, reference_metrics.median_absolute_error
        ),
        brier_score_deltas=tuple(
            (
                candidate_score[0],
                _delta(candidate_score[1], reference_score[1]),
            )
            for candidate_score, reference_score in zip(
                candidate_metrics.brier_scores, reference_metrics.brier_scores, strict=True
            )
        ),
    )


def _delta(candidate: float | None, reference: float | None) -> float | None:
    if candidate is None or reference is None:
        return None
    return round(candidate - reference, 6)


def _round_optional(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def _error_reason(error: ValueError) -> str:
    message = str(error).strip()
    return f"{type(error).__name__}: {message}" if message else type(error).__name__
