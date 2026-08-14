from __future__ import annotations

import hashlib
import json
from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from math import exp, isfinite, log

from sleeper_manager.backtesting.models import ProjectionModel
from sleeper_manager.domain.projection import (
    ProjectionDistribution,
    ProjectionReason,
    ProjectionSnapshot,
)
from sleeper_manager.domain.scoring import ScoringPolicy, calculate_fantasy_points
from sleeper_manager.integrations.nba.historical_features import (
    HistoricalFeatureDataset,
    HistoricalFeatureRow,
)
from sleeper_manager.projections.direct_baseline import DirectFantasyPointBaseline


class ResidualCandidateError(ValueError):
    pass


class ResidualFeature(StrEnum):
    OPPONENT_IDENTITY = "opponent_identity"
    OPPONENT_STRENGTH = "opponent_strength"
    REST = "rest"
    TRAVEL = "travel"
    INJURY = "injury"


class CachingProjectionModel:
    def __init__(self, projector: ProjectionModel, *, max_entries: int | None = None) -> None:
        if max_entries is not None and max_entries <= 0:
            raise ResidualCandidateError("Projection cache size must be positive")
        self.projector = projector
        self.max_entries = max_entries
        self._snapshots: dict[
            tuple[str, str, str, str, float | None],
            ProjectionSnapshot,
        ] = {}

    def project(
        self,
        dataset: HistoricalFeatureDataset,
        *,
        player_id: str,
        game_id: str,
        scoring_policy: ScoringPolicy,
        exceed_score: float | None = None,
    ) -> ProjectionSnapshot:
        key = (
            dataset.dataset_version,
            scoring_policy.version,
            player_id,
            game_id,
            exceed_score,
        )
        if key not in self._snapshots:
            if self.max_entries is not None and len(self._snapshots) >= self.max_entries:
                del self._snapshots[next(iter(self._snapshots))]
            self._snapshots[key] = self.projector.project(
                dataset,
                player_id=player_id,
                game_id=game_id,
                scoring_policy=scoring_policy,
                exceed_score=exceed_score,
            )
        return self._snapshots[key]


@dataclass(frozen=True, slots=True)
class ResidualCandidateConfig:
    features: tuple[ResidualFeature, ...]
    recency_half_life_days: float = 60.0
    shrinkage_games: float = 20.0
    max_adjustment: float = 10.0
    lookback_days: int = 365

    def __post_init__(self) -> None:
        if not self.features or len(set(self.features)) != len(self.features):
            raise ResidualCandidateError("Residual candidate features must be unique and nonempty")
        if not isfinite(self.recency_half_life_days) or self.recency_half_life_days <= 0:
            raise ResidualCandidateError("Residual half-life must be finite and positive")
        if not isfinite(self.shrinkage_games) or self.shrinkage_games < 0:
            raise ResidualCandidateError("Residual shrinkage must be finite and non-negative")
        if not isfinite(self.max_adjustment) or self.max_adjustment <= 0:
            raise ResidualCandidateError("Maximum residual adjustment must be finite and positive")
        if self.lookback_days <= 0:
            raise ResidualCandidateError("Residual lookback must be positive")

    @property
    def model_version(self) -> str:
        payload = {
            "features": self.features,
            "recency_half_life_days": self.recency_half_life_days,
            "shrinkage_games": self.shrinkage_games,
            "max_adjustment": self.max_adjustment,
            "lookback_days": self.lookback_days,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return f"residual-candidate-v1-{hashlib.sha256(encoded).hexdigest()[:12]}"


class ResidualHistory:
    def __init__(self, reference: ProjectionModel | None = None) -> None:
        self.reference = reference or DirectFantasyPointBaseline()
        self._residuals: dict[tuple[str, str, str, str], float | None] = {}
        self._groups: dict[
            tuple[str, str, ResidualFeature],
            _GroupedResiduals,
        ] = {}

    def residual(
        self,
        dataset: HistoricalFeatureDataset,
        row: HistoricalFeatureRow,
        scoring_policy: ScoringPolicy,
    ) -> float | None:
        key = (dataset.dataset_version, scoring_policy.version, row.player_id, row.game_id)
        if key not in self._residuals:
            try:
                snapshot = self.reference.project(
                    dataset,
                    player_id=row.player_id,
                    game_id=row.game_id,
                    scoring_policy=scoring_policy,
                )
            except ValueError:
                self._residuals[key] = None
            else:
                actual = calculate_fantasy_points(row.target_box_score, scoring_policy)
                self._residuals[key] = actual - snapshot.distribution.expected_value
        return self._residuals[key]

    def matching(
        self,
        dataset: HistoricalFeatureDataset,
        *,
        target: HistoricalFeatureRow,
        feature: ResidualFeature,
        scoring_policy: ScoringPolicy,
        lookback_days: int,
    ) -> tuple[tuple[HistoricalFeatureRow, float], ...]:
        state = self._prepare(
            dataset,
            target=target,
            feature=feature,
            scoring_policy=scoring_policy,
        )
        target_key = _feature_key(target, feature)
        return tuple(
            (row, residual)
            for row, residual in state.rows.get(target_key, ())
            if (target.game_start - row.game_start).days <= lookback_days
        )

    def summarize(
        self,
        dataset: HistoricalFeatureDataset,
        *,
        target: HistoricalFeatureRow,
        feature: ResidualFeature,
        scoring_policy: ScoringPolicy,
        lookback_days: int,
        half_life_days: float,
    ) -> _ResidualSummary:
        state = self._prepare(
            dataset,
            target=target,
            feature=feature,
            scoring_policy=scoring_policy,
        )
        weighted = state.weighted.get(half_life_days)
        if weighted is None:
            weighted = {
                key: _WeightedResidualSeries.from_rows(rows, half_life_days=half_life_days)
                for key, rows in state.rows.items()
            }
            state.weighted[half_life_days] = weighted
        series = weighted.get(_feature_key(target, feature))
        if series is None:
            return _ResidualSummary(0.0, 0.0, 0)
        return series.summary(target.game_start, lookback_days=lookback_days)

    def _prepare(
        self,
        dataset: HistoricalFeatureDataset,
        *,
        target: HistoricalFeatureRow,
        feature: ResidualFeature,
        scoring_policy: ScoringPolicy,
    ) -> _GroupedResiduals:
        state_key = (dataset.dataset_version, scoring_policy.version, feature)
        state = self._groups.setdefault(state_key, _GroupedResiduals())
        point_in_time_count = getattr(dataset.rows, "prior_count", None)
        prior_count = (
            point_in_time_count
            if isinstance(point_in_time_count, int)
            else sum(row.game_start < target.game_start for row in dataset.rows)
        )
        if prior_count < state.processed_count:
            raise ResidualCandidateError("Residual candidates must be evaluated chronologically")
        for row in dataset.rows[state.processed_count : prior_count]:
            residual = self.residual(dataset, row, scoring_policy)
            if residual is not None:
                key = _feature_key(row, feature)
                state.rows.setdefault(key, []).append((row, residual))
                for half_life_days, groups in state.weighted.items():
                    series = groups.setdefault(
                        key,
                        _WeightedResidualSeries(half_life_days=half_life_days),
                    )
                    series.append(row, residual)
        state.processed_count = prior_count
        return state


@dataclass(slots=True)
class _GroupedResiduals:
    processed_count: int = 0
    rows: dict[tuple[str, ...], list[tuple[HistoricalFeatureRow, float]]] = field(
        default_factory=dict
    )
    weighted: dict[float, dict[tuple[str, ...], _WeightedResidualSeries]] = field(
        default_factory=dict
    )


@dataclass(frozen=True, slots=True)
class _ResidualSummary:
    weighted_residual: float
    effective_sample: float
    count: int


@dataclass(slots=True)
class _WeightedResidualSeries:
    half_life_days: float
    origin: datetime = datetime(2020, 1, 1, tzinfo=UTC)
    starts: list[datetime] = field(default_factory=list)
    cumulative_weights: list[float] = field(default_factory=list)
    cumulative_weighted_residuals: list[float] = field(default_factory=list)

    @classmethod
    def from_rows(
        cls,
        rows: Sequence[tuple[HistoricalFeatureRow, float]],
        *,
        half_life_days: float,
    ) -> _WeightedResidualSeries:
        result = cls(half_life_days=half_life_days)
        for row, residual in rows:
            result.append(row, residual)
        return result

    def append(self, row: HistoricalFeatureRow, residual: float) -> None:
        age_from_origin = (row.game_start - self.origin).total_seconds() / 86400
        weight = exp(log(2) * age_from_origin / self.half_life_days)
        self.starts.append(row.game_start)
        self.cumulative_weights.append(
            weight + (self.cumulative_weights[-1] if self.cumulative_weights else 0)
        )
        self.cumulative_weighted_residuals.append(
            residual * weight
            + (self.cumulative_weighted_residuals[-1] if self.cumulative_weighted_residuals else 0)
        )

    def summary(self, target_at: datetime, *, lookback_days: int) -> _ResidualSummary:
        upper = bisect_left(self.starts, target_at)
        lower = bisect_left(self.starts, target_at - timedelta(days=lookback_days))
        count = upper - lower
        if count <= 0:
            return _ResidualSummary(0.0, 0.0, 0)
        transformed_weight = self.cumulative_weights[upper - 1] - (
            self.cumulative_weights[lower - 1] if lower else 0
        )
        transformed_residual = self.cumulative_weighted_residuals[upper - 1] - (
            self.cumulative_weighted_residuals[lower - 1] if lower else 0
        )
        target_age = (target_at - self.origin).total_seconds() / 86400
        effective_sample = transformed_weight * exp(-log(2) * target_age / self.half_life_days)
        return _ResidualSummary(
            transformed_residual / transformed_weight,
            effective_sample,
            count,
        )


@dataclass(frozen=True, slots=True)
class _Adjustment:
    feature: ResidualFeature
    key: tuple[str, ...]
    value: float
    effective_sample: float
    matching_count: int
    matching_fingerprint: str


class ShrunkenResidualCandidate:
    def __init__(
        self,
        config: ResidualCandidateConfig,
        *,
        reference: ProjectionModel | None = None,
        residual_history: ResidualHistory | None = None,
    ) -> None:
        self.config = config
        self.reference = reference or DirectFantasyPointBaseline()
        self.residual_history = residual_history or ResidualHistory(self.reference)

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
        reference = self.reference.project(
            dataset,
            player_id=player_id,
            game_id=game_id,
            scoring_policy=scoring_policy,
            exceed_score=None,
        )
        adjustments = tuple(
            self._adjustment(
                dataset,
                target=target,
                feature=feature,
                scoring_policy=scoring_policy,
            )
            for feature in self.config.features
        )
        total_adjustment = _clamp(
            sum(adjustment.value for adjustment in adjustments),
            -self.config.max_adjustment,
            self.config.max_adjustment,
        )
        distribution = ProjectionDistribution.from_weighted_observations(
            tuple(
                (value + total_adjustment, weight)
                for value, weight in reference.distribution.weighted_observations
            ),
            percentiles=tuple(percentile for percentile, _ in reference.distribution.percentiles),
        )
        if exceed_score is not None:
            distribution = distribution.for_exceedance_score(exceed_score)
        enabled_deferred = _enabled_deferred_reasons(self.config.features)
        reasons = tuple(
            reason for reason in reference.reasons if reason.code not in enabled_deferred
        ) + tuple(
            ProjectionReason(
                code=f"residual_{adjustment.feature.value}",
                message=(
                    f"Applied {adjustment.value:+.2f} fantasy points from "
                    f"{adjustment.effective_sample:.2f} effective matching prior games."
                ),
                adjustment=adjustment.value,
                applied=adjustment.effective_sample > 0,
            )
            for adjustment in adjustments
        )
        return ProjectionSnapshot(
            player_id=player_id,
            game_id=game_id,
            available_as_of=target.available_as_of,
            model_version=self.config.model_version,
            input_version=_input_version(reference, target, adjustments),
            scoring_policy_version=scoring_policy.version,
            distribution=distribution,
            reasons=reasons,
        )

    def _adjustment(
        self,
        dataset: HistoricalFeatureDataset,
        *,
        target: HistoricalFeatureRow,
        feature: ResidualFeature,
        scoring_policy: ScoringPolicy,
    ) -> _Adjustment:
        target_key = _feature_key(target, feature)
        summary = self.residual_history.summarize(
            dataset,
            target=target,
            feature=feature,
            scoring_policy=scoring_policy,
            lookback_days=self.config.lookback_days,
            half_life_days=self.config.recency_half_life_days,
        )
        effective_sample = summary.effective_sample
        if effective_sample <= 0:
            return _Adjustment(feature, target_key, 0.0, 0.0, 0, "empty")
        shrinkage = effective_sample / (effective_sample + self.config.shrinkage_games)
        value = _clamp(
            summary.weighted_residual * shrinkage,
            -self.config.max_adjustment,
            self.config.max_adjustment,
        )
        fingerprint = hashlib.sha256(
            repr(
                (
                    feature.value,
                    target_key,
                    summary.count,
                    round(summary.weighted_residual, 12),
                    round(summary.effective_sample, 12),
                )
            ).encode()
        ).hexdigest()
        return _Adjustment(
            feature,
            target_key,
            round(value, 6),
            round(effective_sample, 6),
            summary.count,
            fingerprint,
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
        raise ResidualCandidateError(
            f"Expected one feature row for player/game, found {len(matches)}"
        )
    return matches[0]


def _feature_key(
    row: HistoricalFeatureRow,
    feature: ResidualFeature,
) -> tuple[str, ...]:
    if feature is ResidualFeature.OPPONENT_IDENTITY:
        return (row.opponent_team_id,)
    if feature is ResidualFeature.OPPONENT_STRENGTH:
        return (
            row.opponent_offense_band,
            row.opponent_defense_band,
            row.opponent_pace_band,
        )
    if feature is ResidualFeature.REST:
        if row.is_back_to_back:
            return ("back_to_back",)
        if row.days_rest is None:
            return ("unknown",)
        return (str(min(row.days_rest, 3)),)
    if feature is ResidualFeature.TRAVEL:
        return (
            _distance_band(row.travel_distance_miles),
            _time_zone_band(row.time_zone_change_hours),
            row.travel_direction,
            row.travel_fallback,
        )
    if feature is ResidualFeature.INJURY:
        return (row.availability_status.value, row.availability_observation.value)
    raise ResidualCandidateError(f"Unsupported residual feature: {feature}")


def _distance_band(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < 150:
        return "local"
    if value < 750:
        return "short"
    if value < 1500:
        return "medium"
    return "long"


def _time_zone_band(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value == 0:
        return "none"
    return f"{'east' if value > 0 else 'west'}_{min(abs(round(value)), 3)}"


def _enabled_deferred_reasons(features: tuple[ResidualFeature, ...]) -> set[str]:
    reasons: set[str] = set()
    for feature in features:
        if feature in (ResidualFeature.OPPONENT_IDENTITY, ResidualFeature.OPPONENT_STRENGTH):
            reasons.update(("deferred_opponent", "deferred_pace"))
        else:
            reasons.add(f"deferred_{feature.value}")
    return reasons


def _input_version(
    reference: ProjectionSnapshot,
    target: HistoricalFeatureRow,
    adjustments: tuple[_Adjustment, ...],
) -> str:
    payload = {
        "reference_input_version": reference.input_version,
        "target": (target.player_id, target.game_id, target.available_as_of.isoformat()),
        "adjustments": [
            {
                "feature": adjustment.feature,
                "key": adjustment.key,
                "value": adjustment.value,
                "effective_sample": adjustment.effective_sample,
                "matching_count": adjustment.matching_count,
                "matching_fingerprint": adjustment.matching_fingerprint,
            }
            for adjustment in adjustments
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"residual-input-v2-{hashlib.sha256(encoded).hexdigest()[:12]}"


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))
