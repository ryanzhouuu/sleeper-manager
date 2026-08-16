from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import astuple, dataclass
from math import exp, isfinite, log

from sleeper_manager.domain.nba import AvailabilityStatus
from sleeper_manager.domain.projection import (
    ProjectionAdjustmentKind,
    ProjectionComponent,
    ProjectionDistribution,
    ProjectionFallback,
    ProjectionReason,
    ProjectionSnapshot,
)
from sleeper_manager.domain.scoring import ScoringPolicy, calculate_fantasy_points
from sleeper_manager.integrations.nba.historical_features import (
    AvailabilityObservation,
    HistoricalFeatureDataset,
    HistoricalFeatureRow,
    OpponentStatsFallback,
)


class OpportunityModelError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class OpportunityModelConfig:
    recency_half_life_days: float = 14.0
    participation_prior_strength: float = 4.0
    production_shrinkage_minutes: float = 120.0
    pace_clip: tuple[float, float] = (0.92, 1.08)
    percentiles: tuple[int, ...] = (10, 25, 50, 75, 90)

    def __post_init__(self) -> None:
        if not isfinite(self.recency_half_life_days) or self.recency_half_life_days <= 0:
            raise OpportunityModelError("Recency half-life must be finite and positive")
        if self.participation_prior_strength < 0 or not isfinite(self.participation_prior_strength):
            raise OpportunityModelError("Participation prior strength must be non-negative")
        if self.production_shrinkage_minutes < 0 or not isfinite(self.production_shrinkage_minutes):
            raise OpportunityModelError("Production shrinkage must be non-negative")
        if len(self.pace_clip) != 2 or not 0 < self.pace_clip[0] <= self.pace_clip[1]:
            raise OpportunityModelError("Pace clip must be an ordered positive range")
        if tuple(sorted(set(self.percentiles))) != self.percentiles:
            raise OpportunityModelError("Projection percentiles must be unique and ordered")

    @property
    def model_version(self) -> str:
        payload = {
            "recency_half_life_days": self.recency_half_life_days,
            "participation_prior_strength": self.participation_prior_strength,
            "production_shrinkage_minutes": self.production_shrinkage_minutes,
            "pace_clip": self.pace_clip,
            "percentiles": self.percentiles,
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]
        return f"opportunity-v3-{digest}"


@dataclass(frozen=True, slots=True)
class _Estimate:
    value: float
    baseline: float | None
    adjustment: float | None
    kind: ProjectionAdjustmentKind
    effective_sample: float
    fallback: ProjectionFallback
    message: str

    def component(self, code: str) -> ProjectionComponent:
        return ProjectionComponent(
            code=code,
            estimate=round(self.value, 6),
            baseline=None if self.baseline is None else round(self.baseline, 6),
            adjustment=None if self.adjustment is None else round(self.adjustment, 6),
            kind=self.kind,
            effective_sample=round(self.effective_sample, 6),
            fallback=self.fallback,
            message=self.message,
        )


class AvailabilityModel:
    def __init__(self, config: OpportunityModelConfig) -> None:
        self.config = config

    def estimate(
        self, target: HistoricalFeatureRow, prior_rows: Sequence[HistoricalFeatureRow]
    ) -> _Estimate:
        observations = tuple(_weighted_rows(target, prior_rows, self.config.recency_half_life_days))
        effective = sum(weight for _, weight in observations)
        played = sum(weight for row, weight in observations if row.target_did_play)
        prior_probability = (
            (played + self.config.participation_prior_strength * 0.75)
            / (effective + self.config.participation_prior_strength)
            if effective + self.config.participation_prior_strength
            else 0.75
        )
        status_probability = _status_probability(target.availability_status)
        if target.availability_status is AvailabilityStatus.UNKNOWN:
            status_probability = 0.75
        estimate = (2 * prior_probability + status_probability) / 3
        if target.availability_observation is AvailabilityObservation.MISSING_REPORT:
            message = "No injury report was available; retained an explicit uncertain status."
        elif target.availability_observation is AvailabilityObservation.TEAM_NOT_YET_SUBMITTED:
            message = "The team report was not submitted by cutoff; no AVAILABLE shortcut was used."
        else:
            message = (
                f"Combined {effective:.2f} weighted prior participation games with status evidence."
            )
        fallback = ProjectionFallback.OBSERVED if effective else ProjectionFallback.SHRUNK
        return _Estimate(
            value=min(max(estimate, 0.0), 1.0),
            baseline=prior_probability,
            adjustment=estimate - prior_probability,
            kind=ProjectionAdjustmentKind.ADDITIVE,
            effective_sample=effective,
            fallback=fallback,
            message=message,
        )


class MinutesModel:
    def __init__(self, config: OpportunityModelConfig) -> None:
        self.config = config

    def estimate(
        self, target: HistoricalFeatureRow, prior_rows: Sequence[HistoricalFeatureRow]
    ) -> _Estimate:
        played = tuple(
            (row, weight)
            for row, weight in _weighted_rows(
                target, prior_rows, self.config.recency_half_life_days
            )
            if row.target_did_play and row.target_minutes is not None and row.target_minutes > 0
        )
        effective = sum(weight for _, weight in played)
        if not played:
            estimate = target.prior_minutes_mean or 0.0
            return _Estimate(
                estimate,
                None,
                None,
                ProjectionAdjustmentKind.ADDITIVE,
                0.0,
                ProjectionFallback.LEAGUE_AVERAGE,
                "No played-minute history was available; used the row's prior role fallback.",
            )
        weighted_minutes = _weighted_mean(
            ((row.target_minutes or 0.0, weight) for row, weight in played)
        )
        start_rate = _weighted_mean(((float(row.target_started), weight) for row, weight in played))
        role_multiplier = 0.92 + 0.16 * start_rate
        estimate = weighted_minutes * role_multiplier
        return _Estimate(
            value=max(estimate, 0.0),
            baseline=weighted_minutes,
            adjustment=estimate - weighted_minutes,
            kind=ProjectionAdjustmentKind.MULTIPLICATIVE,
            effective_sample=effective,
            fallback=(ProjectionFallback.OBSERVED if effective >= 3 else ProjectionFallback.SHRUNK),
            message=(
                f"Used {effective:.2f} weighted played games and a {start_rate:.0%} start rate."
            ),
        )


class ProductionRateModel:
    def __init__(self, config: OpportunityModelConfig) -> None:
        self.config = config

    def estimate(
        self,
        target: HistoricalFeatureRow,
        prior_rows: Sequence[HistoricalFeatureRow],
        scoring_policy: ScoringPolicy,
        *,
        league_prior_rows: Sequence[HistoricalFeatureRow] = (),
    ) -> _Estimate:
        observations = tuple(
            (row, weight)
            for row, weight in _weighted_rows(
                target, prior_rows, self.config.recency_half_life_days
            )
            if row.target_did_play and row.target_minutes is not None and row.target_minutes > 0
        )
        if not observations:
            return _Estimate(
                0.0,
                None,
                None,
                ProjectionAdjustmentKind.ADDITIVE,
                0.0,
                ProjectionFallback.LEAGUE_AVERAGE,
                "No conditional production history was available; used a zero-rate fallback.",
            )
        rates = _production_rates(observations, scoring_policy)
        player_rate = _weighted_mean(rates)
        effective_minutes = sum(weight for _, weight in rates)
        independent_prior_rows = tuple(
            row for row in league_prior_rows if row.player_id != target.player_id
        )
        independent_observations = tuple(
            (row, weight)
            for row, weight in _weighted_rows(
                target, independent_prior_rows, self.config.recency_half_life_days
            )
            if row.target_did_play and row.target_minutes is not None and row.target_minutes > 0
        )
        if independent_observations:
            league_rate = _weighted_mean(
                _production_rates(independent_observations, scoring_policy)
            )
            prior_source = "independent league prior"
        else:
            shared_observations = tuple(
                (row, weight)
                for row, weight in _weighted_rows(
                    target, league_prior_rows, self.config.recency_half_life_days
                )
                if row.target_did_play and row.target_minutes is not None and row.target_minutes > 0
            )
            if shared_observations:
                league_rate = _weighted_mean(_production_rates(shared_observations, scoring_policy))
                prior_source = "shared player-history fallback"
            else:
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
        shrinkage = effective_minutes / (
            effective_minutes + self.config.production_shrinkage_minutes
        )
        estimate = player_rate * shrinkage + league_rate * (1 - shrinkage)
        if prior_source == "shared player-history fallback":
            fallback = ProjectionFallback.OBSERVED
            message = (
                "No independent player evidence was available for the league prior; used a "
                f"shared player-history fallback with {effective_minutes:.1f} weighted minutes."
            )
        else:
            fallback = (
                ProjectionFallback.OBSERVED
                if effective_minutes >= 120
                else ProjectionFallback.SHRUNK
            )
            message = (
                f"Applied an independent league FPPM prior with {effective_minutes:.1f} "
                f"weighted player minutes and {shrinkage:.1%} player weight."
            )
        return _Estimate(
            value=estimate,
            baseline=player_rate,
            adjustment=estimate - player_rate,
            kind=ProjectionAdjustmentKind.ADDITIVE,
            effective_sample=effective_minutes,
            fallback=fallback,
            message=message,
        )


class EnvironmentModel:
    def __init__(self, config: OpportunityModelConfig) -> None:
        self.config = config

    def estimate(self, target: HistoricalFeatureRow) -> tuple[_Estimate, ...]:
        pace = target.pace_factor or 1.0
        pace = min(max(pace, self.config.pace_clip[0]), self.config.pace_clip[1])
        defense = 1.0
        if (
            target.opponent_defensive_rating is not None
            and target.league_defensive_rating is not None
            and target.league_defensive_rating > 0
        ):
            defense = min(
                max(
                    target.opponent_defensive_rating / target.league_defensive_rating,
                    0.9,
                ),
                1.1,
            )
            defense_fallback = _defense_fallback(target.opponent_stats_fallback)
            defense_message = (
                "Scaled opponent defense by the opponent defensive rating "
                f"{target.opponent_defensive_rating:.2f} relative to the prior league "
                f"baseline {target.league_defensive_rating:.2f}."
            )
        else:
            defense_fallback = ProjectionFallback.MISSING
            defense_message = (
                "Opponent defense was unavailable or had a non-positive league baseline; "
                "used a neutral factor."
            )
        rest = 0.97 if target.is_back_to_back else 1.0
        if target.days_rest is not None and target.days_rest >= 2:
            rest = 1.01
        travel = 1.0
        if target.time_zone_change_hours is not None:
            travel = max(0.97, 1.0 - abs(target.time_zone_change_hours) * 0.005)
        return (
            _Estimate(
                pace,
                1.0,
                pace - 1.0,
                ProjectionAdjustmentKind.MULTIPLICATIVE,
                float(target.opponent_sample_size),
                ProjectionFallback.OBSERVED
                if target.pace_factor is not None
                else ProjectionFallback.MISSING,
                "Scaled opportunity by the continuous relative matchup pace factor.",
            ),
            _Estimate(
                defense,
                1.0,
                defense - 1.0,
                ProjectionAdjustmentKind.MULTIPLICATIVE,
                float(target.opponent_sample_size),
                defense_fallback,
                defense_message,
            ),
            _Estimate(
                rest,
                1.0,
                rest - 1.0,
                ProjectionAdjustmentKind.MULTIPLICATIVE,
                1.0 if target.days_rest is not None else 0.0,
                ProjectionFallback.OBSERVED
                if target.days_rest is not None
                else ProjectionFallback.MISSING,
                "Adjusted for rest and back-to-back schedule compression.",
            ),
            _Estimate(
                travel,
                1.0,
                travel - 1.0,
                ProjectionAdjustmentKind.MULTIPLICATIVE,
                1.0 if target.time_zone_change_hours is not None else 0.0,
                ProjectionFallback.OBSERVED
                if target.time_zone_change_hours is not None
                else ProjectionFallback.MISSING,
                "Applied a bounded travel time-zone adjustment when location data was present.",
            ),
        )


class InterpretableOpportunityModel:
    def __init__(self, config: OpportunityModelConfig | None = None) -> None:
        self.config = config or OpportunityModelConfig()
        self.availability = AvailabilityModel(self.config)
        self.minutes = MinutesModel(self.config)
        self.production_rate = ProductionRateModel(self.config)
        self.environment = EnvironmentModel(self.config)

    def project(
        self,
        dataset: HistoricalFeatureDataset,
        *,
        player_id: str,
        game_id: str,
        scoring_policy: ScoringPolicy,
        exceed_score: float | None = None,
    ) -> ProjectionSnapshot:
        target = _find_target(dataset.rows, player_id, game_id)
        player_prior_rows = tuple(
            row
            for row in dataset.rows
            if row.player_id == player_id and row.game_start < target.game_start
        )
        league_prior_rows = tuple(
            row
            for row in dataset.rows
            if row.game_start < target.game_start
            and row.target_did_play
            and row.target_minutes is not None
            and row.target_minutes > 0
        )
        availability = self.availability.estimate(target, player_prior_rows)
        minutes = self.minutes.estimate(target, player_prior_rows)
        rate = self.production_rate.estimate(
            target,
            player_prior_rows,
            scoring_policy,
            league_prior_rows=league_prior_rows,
        )
        environments = self.environment.estimate(target)
        environment_multiplier = 1.0
        for environment in environments:
            environment_multiplier *= environment.value
        conditional_expected = minutes.value * rate.value * environment_multiplier
        conditional_observations = _conditional_observations(
            target,
            player_prior_rows,
            scoring_policy,
            conditional_expected,
            self.config,
        )
        weighted: list[tuple[float, float]] = [(0.0, max(1.0 - availability.value, 1e-9))]
        if conditional_observations and availability.value > 0:
            weighted.extend(
                (value, weight * availability.value) for value, weight in conditional_observations
            )
        else:
            weighted[0] = (0.0, 1.0)
        distribution = ProjectionDistribution.from_weighted_observations(
            weighted, percentiles=self.config.percentiles
        )
        if exceed_score is not None:
            distribution = distribution.for_exceedance_score(exceed_score)
        components = (
            availability.component("availability"),
            minutes.component("minutes"),
            rate.component("production_rate"),
            environments[0].component("pace"),
            environments[1].component("opponent_defense"),
            environments[2].component("rest"),
            environments[3].component("travel"),
        )
        reasons = (
            ProjectionReason(
                "availability", availability.message, adjustment=availability.adjustment
            ),
            ProjectionReason("minutes", minutes.message, adjustment=minutes.adjustment),
            ProjectionReason("production_rate", rate.message, adjustment=rate.adjustment),
            ProjectionReason(
                "environment",
                "Combined pace, defense, rest, and travel multipliers to "
                f"{environment_multiplier:.4f}.",
                adjustment=environment_multiplier - 1.0,
            ),
            ProjectionReason(
                "mixture",
                "Full expectation is "
                f"{availability.value:.3f} × {conditional_expected:.2f}; DNP mass is explicit.",
            ),
        )
        return ProjectionSnapshot(
            player_id=player_id,
            game_id=game_id,
            available_as_of=target.available_as_of,
            model_version=self.config.model_version,
            input_version=_input_version(
                dataset,
                target,
                player_prior_rows,
                league_prior_rows,
                scoring_policy,
            ),
            scoring_policy_version=scoring_policy.version,
            distribution=distribution,
            reasons=reasons,
            components=components,
        )


def _find_target(
    rows: Sequence[HistoricalFeatureRow], player_id: str, game_id: str
) -> HistoricalFeatureRow:
    matches = tuple(row for row in rows if row.player_id == player_id and row.game_id == game_id)
    if len(matches) != 1:
        raise OpportunityModelError(
            f"Expected one feature row for player/game, found {len(matches)}"
        )
    return matches[0]


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


def _input_version(
    dataset: HistoricalFeatureDataset,
    target: HistoricalFeatureRow,
    prior_rows: Sequence[HistoricalFeatureRow],
    league_prior_rows: Sequence[HistoricalFeatureRow],
    scoring_policy: ScoringPolicy,
) -> str:
    payload = {
        "dataset": dataset.dataset_version,
        "feature_schema": dataset.feature_schema_version,
        "target": (target.player_id, target.game_id, target.available_as_of.isoformat()),
        "target_environment": (
            target.pace_factor,
            target.opponent_defensive_rating,
            target.league_defensive_rating,
            target.days_rest,
            target.is_back_to_back,
            target.time_zone_change_hours,
        ),
        "player_prior": tuple(_input_row(row) for row in prior_rows),
        "league_prior": tuple(_input_row(row) for row in league_prior_rows),
        "scoring": scoring_policy.version,
    }
    return "inputs-" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def _input_row(row: HistoricalFeatureRow) -> tuple[object, ...]:
    return (
        row.player_id,
        row.game_id,
        row.game_start.isoformat(),
        row.target_did_play,
        row.target_minutes,
        astuple(row.target_box_score),
    )


__all__ = (
    "AvailabilityModel",
    "EnvironmentModel",
    "InterpretableOpportunityModel",
    "MinutesModel",
    "OpportunityModelConfig",
    "OpportunityModelError",
    "ProductionRateModel",
)
