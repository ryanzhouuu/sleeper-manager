from __future__ import annotations

from bisect import bisect_left
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import replace
from datetime import datetime
from itertools import islice
from math import sqrt
from statistics import median
from typing import overload

from sleeper_manager.backtesting.models import (
    BacktestComparison,
    BacktestConfig,
    BacktestError,
    BacktestMetrics,
    BacktestModel,
    BacktestModelResult,
    BacktestObservation,
    BacktestReport,
    BacktestSkip,
    IntervalMetric,
    TargetSkip,
    model_names,
)
from sleeper_manager.domain.projection import ProjectionSnapshot
from sleeper_manager.domain.scoring import BoxScoreLine, ScoringPolicy, calculate_fantasy_points
from sleeper_manager.integrations.nba.historical_features import (
    HistoricalFeatureDataset,
    HistoricalFeatureRow,
)


def run_backtest(
    dataset: HistoricalFeatureDataset,
    *,
    scoring_policy: ScoringPolicy,
    models: Iterable[BacktestModel],
    config: BacktestConfig | None = None,
    reference_model: str | None = None,
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

    for target in targets:
        point_in_time_dataset = _point_in_time_dataset(
            dataset,
            chronological_rows,
            game_starts,
            target,
        )
        actual_score = calculate_fantasy_points(target.target_box_score, scoring_policy)
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
                    expected_value=snapshot.distribution.expected_value,
                    percentiles=snapshot.distribution.percentiles,
                    exceedance_probabilities=tuple(
                        (
                            threshold,
                            snapshot.distribution.probability_of_exceeding(threshold),
                        )
                        for threshold in config.thresholds
                    ),
                )
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
    reference_observations = {
        (observation.player_id, observation.game_id): observation
        for observation in reference.observations
    }
    candidate_observations = {
        (observation.player_id, observation.game_id): observation
        for observation in candidate.observations
    }
    keys = tuple(sorted(reference_observations.keys() & candidate_observations.keys()))
    common_reference = tuple(reference_observations[key] for key in keys)
    common_candidate = tuple(candidate_observations[key] for key in keys)
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
