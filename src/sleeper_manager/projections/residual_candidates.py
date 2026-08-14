from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
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
    def __init__(self, projector: ProjectionModel) -> None:
        self.projector = projector
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
        state_key = (dataset.dataset_version, scoring_policy.version, feature)
        state = self._groups.setdefault(state_key, _GroupedResiduals())
        prior_count = (
            len(dataset.rows) - 1
            if dataset.rows and dataset.rows[-1] == target
            else sum(row.game_start < target.game_start for row in dataset.rows)
        )
        if prior_count < state.processed_count:
            raise ResidualCandidateError("Residual candidates must be evaluated chronologically")
        for row in dataset.rows[state.processed_count : prior_count]:
            residual = self.residual(dataset, row, scoring_policy)
            if residual is not None:
                state.rows.setdefault(_feature_key(row, feature), []).append((row, residual))
        state.processed_count = prior_count
        target_key = _feature_key(target, feature)
        return tuple(
            (row, residual)
            for row, residual in state.rows.get(target_key, ())
            if (target.game_start - row.game_start).days <= lookback_days
        )


@dataclass(slots=True)
class _GroupedResiduals:
    processed_count: int = 0
    rows: dict[tuple[str, ...], list[tuple[HistoricalFeatureRow, float]]] = field(
        default_factory=dict
    )


@dataclass(frozen=True, slots=True)
class _Adjustment:
    feature: ResidualFeature
    key: tuple[str, ...]
    value: float
    effective_sample: float
    matching_rows: tuple[tuple[str, str], ...]


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
        observations: list[tuple[float, float, HistoricalFeatureRow]] = []
        matching = self.residual_history.matching(
            dataset,
            target=target,
            feature=feature,
            scoring_policy=scoring_policy,
            lookback_days=self.config.lookback_days,
        )
        for row, residual in matching:
            age_days = max((target.game_start - row.game_start).total_seconds() / 86400, 0)
            weight = exp(-log(2) * age_days / self.config.recency_half_life_days)
            observations.append((residual, weight, row))
        effective_sample = sum(weight for _, weight, _ in observations)
        if effective_sample <= 0:
            return _Adjustment(feature, target_key, 0.0, 0.0, ())
        weighted_residual = (
            sum(residual * weight for residual, weight, _ in observations) / effective_sample
        )
        shrinkage = effective_sample / (effective_sample + self.config.shrinkage_games)
        value = _clamp(
            weighted_residual * shrinkage,
            -self.config.max_adjustment,
            self.config.max_adjustment,
        )
        return _Adjustment(
            feature,
            target_key,
            round(value, 6),
            round(effective_sample, 6),
            tuple((row.player_id, row.game_id) for _, _, row in observations),
        )


def _find_target(
    rows: Iterable[HistoricalFeatureRow], *, player_id: str, game_id: str
) -> HistoricalFeatureRow:
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
                "matching_rows": adjustment.matching_rows,
            }
            for adjustment in adjustments
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"residual-input-v1-{hashlib.sha256(encoded).hexdigest()[:12]}"


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))
