from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Protocol

from sleeper_manager.domain.projection import ProjectionDistribution, ProjectionSnapshot
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.integrations.nba.historical_features import HistoricalFeatureDataset


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
class BacktestObservation:
    player_id: str
    game_id: str
    game_start: datetime
    available_as_of: datetime
    actual_score: float
    distribution: ProjectionDistribution

    @property
    def absolute_error(self) -> float:
        return abs(self.distribution.expected_value - self.actual_score)

    @property
    def squared_error(self) -> float:
        error = self.distribution.expected_value - self.actual_score
        return error * error


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
