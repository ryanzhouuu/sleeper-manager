from __future__ import annotations

from collections.abc import Iterable, Sequence
from math import exp, log

from sleeper_manager.domain.nba import AvailabilityStatus
from sleeper_manager.domain.projection import ProjectionAdjustmentKind, ProjectionFallback
from sleeper_manager.domain.scoring import ScoringPolicy, calculate_fantasy_points
from sleeper_manager.integrations.nba.historical_feature_models import (
    HistoricalFeatureRow,
    OpponentStatsFallback,
)
from sleeper_manager.projections.opportunity_types import (
    OpportunityModelConfig,
    OpportunityModelError,
    _Estimate,
)


def _weighted_rows(
    target: HistoricalFeatureRow, rows: Iterable[HistoricalFeatureRow], half_life: float
) -> tuple[tuple[HistoricalFeatureRow, float], ...]:
    return tuple(
        (
            row,
            exp(
                -log(2)
                * max((target.game_start - row.game_start).total_seconds(), 0)
                / 86400
                / half_life
            ),
        )
        for row in rows
        if row.game_start < target.game_start
    )


def _played_observations(
    target: HistoricalFeatureRow,
    rows: Iterable[HistoricalFeatureRow],
    half_life: float,
) -> tuple[tuple[HistoricalFeatureRow, float], ...]:
    return tuple(
        (row, weight)
        for row, weight in _weighted_rows(target, rows, half_life)
        if row.target_did_play and row.target_minutes is not None and row.target_minutes > 0
    )


def _unshrunk_rate(player_rate: float, effective_minutes: float) -> _Estimate:
    return _Estimate(
        value=player_rate,
        baseline=player_rate,
        adjustment=0.0,
        kind=ProjectionAdjustmentKind.ADDITIVE,
        effective_sample=effective_minutes,
        fallback=ProjectionFallback.MISSING,
        message=(
            "No league production prior was available; retained the player-only "
            "rate without shrinkage."
        ),
    )


def _last(values: Sequence[float]) -> float:
    return values[-1] if values else 0.0


def _weighted_mean(values: Iterable[tuple[float, float]]) -> float:
    records = tuple(values)
    total_weight = sum(weight for _, weight in records)
    if not records or total_weight <= 0:
        raise OpportunityModelError("Cannot calculate a weighted mean without observations")
    return sum(value * weight for value, weight in records) / total_weight


def _production_rates(
    observations: Sequence[tuple[HistoricalFeatureRow, float]],
    scoring_policy: ScoringPolicy,
) -> tuple[tuple[float, float], ...]:
    return tuple(
        (
            calculate_fantasy_points(row.target_box_score, scoring_policy)
            / (row.target_minutes or 1.0),
            weight * (row.target_minutes or 1.0),
        )
        for row, weight in observations
    )


def _conditional_observations(
    target: HistoricalFeatureRow,
    rows: Sequence[HistoricalFeatureRow],
    policy: ScoringPolicy,
    target_mean: float,
    config: OpportunityModelConfig,
) -> tuple[tuple[float, float], ...]:
    played = tuple(
        (row, weight)
        for row, weight in _weighted_rows(target, rows, config.recency_half_life_days)
        if row.target_did_play and row.target_minutes is not None and row.target_minutes > 0
    )
    if not played:
        return ()
    values = tuple(
        (calculate_fantasy_points(row.target_box_score, policy), weight) for row, weight in played
    )
    raw_mean = _weighted_mean(values)
    total_weight = sum(weight for _, weight in values)
    return tuple(
        (value + target_mean - raw_mean, weight / total_weight) for value, weight in values
    )


def _status_probability(status: AvailabilityStatus) -> float:
    return {
        AvailabilityStatus.AVAILABLE: 0.98,
        AvailabilityStatus.PROBABLE: 0.92,
        AvailabilityStatus.QUESTIONABLE: 0.68,
        AvailabilityStatus.DOUBTFUL: 0.35,
        AvailabilityStatus.OUT: 0.02,
        AvailabilityStatus.UNKNOWN: 0.75,
    }[status]


def _defense_fallback(fallback: OpponentStatsFallback) -> ProjectionFallback:
    return {
        OpponentStatsFallback.OBSERVED: ProjectionFallback.OBSERVED,
        OpponentStatsFallback.SHRUNK: ProjectionFallback.SHRUNK,
        OpponentStatsFallback.LEAGUE_AVERAGE: ProjectionFallback.LEAGUE_AVERAGE,
        OpponentStatsFallback.MISSING: ProjectionFallback.MISSING,
    }[fallback]
