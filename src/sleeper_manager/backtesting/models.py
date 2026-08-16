from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Protocol

from sleeper_manager.domain.projection import ProjectionComponent, ProjectionSnapshot
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.integrations.nba.historical_features import HistoricalFeatureDataset

_EXCLUSIVE_TIERS = frozenset({"top_108", "ranks_109_180", "below_180"})


class BacktestError(ValueError):
    """Raised when a backtest configuration or model contract is invalid."""


class ProjectionModel(Protocol):
    def project(
        self,
        dataset: HistoricalFeatureDataset,
        *,
        player_id: str,
        game_id: str,
        scoring_policy: ScoringPolicy,
        exceed_score: float | None = None,
    ) -> ProjectionSnapshot: ...


@dataclass(frozen=True, slots=True)
class BacktestModel:
    name: str
    projector: ProjectionModel

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise BacktestError("Backtest model names must not be empty")


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    thresholds: tuple[float, ...] = (10.0, 20.0, 30.0, 40.0)
    intervals: tuple[tuple[int, int], ...] = ((10, 90), (25, 75))
    min_prior_games: int = 1
    start_at: datetime | None = None
    end_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.min_prior_games < 0:
            raise BacktestError("Minimum prior games must be non-negative")
        if tuple(sorted(set(self.thresholds))) != self.thresholds:
            raise BacktestError("Backtest thresholds must be unique and ordered")
        if not all(isfinite(threshold) for threshold in self.thresholds):
            raise BacktestError("Backtest thresholds must be finite")
        if tuple(sorted(set(self.intervals))) != self.intervals:
            raise BacktestError("Backtest intervals must be unique and ordered")
        for lower, upper in self.intervals:
            if not 0 <= lower < upper <= 100:
                raise BacktestError("Backtest intervals must be ordered percentiles from 0 to 100")
        for value, field in ((self.start_at, "start_at"), (self.end_at, "end_at")):
            if value is not None and value.tzinfo is None:
                raise BacktestError(f"Backtest {field} must be timezone-aware")
        if self.start_at is not None and self.end_at is not None and self.start_at > self.end_at:
            raise BacktestError("Backtest start_at must not be after end_at")

    @property
    def version(self) -> str:
        payload = {
            "thresholds": self.thresholds,
            "intervals": self.intervals,
            "min_prior_games": self.min_prior_games,
            "start_at": self.start_at.isoformat() if self.start_at else None,
            "end_at": self.end_at.isoformat() if self.end_at else None,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return f"backtest-config-v1-{hashlib.sha256(encoded).hexdigest()[:12]}"


@dataclass(frozen=True, slots=True)
class CohortAssignment:
    """The frozen, model-independent cohort membership for one target player-game."""

    rank: int
    tier: str
    top_180: bool

    def __post_init__(self) -> None:
        if self.rank <= 0:
            raise BacktestError("Cohort rank must be positive")
        if self.tier not in _EXCLUSIVE_TIERS:
            raise BacktestError(f"Unknown cohort tier: {self.tier!r}")
        if self.top_180 != (self.rank <= 180):
            raise BacktestError("Cohort top_180 flag must match rank <= 180")


@dataclass(frozen=True, slots=True)
class PredictedComponentDiagnostic:
    """A model's own predicted participation, conditional minutes, and conditional rate.

    Any component a model does not expose is ``None`` with an explicit reason recorded in
    ``missing_reasons`` -- never imputed.
    """

    participation: float | None
    minutes: float | None
    rate: float | None
    missing_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BacktestObservation:
    player_id: str
    game_id: str
    game_start: datetime
    available_as_of: datetime
    actual_score: float
    model_version: str
    input_version: str
    expected_value: float
    percentiles: tuple[tuple[int, float], ...]
    exceedance_probabilities: tuple[tuple[float, float], ...]
    cohort: CohortAssignment
    realized_participation: bool = False
    realized_minutes: float | None = None
    realized_rate: float | None = None
    components: tuple[ProjectionComponent, ...] = ()
    predicted_component: PredictedComponentDiagnostic = PredictedComponentDiagnostic(
        None, None, None
    )

    @property
    def absolute_error(self) -> float:
        return abs(self.expected_value - self.actual_score)

    @property
    def squared_error(self) -> float:
        error = self.expected_value - self.actual_score
        return error * error

    def probability_of_exceeding(self, threshold: float) -> float:
        for configured_threshold, probability in self.exceedance_probabilities:
            if configured_threshold == threshold:
                return probability
        raise BacktestError(f"No exceedance probability recorded for threshold {threshold}")


@dataclass(frozen=True, slots=True)
class TargetSkip:
    player_id: str
    game_id: str
    game_start: datetime
    reason: str


@dataclass(frozen=True, slots=True)
class BacktestSkip:
    model_name: str
    player_id: str
    game_id: str
    game_start: datetime
    reason: str
    cohort: CohortAssignment


@dataclass(frozen=True, slots=True)
class IntervalMetric:
    lower_percentile: int
    upper_percentile: int
    nominal_coverage: float
    observed_coverage: float | None
    mean_width: float | None


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    target_count: int
    sample_count: int
    coverage: float
    mae: float | None
    rmse: float | None
    median_absolute_error: float | None
    intervals: tuple[IntervalMetric, ...]
    brier_scores: tuple[tuple[float, float | None], ...]


@dataclass(frozen=True, slots=True)
class BacktestModelResult:
    model: BacktestModel
    observations: tuple[BacktestObservation, ...]
    skips: tuple[BacktestSkip, ...]
    metrics: BacktestMetrics


@dataclass(frozen=True, slots=True)
class BacktestComparison:
    reference_model: str
    candidate_model: str
    common_sample_count: int
    mae_delta: float | None
    rmse_delta: float | None
    median_absolute_error_delta: float | None
    brier_score_deltas: tuple[tuple[float, float | None], ...]


@dataclass(frozen=True, slots=True)
class BacktestReport:
    dataset_version: str
    scoring_policy_version: str
    config_version: str
    target_count: int
    target_skips: tuple[TargetSkip, ...]
    reference_model: str
    model_results: tuple[BacktestModelResult, ...]
    comparisons: tuple[BacktestComparison, ...]

    def result_for(self, model_name: str) -> BacktestModelResult:
        for result in self.model_results:
            if result.model.name == model_name:
                return result
        raise KeyError(model_name)


def model_names(models: Iterable[BacktestModel]) -> tuple[str, ...]:
    names = tuple(model.name for model in models)
    if len(set(names)) != len(names):
        raise BacktestError("Backtest model names must be unique")
    return names
