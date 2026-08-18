from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sleeper_manager.backtesting.backtest_execution import run_backtest
from sleeper_manager.backtesting.backtest_metrics import cohort_matches, compare_observation_sets
from sleeper_manager.backtesting.models import (
    BacktestComparison,
    BacktestConfig,
    BacktestModel,
    BacktestObservation,
)
from sleeper_manager.backtesting.validation.models import ChronologicalFold, FoldResult
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.integrations.nba.historical_feature_models import HistoricalFeatureDataset


def regular_season_folds(
    *,
    development_seasons: tuple[int, ...] = (2022, 2023, 2024),
    holdout_season: int = 2025,
) -> tuple[ChronologicalFold, ...]:
    folds: list[ChronologicalFold] = []
    for season_start in development_seasons + (holdout_season,):
        holdout = season_start == holdout_season
        windows = (
            (
                "early",
                datetime(season_start, 10, 1, tzinfo=UTC),
                datetime(season_start + 1, 1, 1, tzinfo=UTC),
            ),
            (
                "middle",
                datetime(season_start + 1, 1, 1, tzinfo=UTC),
                datetime(season_start + 1, 3, 1, tzinfo=UTC),
            ),
            (
                "late",
                datetime(season_start + 1, 3, 1, tzinfo=UTC),
                datetime(season_start + 1, 5, 1, tzinfo=UTC),
            ),
        )
        for phase, start_at, next_start in windows:
            folds.append(
                ChronologicalFold(
                    name=f"{season_start}-{str(season_start + 1)[-2:]}-{phase}",
                    season_start=season_start,
                    phase=phase,
                    start_at=start_at,
                    end_at=next_start - timedelta(microseconds=1),
                    holdout=holdout,
                )
            )
    return tuple(folds)


def run_validation_folds(
    dataset: HistoricalFeatureDataset,
    *,
    scoring_policy: ScoringPolicy,
    models: Iterable[BacktestModel],
    folds: Iterable[ChronologicalFold],
    config: BacktestConfig | None = None,
    reference_model: str | None = None,
) -> tuple[FoldResult, ...]:
    base = config or BacktestConfig(
        thresholds=(20.0, 30.0, 40.0, 50.0, 60.0),
        intervals=((10, 90), (25, 75)),
    )
    records = tuple(models)
    return tuple(
        FoldResult(
            fold,
            run_backtest(
                dataset,
                scoring_policy=scoring_policy,
                models=records,
                config=replace(base, start_at=fold.start_at, end_at=fold.end_at),
                reference_model=reference_model,
            ),
        )
        for fold in folds
    )


def cohort_comparison_across_folds(
    fold_results: Iterable[FoldResult],
    *,
    reference_model: str,
    candidate_model: str,
    cohort: str,
    config: BacktestConfig,
) -> BacktestComparison:
    """Compare two models on their common successful observations within one cohort, pooled
    across every given fold. Folds are time-disjoint, so a (player_id, game_id) key can appear
    in at most one fold -- pooling never double-counts or collides across folds."""
    reference_observations: list[BacktestObservation] = []
    candidate_observations: list[BacktestObservation] = []
    for fold_result in fold_results:
        reference_result = fold_result.report.result_for(reference_model)
        candidate_result = fold_result.report.result_for(candidate_model)
        reference_observations.extend(
            observation
            for observation in reference_result.observations
            if cohort_matches(observation.cohort, cohort)
        )
        candidate_observations.extend(
            observation
            for observation in candidate_result.observations
            if cohort_matches(observation.cohort, cohort)
        )
    return compare_observation_sets(
        reference_model=reference_model,
        candidate_model=candidate_model,
        reference_observations=tuple(reference_observations),
        candidate_observations=tuple(candidate_observations),
        config=config,
    )
