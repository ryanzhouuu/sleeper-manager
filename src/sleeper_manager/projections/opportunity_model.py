from __future__ import annotations

from sleeper_manager.domain.projection import (
    ProjectionDistribution,
    ProjectionReason,
    ProjectionSnapshot,
)
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.integrations.nba.historical_feature_models import HistoricalFeatureDataset
from sleeper_manager.projections.opportunity_components import (
    AvailabilityModel,
    EnvironmentModel,
    MinutesModel,
    ProductionRateModel,
)
from sleeper_manager.projections.opportunity_history import _HistoricalIndex, _input_version
from sleeper_manager.projections.opportunity_statistics import _conditional_observations
from sleeper_manager.projections.opportunity_types import OpportunityModelConfig


class InterpretableOpportunityModel:
    def __init__(self, config: OpportunityModelConfig | None = None) -> None:
        self.config = config or OpportunityModelConfig()
        self.availability = AvailabilityModel(self.config)
        self.minutes = MinutesModel(self.config)
        self.production_rate = ProductionRateModel(self.config)
        self.environment = EnvironmentModel(self.config)
        self._index: _HistoricalIndex | None = None

    @property
    def model_version(self) -> str:
        return self.config.model_version

    def _history(self, dataset_version: str, scoring_policy: ScoringPolicy) -> _HistoricalIndex:
        index = self._index
        if index is None or not index.matches(dataset_version, scoring_policy.version):
            index = _HistoricalIndex(
                dataset_version,
                scoring_policy,
                self.config.recency_half_life_days,
            )
            self._index = index
        return index

    def project(
        self,
        dataset: HistoricalFeatureDataset,
        *,
        player_id: str,
        game_id: str,
        scoring_policy: ScoringPolicy,
        exceed_score: float | None = None,
    ) -> ProjectionSnapshot:
        index = self._history(dataset.dataset_version, scoring_policy)
        target = index.resolve_target(dataset.rows, player_id, game_id)
        player_prior_rows = index.player_prior_rows(player_id, target.game_start)
        league_prior = index.league_production_prior(player_id, target.game_start)
        availability = self.availability.estimate(target, player_prior_rows)
        minutes = self.minutes.estimate(target, player_prior_rows)
        rate = self.production_rate.estimate(
            target,
            player_prior_rows,
            scoring_policy,
            league_prior=league_prior,
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
                league_prior.fingerprint,
                scoring_policy,
                self.config,
            ),
            scoring_policy_version=scoring_policy.version,
            distribution=distribution,
            reasons=reasons,
            components=components,
        )


__all__ = ("InterpretableOpportunityModel",)
