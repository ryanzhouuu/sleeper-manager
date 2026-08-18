from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sleeper_manager.backtesting.models import BacktestReport


@dataclass(frozen=True, slots=True)
class ChronologicalFold:
    """One time-ordered train and evaluation boundary."""

    name: str
    season_start: int
    phase: str
    start_at: datetime
    end_at: datetime
    holdout: bool = False


@dataclass(frozen=True, slots=True)
class FoldResult:
    fold: ChronologicalFold
    report: BacktestReport


@dataclass(frozen=True, slots=True)
class BootstrapInterval:
    metric: str
    candidate_model: str
    reference_model: str
    sample_count: int
    seed: int
    lower: float | None
    upper: float | None


@dataclass(frozen=True, slots=True)
class SegmentComparison:
    segment: str
    value: str
    candidate_model: str
    reference_model: str
    player_game_count: int
    game_count: int
    conclusive: bool
    reference_mae: float | None
    candidate_mae: float | None
    mae_delta: float | None


@dataclass(frozen=True, slots=True)
class PromotionGateConfig:
    min_mae_points: float = 0.25
    min_mae_fraction: float = 0.01
    max_rmse_points: float = 0.25
    max_rmse_fraction: float = 0.01
    max_segment_mae_points: float = 0.5
    max_segment_mae_fraction: float = 0.02
    interval_coverage_tolerance: float = 0.05
    max_interval_width_fraction: float = 0.05
    max_brier_delta: float = 0.005
    min_coverage_ratio: float = 0.95
    intervals: tuple[tuple[int, int], ...] = ((10, 90), (25, 75))
    thresholds: tuple[float, ...] = (20.0, 30.0, 40.0, 50.0, 60.0)


@dataclass(frozen=True, slots=True)
class GateResult:
    name: str
    passed: bool
    evidence: str


@dataclass(frozen=True, slots=True)
class ComponentGateConfig:
    max_regression_fraction: float = 0.02
    min_calibration_bin_size: int = 100
    calibration_tolerance: float = 0.05


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    candidate_model: str
    recommendation: str
    promotable: bool
    gates: tuple[GateResult, ...]


@dataclass(frozen=True, slots=True)
class DevelopmentDecision:
    candidate_model: str
    selected: bool
    promotion_eligible: bool
    improved_folds: int
    fold_count: int
    bootstrap: BootstrapInterval
    evidence: str


@dataclass(frozen=True, slots=True)
class _AggregateMetrics:
    sample_count: int
    mae: float | None
    rmse: float | None
    interval_coverage: tuple[tuple[tuple[int, int], float | None], ...]
    interval_width: tuple[tuple[tuple[int, int], float | None], ...]
    brier_scores: tuple[tuple[float, float | None], ...]
