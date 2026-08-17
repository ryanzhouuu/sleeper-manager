from __future__ import annotations

from collections.abc import Sequence

from sleeper_manager.domain.nba import AvailabilityStatus
from sleeper_manager.domain.projection import ProjectionAdjustmentKind, ProjectionFallback
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.integrations.nba.historical_feature_models import (
    AvailabilityObservation,
    HistoricalFeatureRow,
)
from sleeper_manager.projections.opportunity_statistics import (
    _defense_fallback,
    _played_observations,
    _production_rates,
    _status_probability,
    _unshrunk_rate,
    _weighted_mean,
    _weighted_rows,
)
from sleeper_manager.projections.opportunity_types import (
    OpportunityModelConfig,
    _Estimate,
    _LeagueProductionPrior,
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
        league_prior: _LeagueProductionPrior | None = None,
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
        if league_prior is not None and league_prior.independent_rate is not None:
            league_rate = league_prior.independent_rate
            prior_source = "independent league prior"
        elif league_prior is not None and league_prior.shared_rate is not None:
            league_rate = league_prior.shared_rate
            prior_source = "shared player-history fallback"
        else:
            independent_prior_rows = tuple(
                row for row in league_prior_rows if row.player_id != target.player_id
            )
            independent_observations = _played_observations(
                target,
                independent_prior_rows,
                self.config.recency_half_life_days,
            )
            if independent_observations:
                league_rate = _weighted_mean(
                    _production_rates(independent_observations, scoring_policy)
                )
                prior_source = "independent league prior"
            else:
                shared_observations = _played_observations(
                    target,
                    league_prior_rows,
                    self.config.recency_half_life_days,
                )
                if shared_observations:
                    league_rate = _weighted_mean(
                        _production_rates(shared_observations, scoring_policy)
                    )
                    prior_source = "shared player-history fallback"
                else:
                    return _unshrunk_rate(player_rate, effective_minutes)
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
        if self.config.disable_pace:
            pace = 1.0
            pace_fallback = ProjectionFallback.MISSING
            pace_message = "Pace ablation is enabled; the pace multiplier is neutralized to 1.0."
            pace_sample = 0.0
        else:
            pace = target.pace_factor or 1.0
            pace = min(max(pace, self.config.pace_clip[0]), self.config.pace_clip[1])
            pace_fallback = (
                ProjectionFallback.OBSERVED
                if target.pace_factor is not None
                else ProjectionFallback.MISSING
            )
            pace_message = "Scaled opportunity by the continuous relative matchup pace factor."
            pace_sample = float(target.opponent_sample_size)
        if self.config.disable_defense:
            defense = 1.0
            defense_fallback = ProjectionFallback.MISSING
            defense_message = (
                "Defense ablation is enabled; the defense multiplier is neutralized to 1.0."
            )
        elif (
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
            defense = 1.0
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
                pace_sample,
                pace_fallback,
                pace_message,
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
