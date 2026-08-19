from __future__ import annotations

from bisect import bisect_left
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import replace
from datetime import datetime
from itertools import islice
from typing import overload

from sleeper_manager.backtesting.backtest_metrics import (
    _cohort_diagnostics,
    _compare_results,
    _component_control,
    _error_reason,
    _metrics,
    _predicted_component,
    _validate_cohort_invariants,
)
from sleeper_manager.backtesting.cohorts import (
    CohortConfig,
    IndependentCohortRanker,
    cohort_for_rank,
)
from sleeper_manager.backtesting.models import (
    BacktestConfig,
    BacktestError,
    BacktestModel,
    BacktestModelResult,
    BacktestObservation,
    BacktestReport,
    BacktestSkip,
    CohortAssignment,
    TargetSkip,
    model_names,
)
from sleeper_manager.domain.nba_season import nba_season_start_year
from sleeper_manager.domain.projection import ProjectionSnapshot
from sleeper_manager.domain.scoring import BoxScoreLine, ScoringPolicy, calculate_fantasy_points
from sleeper_manager.integrations.nba.historical_feature_models import (
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
        player_season = row.player_id, nba_season_start_year(row.game_start)
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
