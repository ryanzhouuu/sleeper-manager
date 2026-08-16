from __future__ import annotations

from bisect import bisect_left
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from itertools import islice
from math import exp, log, sqrt
from statistics import median
from typing import overload

from sleeper_manager.backtesting.cohorts import (
    CohortConfig,
    IndependentCohortRanker,
    cohort_for_rank,
)
from sleeper_manager.backtesting.models import (
    COHORT_NAMES,
    BacktestComparison,
    BacktestConfig,
    BacktestError,
    BacktestMetrics,
    BacktestModel,
    BacktestModelResult,
    BacktestObservation,
    BacktestReport,
    BacktestSkip,
    CohortAssignment,
    CohortDiagnostics,
    ComponentControlEstimate,
    IntervalMetric,
    ParticipationCalibrationBin,
    PredictedComponentDiagnostic,
    TargetSkip,
    model_names,
)
from sleeper_manager.domain.projection import ProjectionComponent, ProjectionSnapshot
from sleeper_manager.domain.scoring import BoxScoreLine, ScoringPolicy, calculate_fantasy_points
from sleeper_manager.integrations.nba.historical_features import (
    HistoricalFeatureDataset,
    HistoricalFeatureRow,
)

_PARTICIPATION_COMPONENT_CODE = "availability"
_MINUTES_COMPONENT_CODE = "minutes"
_RATE_COMPONENT_CODE = "production_rate"

# Frozen, prior-only control parameters. Deliberately independent of any candidate model's own
# configuration (OpportunityModelConfig, CohortConfig): the control must not share tuning with
# the thing it is used to gate.
_CONTROL_HALF_LIFE_DAYS = 14.0
_CONTROL_SHRINKAGE_MINUTES = 120.0
_CONTROL_RATE_PRIOR = 0.5

_CALIBRATION_BIN_EDGES: tuple[float, ...] = tuple(i / 10 for i in range(11))


def run_backtest(
    dataset: HistoricalFeatureDataset,
    *,
    scoring_policy: ScoringPolicy,
    models: Iterable[BacktestModel],
    config: BacktestConfig | None = None,
    reference_model: str | None = None,
    cohort_config: CohortConfig | None = None,
) -> BacktestReport:
    config = config or BacktestConfig()
    model_records = tuple(models)
    if not model_records:
        raise BacktestError("At least one backtest model is required")
    names = model_names(model_records)
    reference = reference_model or names[0]
    if reference not in names:
        raise BacktestError(f"Unknown reference model: {reference!r}")
    _validate_dataset(dataset)
    chronological_rows = tuple(
        sorted(dataset.rows, key=lambda row: (row.game_start, row.game_id, row.player_id))
    )
    game_starts = tuple(row.game_start for row in chronological_rows)
    targets, target_skips = _eligible_targets(chronological_rows, config)
    observations_by_model: dict[str, list[BacktestObservation]] = {
        model.name: [] for model in model_records
    }
    skips_by_model: dict[str, list[BacktestSkip]] = {model.name: [] for model in model_records}
    batch_player_ids: dict[datetime, list[str]] = {}
    for target in targets:
        batch_player_ids.setdefault(target.game_start, []).append(target.player_id)
    ranker = IndependentCohortRanker(cohort_config)
    current_batch_game_start: datetime | None = None
    current_rank_map: dict[str, int] = {}
    target_cohorts: dict[tuple[str, str], CohortAssignment] = {}

    for target in targets:
        if target.game_start != current_batch_game_start:
            current_rank_map = ranker.rank_players_as_of(
                chronological_rows,
                target.game_start,
                scoring_policy=scoring_policy,
                dataset_version=dataset.dataset_version,
                player_ids=batch_player_ids[target.game_start],
            )
            current_batch_game_start = target.game_start
        rank = current_rank_map.get(target.player_id)
        if rank is None:
            raise BacktestError(
                f"Target player {target.player_id!r} at {target.game_start!r} has no cohort rank"
            )
        cohort = CohortAssignment(rank=rank, tier=cohort_for_rank(rank), top_180=rank <= 180)
        target_cohorts[(target.player_id, target.game_id)] = cohort
        control = _component_control(
            ranker.prior_rows(target.player_id), target.game_start, scoring_policy
        )
        point_in_time_dataset = _point_in_time_dataset(
            dataset,
            chronological_rows,
            game_starts,
            target,
        )
        actual_score = calculate_fantasy_points(target.target_box_score, scoring_policy)
        realized_participation = target.target_did_play
        realized_minutes = target.target_minutes if target.target_did_play else None
        realized_rate = (
            actual_score / target.target_minutes
            if target.target_did_play and target.target_minutes
            else None
        )
        for model in model_records:
            try:
                snapshot = model.projector.project(
                    point_in_time_dataset,
                    player_id=target.player_id,
                    game_id=target.game_id,
                    scoring_policy=scoring_policy,
                )
            except ValueError as error:
                skips_by_model[model.name].append(
                    BacktestSkip(
                        model_name=model.name,
                        player_id=target.player_id,
                        game_id=target.game_id,
                        game_start=target.game_start,
                        reason=_error_reason(error),
                        cohort=cohort,
                    )
                )
                continue
            _validate_snapshot(snapshot, target, scoring_policy, config)
            observations_by_model[model.name].append(
                BacktestObservation(
                    player_id=target.player_id,
                    game_id=target.game_id,
                    game_start=target.game_start,
                    available_as_of=target.available_as_of,
                    actual_score=actual_score,
                    model_version=snapshot.model_version,
                    input_version=snapshot.input_version,
                    expected_value=snapshot.distribution.expected_value,
                    percentiles=snapshot.distribution.percentiles,
                    exceedance_probabilities=tuple(
                        (
                            threshold,
                            snapshot.distribution.probability_of_exceeding(threshold),
                        )
                        for threshold in config.thresholds
                    ),
                    cohort=cohort,
                    realized_participation=realized_participation,
                    realized_minutes=realized_minutes,
                    realized_rate=realized_rate,
                    components=snapshot.components,
                    predicted_component=_predicted_component(snapshot.components),
                    component_control=control,
                )
            )

    _validate_cohort_invariants(
        target_cohorts=target_cohorts,
        targets=targets,
        observations_by_model=observations_by_model,
        skips_by_model=skips_by_model,
    )
    results = tuple(
        BacktestModelResult(
            model=model,
            observations=tuple(observations_by_model[model.name]),
            skips=tuple(skips_by_model[model.name]),
            metrics=_metrics(
                observations_by_model[model.name],
                config,
                target_count=len(targets),
            ),
            cohort_diagnostics=_cohort_diagnostics(
                observations_by_model[model.name],
                skips_by_model[model.name],
                target_cohorts,
                config,
            ),
        )
        for model in model_records
    )
    comparisons = tuple(
        _compare_results(
            results=results,
            reference_model=reference,
            candidate_model=model.name,
            config=config,
        )
        for model in model_records
        if model.name != reference
    )
    return BacktestReport(
        dataset_version=dataset.dataset_version,
        scoring_policy_version=scoring_policy.version,
        config_version=config.version,
        target_count=len(targets),
        target_skips=tuple(target_skips),
        reference_model=reference,
        model_results=results,
        comparisons=comparisons,
    )


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
    minutes_estimate = _weighted_mean(weighted_minutes)
    raw_rate = _weighted_mean(weighted_rates)
    effective = sum(weight for _, weight in weighted_minutes)
    shrinkage = effective / (effective + _CONTROL_SHRINKAGE_MINUTES)
    rate_estimate = _CONTROL_RATE_PRIOR + shrinkage * (raw_rate - _CONTROL_RATE_PRIOR)
    return ComponentControlEstimate(
        participation=participation, minutes=minutes_estimate, rate=rate_estimate
    )


def _weighted_mean(values: Iterable[tuple[float, float]]) -> float:
    records = tuple(values)
    total = sum(weight for _, weight in records)
    return sum(value * weight for value, weight in records) / total if total else 0.0


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


def _validate_dataset(dataset: HistoricalFeatureDataset) -> None:
    if dataset.generated_at.tzinfo is None:
        raise BacktestError("Historical dataset generated_at must be timezone-aware")
    keys: set[tuple[str, str]] = set()
    for row in dataset.rows:
        key = row.player_id, row.game_id
        if key in keys:
            raise BacktestError(f"Duplicate historical feature row for {key!r}")
        keys.add(key)
        if row.game_start.tzinfo is None or row.available_as_of.tzinfo is None:
            raise BacktestError("Historical feature timestamps must be timezone-aware")
        if row.available_as_of > row.game_start:
            raise BacktestError(f"Feature row {key!r} is available after game start")


def _eligible_targets(
    rows: Iterable[HistoricalFeatureRow], config: BacktestConfig
) -> tuple[tuple[HistoricalFeatureRow, ...], tuple[TargetSkip, ...]]:
    records = tuple(sorted(rows, key=lambda row: (row.game_start, row.game_id, row.player_id)))
    targets: list[HistoricalFeatureRow] = []
    skips: list[TargetSkip] = []
    prior_games_by_player_season: dict[tuple[str, int], int] = {}
    pending_game_time: datetime | None = None
    pending_counts: dict[tuple[str, int], int] = {}
    for row in records:
        if pending_game_time is not None and row.game_start != pending_game_time:
            for key, count in pending_counts.items():
                prior_games_by_player_season[key] = prior_games_by_player_season.get(key, 0) + count
            pending_counts.clear()
        pending_game_time = row.game_start
        player_season = row.player_id, _season_key(row.game_start)
        prior_games = prior_games_by_player_season.get(player_season, 0)
        pending_counts[player_season] = pending_counts.get(player_season, 0) + 1
        if config.start_at is not None and row.game_start < config.start_at:
            continue
        if config.end_at is not None and row.game_start > config.end_at:
            continue
        if prior_games < config.min_prior_games:
            skips.append(
                TargetSkip(
                    player_id=row.player_id,
                    game_id=row.game_id,
                    game_start=row.game_start,
                    reason=(
                        f"Warmup requires {config.min_prior_games} prior same-season games; "
                        f"found {prior_games}."
                    ),
                )
            )
            continue
        targets.append(row)
    return tuple(targets), tuple(skips)


def _point_in_time_dataset(
    dataset: HistoricalFeatureDataset,
    chronological_rows: tuple[HistoricalFeatureRow, ...],
    game_starts: tuple[datetime, ...],
    target: HistoricalFeatureRow,
) -> HistoricalFeatureDataset:
    prior_count = bisect_left(game_starts, target.game_start)
    sanitized_target = replace(
        target,
        target_minutes=None,
        target_started=False,
        target_did_play=False,
        target_box_score=BoxScoreLine(),
        target_line_points=0,
        target_line_rebounds=0,
        target_line_assists=0,
        target_line_steals=0,
        target_line_blocks=0,
        target_line_turnovers=0,
    )
    return replace(
        dataset,
        rows=_PointInTimeRows(chronological_rows, prior_count, sanitized_target),
    )


class _PointInTimeRows(Sequence[HistoricalFeatureRow]):
    def __init__(
        self,
        rows: tuple[HistoricalFeatureRow, ...],
        prior_count: int,
        target: HistoricalFeatureRow,
    ) -> None:
        self._rows = rows
        self._prior_count = prior_count
        self._target = target

    def __len__(self) -> int:
        return self._prior_count + 1

    @property
    def prior_count(self) -> int:
        return self._prior_count

    def __iter__(self) -> Iterator[HistoricalFeatureRow]:
        yield from islice(self._rows, self._prior_count)
        yield self._target

    @overload
    def __getitem__(self, index: int) -> HistoricalFeatureRow: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[HistoricalFeatureRow, ...]: ...

    def __getitem__(
        self, index: int | slice
    ) -> HistoricalFeatureRow | tuple[HistoricalFeatureRow, ...]:
        if isinstance(index, slice):
            start, stop, step = index.indices(len(self))
            return tuple(self[position] for position in range(start, stop, step))
        normalized = index if index >= 0 else len(self) + index
        if normalized < 0 or normalized >= len(self):
            raise IndexError(index)
        if normalized == self._prior_count:
            return self._target
        return self._rows[normalized]


def _validate_snapshot(
    snapshot: ProjectionSnapshot,
    target: HistoricalFeatureRow,
    scoring_policy: ScoringPolicy,
    config: BacktestConfig,
) -> None:
    if snapshot.player_id != target.player_id or snapshot.game_id != target.game_id:
        raise BacktestError("Projection snapshot identity does not match the backtest target")
    if snapshot.available_as_of != target.available_as_of:
        raise BacktestError("Projection snapshot available_as_of does not match the target cutoff")
    if not snapshot.model_version.strip() or not snapshot.input_version.strip():
        raise BacktestError("Projection snapshots require model and input versions")
    if snapshot.scoring_policy_version != scoring_policy.version:
        raise BacktestError("Projection snapshot used a different scoring policy")
    percentile_values = dict(snapshot.distribution.percentiles)
    for lower, upper in config.intervals:
        if lower not in percentile_values or upper not in percentile_values:
            raise BacktestError(
                "Projection snapshot is missing configured interval percentiles "
                f"{lower} and {upper}"
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


def _season_key(value: datetime) -> int:
    return value.year if value.month >= 10 else value.year - 1
