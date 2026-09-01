from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sleeper_manager.backtesting.backtest_dataset import (
    PreparedBacktestDataset,
    PreparedBacktestWindow,
    prepare_backtest_dataset,
)
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
    model_names,
)
from sleeper_manager.backtesting.progress import ProgressCounter
from sleeper_manager.domain.projection import ProjectionSnapshot
from sleeper_manager.domain.scoring import ScoringPolicy, calculate_fantasy_points
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
    progress: ProgressCounter | None = None,
) -> BacktestReport:
    """Evaluate every model against a shared point-in-time target sequence.

    ``progress`` advances only after all models have either projected or skipped one target.
    """
    resolved_config = config or BacktestConfig()
    model_records, reference = prepare_backtest_models(models, reference_model)
    prepared = prepare_backtest_dataset(dataset)
    return run_prepared_backtest(
        prepared,
        window=prepared.prepare_window(resolved_config),
        scoring_policy=scoring_policy,
        models=model_records,
        reference_model=reference,
        cohort_config=cohort_config,
        progress=progress,
    )


def prepare_backtest_models(
    models: Iterable[BacktestModel], reference_model: str | None
) -> tuple[tuple[BacktestModel, ...], str]:
    """Materialize a valid ordered model suite and resolve its reference model."""
    model_records = tuple(models)
    if not model_records:
        raise BacktestError("At least one backtest model is required")
    names = model_names(model_records)
    reference = reference_model or names[0]
    if reference not in names:
        raise BacktestError(f"Unknown reference model: {reference!r}")
    return model_records, reference


def run_prepared_backtest(
    prepared: PreparedBacktestDataset,
    *,
    window: PreparedBacktestWindow,
    scoring_policy: ScoringPolicy,
    models: tuple[BacktestModel, ...],
    reference_model: str,
    cohort_config: CohortConfig | None = None,
    progress: ProgressCounter | None = None,
) -> BacktestReport:
    """Execute one frozen target window without repeating dataset preparation."""
    dataset = prepared.dataset
    chronological_rows = prepared.rows
    config = window.config
    targets = window.targets
    target_skips = window.target_skips
    observations_by_model: dict[str, list[BacktestObservation]] = {
        model.name: [] for model in models
    }
    skips_by_model: dict[str, list[BacktestSkip]] = {model.name: [] for model in models}
    batch_player_ids: dict[datetime, list[str]] = {}
    for target in targets:
        batch_player_ids.setdefault(target.game_start, []).append(target.player_id)
    ranker = IndependentCohortRanker(cohort_config)
    current_batch_game_start: datetime | None = None
    current_rank_map: dict[str, int] = {}
    target_cohorts: dict[tuple[str, str], CohortAssignment] = {}

    if progress is not None and not targets:
        progress.advance(0, 0)

    for target_position, target in enumerate(targets, start=1):
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
        point_in_time_dataset = prepared.point_in_time_dataset(target)
        actual_score = calculate_fantasy_points(target.target_box_score, scoring_policy)
        realized_participation = target.target_did_play
        realized_minutes = target.target_minutes if target.target_did_play else None
        realized_rate = (
            actual_score / target.target_minutes
            if target.target_did_play and target.target_minutes
            else None
        )
        for model in models:
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
        if progress is not None:
            progress.advance(target_position, len(targets))

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
        for model in models
    )
    comparisons = tuple(
        _compare_results(
            results=results,
            reference_model=reference_model,
            candidate_model=model.name,
            config=config,
        )
        for model in models
        if model.name != reference_model
    )
    return BacktestReport(
        dataset_version=dataset.dataset_version,
        scoring_policy_version=scoring_policy.version,
        config_version=config.version,
        target_count=len(targets),
        target_skips=tuple(target_skips),
        reference_model=reference_model,
        model_results=results,
        comparisons=comparisons,
    )


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
