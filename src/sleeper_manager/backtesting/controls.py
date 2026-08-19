from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Literal

from sleeper_manager.backtesting.models import BacktestError, ProjectionModel
from sleeper_manager.domain.nba_season import nba_season_start_year
from sleeper_manager.domain.projection import (
    ProjectionDistribution,
    ProjectionReason,
    ProjectionSnapshot,
)
from sleeper_manager.domain.scoring import ScoringPolicy, calculate_fantasy_points
from sleeper_manager.integrations.nba.historical_feature_models import (
    HistoricalFeatureDataset,
    HistoricalFeatureRow,
)

NaiveProjectionKind = Literal["last_game", "season_average"]


class NaiveProjectionBaseline:
    def __init__(
        self,
        kind: NaiveProjectionKind,
        *,
        percentiles: tuple[int, ...] = (10, 25, 50, 75, 90),
    ) -> None:
        if kind not in ("last_game", "season_average"):
            raise BacktestError(f"Unknown naive projection kind: {kind!r}")
        if not percentiles or tuple(sorted(set(percentiles))) != percentiles:
            raise BacktestError("Naive projection percentiles must be unique and ordered")
        if any(percentile < 0 or percentile > 100 for percentile in percentiles):
            raise BacktestError("Naive projection percentiles must be between zero and 100")
        self.kind = kind
        self.percentiles = percentiles
        self._indexes: dict[str, _NaiveHistoryIndex] = {}

    @property
    def model_version(self) -> str:
        return f"naive-{self.kind.replace('_', '-')}-v1"

    def project(
        self,
        dataset: HistoricalFeatureDataset,
        *,
        player_id: str,
        game_id: str,
        scoring_policy: ScoringPolicy,
        exceed_score: float | None = None,
    ) -> ProjectionSnapshot:
        target = _find_target(dataset.rows, player_id=player_id, game_id=game_id)
        index = self._indexes.setdefault(dataset.dataset_version, _NaiveHistoryIndex())
        point_in_time_count = getattr(dataset.rows, "prior_count", None)
        target_is_last = bool(dataset.rows) and dataset.rows[-1] == target
        prior_count = (
            point_in_time_count
            if isinstance(point_in_time_count, int)
            else len(dataset.rows) - int(target_is_last)
        )
        index.extend(dataset.rows, prior_count)
        prior_rows = index.rows_before(
            player_id,
            nba_season_start_year(target.game_start),
            target.game_start,
        )
        if not prior_rows:
            raise BacktestError(
                f"No prior same-season observations for {player_id!r} before {game_id!r}"
            )
        prior_scores = tuple(
            calculate_fantasy_points(row.target_box_score, scoring_policy) for row in prior_rows
        )
        observations: tuple[tuple[float, float], ...]
        if self.kind == "last_game":
            latest = max(prior_rows, key=lambda row: (row.game_start, row.game_id))
            observations = (
                (calculate_fantasy_points(latest.target_box_score, scoring_policy), 1.0),
            )
            message = f"Used the latest prior same-season score of {observations[0][0]:.2f}."
        else:
            observations = tuple((score, 1.0) for score in prior_scores)
            message = (
                f"Used an equal-weighted average of {len(prior_scores)} prior same-season scores."
            )
        distribution = ProjectionDistribution.from_weighted_observations(
            observations, percentiles=self.percentiles
        )
        if exceed_score is not None:
            distribution = distribution.for_exceedance_score(exceed_score)
        return ProjectionSnapshot(
            player_id=player_id,
            game_id=game_id,
            available_as_of=target.available_as_of,
            model_version=self.model_version,
            input_version=_input_version(dataset, target, prior_rows, scoring_policy),
            scoring_policy_version=scoring_policy.version,
            distribution=distribution,
            reasons=(ProjectionReason("naive_control", message),),
        )


class CalibratedProjectionModel:
    """Wrap a projector with a point-in-time empirical residual distribution."""

    def __init__(
        self,
        projector: ProjectionModel,
        *,
        min_samples: int = 64,
        max_samples: int = 4096,
        refresh_interval: int = 256,
    ) -> None:
        if min_samples <= 0:
            raise BacktestError("Calibration minimum samples must be positive")
        if max_samples < min_samples:
            raise BacktestError("Calibration sample capacity must cover minimum samples")
        if refresh_interval <= 0:
            raise BacktestError("Calibration refresh interval must be positive")
        self.projector = projector
        self.min_samples = min_samples
        self.max_samples = max_samples
        self.refresh_interval = refresh_interval
        self._residuals: deque[float] = deque(maxlen=max_samples)
        self._pending: dict[tuple[str, str], _PendingCalibration] = {}
        self._total_residuals = 0
        self._last_refresh_count = 0
        self._sorted_residuals: tuple[float, ...] = ()
        self._residual_mean = 0.0
        self._last_game_start: datetime | None = None
        self._dataset_version: str | None = None
        self._indexed_prior_count = 0
        self._rows_by_key: dict[tuple[str, str], HistoricalFeatureRow] = {}

    @property
    def model_version(self) -> str:
        wrapped_version = getattr(self.projector, "model_version", type(self.projector).__name__)
        return (
            f"calibrated-{wrapped_version}-v1-{self.min_samples}-"
            f"{self.max_samples}-{self.refresh_interval}"
        )

    def project(
        self,
        dataset: HistoricalFeatureDataset,
        *,
        player_id: str,
        game_id: str,
        scoring_policy: ScoringPolicy,
        exceed_score: float | None = None,
    ) -> ProjectionSnapshot:
        target = _find_target(dataset.rows, player_id=player_id, game_id=game_id)
        if self._dataset_version is None:
            self._dataset_version = dataset.dataset_version
        elif self._dataset_version != dataset.dataset_version:
            raise BacktestError("Calibrated projections cannot mix dataset versions")
        if self._last_game_start is not None and target.game_start < self._last_game_start:
            raise BacktestError("Calibrated projections must be evaluated chronologically")
        self._resolve_pending(dataset, target=target, scoring_policy=scoring_policy)
        base = self.projector.project(
            dataset,
            player_id=player_id,
            game_id=game_id,
            scoring_policy=scoring_policy,
            exceed_score=None,
        )
        key = player_id, game_id
        if key in self._pending:
            raise BacktestError(f"Duplicate pending calibrated projection for {key!r}")
        self._pending[key] = _PendingCalibration(
            target.game_start, base.distribution.expected_value
        )
        self._last_game_start = target.game_start
        distribution = self._calibrated_distribution(base.distribution)
        if exceed_score is not None:
            distribution = distribution.for_exceedance_score(exceed_score)
        calibration_reason = ProjectionReason(
            "residual_calibration",
            (
                f"Used {len(self._sorted_residuals)} prior residuals for empirical interval "
                f"calibration; minimum is {self.min_samples}."
            ),
            applied=len(self._sorted_residuals) >= self.min_samples,
        )
        return replace(
            base,
            model_version=self.model_version,
            input_version=_calibration_input_version(
                base,
                sample_count=len(self._sorted_residuals),
                residual_mean=self._residual_mean,
            ),
            distribution=distribution,
            reasons=base.reasons + (calibration_reason,),
        )

    def _resolve_pending(
        self,
        dataset: HistoricalFeatureDataset,
        *,
        target: HistoricalFeatureRow,
        scoring_policy: ScoringPolicy,
    ) -> None:
        point_in_time_count = getattr(dataset.rows, "prior_count", None)
        if isinstance(point_in_time_count, int):
            for row in dataset.rows[self._indexed_prior_count : point_in_time_count]:
                self._rows_by_key.setdefault((row.player_id, row.game_id), row)
            self._indexed_prior_count = max(self._indexed_prior_count, point_in_time_count)
        else:
            for row in dataset.rows:
                if row.game_start < target.game_start:
                    self._rows_by_key.setdefault((row.player_id, row.game_id), row)
        resolved: list[tuple[str, str]] = []
        for key, pending in self._pending.items():
            if pending.game_start >= target.game_start:
                continue
            prior_row = self._rows_by_key.get(key)
            if prior_row is not None:
                actual = calculate_fantasy_points(prior_row.target_box_score, scoring_policy)
                self._add_residual(actual - pending.expected_value)
            resolved.append(key)
        for key in resolved:
            del self._pending[key]

    def _add_residual(self, residual: float) -> None:
        self._residuals.append(residual)
        self._total_residuals += 1
        if (
            self._total_residuals >= self.min_samples
            and self._total_residuals - self._last_refresh_count >= self.refresh_interval
        ):
            self._sorted_residuals = tuple(sorted(self._residuals))
            self._residual_mean = sum(self._residuals) / len(self._residuals)
            self._last_refresh_count = self._total_residuals

    def _calibrated_distribution(
        self,
        base: ProjectionDistribution,
    ) -> ProjectionDistribution:
        if len(self._sorted_residuals) < self.min_samples:
            return base
        residual_quantiles = tuple(
            _quantile(self._sorted_residuals, percentile / 100) for percentile in range(101)
        )
        observations = tuple(
            (base.expected_value + residual - self._residual_mean, 1.0)
            for residual in residual_quantiles
        )
        calibrated = ProjectionDistribution.from_weighted_observations(
            observations,
            percentiles=tuple(percentile for percentile, _ in base.percentiles),
        )
        return replace(calibrated, expected_value=base.expected_value)


@dataclass(frozen=True, slots=True)
class _PendingCalibration:
    game_start: datetime
    expected_value: float


@dataclass(slots=True)
class _NaiveHistoryIndex:
    processed_count: int = 0
    players: dict[tuple[str, int], list[HistoricalFeatureRow]] = field(default_factory=dict)

    def extend(self, rows: Sequence[HistoricalFeatureRow], prior_count: int) -> None:
        if prior_count <= self.processed_count:
            return
        for row in rows[self.processed_count : prior_count]:
            self.players.setdefault(
                (row.player_id, nba_season_start_year(row.game_start)), []
            ).append(row)
        self.processed_count = prior_count

    def rows_before(
        self,
        player_id: str,
        season: int,
        game_start: datetime,
    ) -> tuple[HistoricalFeatureRow, ...]:
        return tuple(
            row for row in self.players.get((player_id, season), ()) if row.game_start < game_start
        )


def _find_target(
    rows: Sequence[HistoricalFeatureRow], *, player_id: str, game_id: str
) -> HistoricalFeatureRow:
    if rows:
        latest = rows[-1]
        if latest.player_id == player_id and latest.game_id == game_id:
            return latest
    matches = tuple(row for row in rows if row.player_id == player_id and row.game_id == game_id)
    if len(matches) != 1:
        raise BacktestError(f"Expected one feature row for player/game, found {len(matches)}")
    return matches[0]


def _input_version(
    dataset: HistoricalFeatureDataset,
    target: HistoricalFeatureRow,
    prior_rows: Iterable[HistoricalFeatureRow],
    policy: ScoringPolicy,
) -> str:
    prior_inputs = [
        {
            "game_id": row.game_id,
            "game_start": row.game_start.isoformat(),
            "score": calculate_fantasy_points(row.target_box_score, policy),
        }
        for row in sorted(prior_rows, key=lambda row: (row.game_start, row.game_id))
    ]
    payload = {
        "dataset_version": dataset.dataset_version,
        "feature_schema_version": dataset.feature_schema_version,
        "player_id": target.player_id,
        "game_id": target.game_id,
        "available_as_of": target.available_as_of.isoformat(),
        "scoring_policy_version": policy.version,
        "prior_inputs": prior_inputs,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"naive-input-v1-{hashlib.sha256(encoded).hexdigest()[:12]}"


def _calibration_input_version(
    base: ProjectionSnapshot,
    *,
    sample_count: int,
    residual_mean: float,
) -> str:
    payload = {
        "base_input_version": base.input_version,
        "sample_count": sample_count,
        "residual_mean": round(residual_mean, 12),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"calibration-input-v1-{hashlib.sha256(encoded).hexdigest()[:12]}"


def _quantile(values: tuple[float, ...], fraction: float) -> float:
    if not values:
        raise BacktestError("Calibration quantiles require residual observations")
    position = fraction * (len(values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    weight = position - lower
    return values[lower] * (1 - weight) + values[upper] * weight
