"""Opening-week joint-outcome candidate; every failed attempt preserves its opportunity."""

import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from hashlib import sha256
from math import fsum

from sleeper_manager.domain.forecast_capture import ForecastRetrievalResult
from sleeper_manager.domain.nba_season import nba_season_start_year
from sleeper_manager.domain.projection import (
    ProjectionDistribution,
    ProjectionReason,
    ProjectionSnapshot,
)
from sleeper_manager.domain.scoring import ScoringPolicy, calculate_fantasy_points
from sleeper_manager.projections.hybrid_config import HybridProjectionConfig, HybridProjectionError
from sleeper_manager.projections.hybrid_external import qualify_external_center
from sleeper_manager.projections.hybrid_fitting import (
    JointWeightFit,
    fit_joint_weights,
    weight_support,
)
from sleeper_manager.projections.hybrid_participation import (
    HybridParticipation,
    ParticipationEstimate,
)
from sleeper_manager.projections.hybrid_pools import HybridHistoryPools, JointStatPool, stat_means
from sleeper_manager.projections.hybrid_types import GameAvailability, HybridHistory, require_aware
from sleeper_manager.projections.live_availability import constrain_live_availability
from sleeper_manager.projections.live_baseline import LiveProjectionTarget


@dataclass(frozen=True, slots=True)
class HybridProjectionResult:
    """Retain the attempt key and evidence even when no legal snapshot can be generated."""

    target: LiveProjectionTarget
    cutoff: datetime
    projection: ProjectionSnapshot | None = None
    failure: str | None = None
    source: str | None = None
    external_rejection: str | None = None
    forecast: ForecastRetrievalResult | None = None
    requested_center: tuple[float, ...] | None = None
    actual_center: tuple[float, ...] = ()
    donor_ids: tuple[str, ...] = ()
    history_kind: str | None = None
    effective_sample: float | None = None
    maximum_weight: float | None = None
    participation: ParticipationEstimate | None = None
    joint_weights: tuple[tuple[str, str, float], ...] = ()
    excluded_games: tuple[tuple[str, str], ...] = ()
    history_fingerprint: str | None = None
    conditional_projection_gap: str | None = None


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Cannot fingerprint {type(value).__name__}")


def _fingerprint(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=_json_default).encode()
    ).hexdigest()


class HybridProjectionBatch:
    """Share cutoff preparation across every remaining game on either roster."""

    def __init__(
        self,
        history: HybridHistory,
        *,
        cutoff: datetime,
        game_start: datetime,
        scoring_policy: ScoringPolicy,
        config: HybridProjectionConfig | None = None,
    ) -> None:
        require_aware(cutoff)
        require_aware(game_start)
        self.cutoff = cutoff
        self.season = nba_season_start_year(game_start)
        self.scoring_policy = scoring_policy
        self.config = config or HybridProjectionConfig()
        self._fits: dict[tuple[str, tuple[float, ...]], JointWeightFit] = {}
        self.pools = HybridHistoryPools(
            history,
            cutoff=cutoff,
            game_start=game_start,
            scoring_policy=scoring_policy,
            config=self.config,
        )
        self.participation = HybridParticipation(
            history, cutoff=cutoff, game_start=game_start, config=self.config
        )
        visible = tuple(
            asdict(row)
            for row in sorted(history.observations, key=lambda row: (row.player_id, row.game_id))
            if row.game_start < cutoff
            and row.finalized_at <= cutoff
            and self.season - nba_season_start_year(row.game_start) in (0, 1, 2)
        )
        self.history_fingerprint = _fingerprint(
            {
                "dataset": history.dataset_version,
                "observations": visible,
                "opportunities": tuple(asdict(row) for row in self.participation.opportunities),
            }
        )

    @classmethod
    def from_prepared(
        cls,
        *,
        cutoff: datetime,
        season: int,
        scoring_policy: ScoringPolicy,
        config: HybridProjectionConfig,
        pools: HybridHistoryPools,
        participation: HybridParticipation,
        history_fingerprint: str,
    ) -> "HybridProjectionBatch":
        """Reuse verified indexed preparation while retaining the ordinary projection algorithm."""
        require_aware(cutoff)
        prepared = cls.__new__(cls)
        prepared.cutoff, prepared.season = cutoff, season
        prepared.scoring_policy, prepared.config = scoring_policy, config
        prepared.pools, prepared.participation = pools, participation
        prepared.history_fingerprint = history_fingerprint
        prepared._fits = {}
        return prepared

    def project(
        self,
        target: LiveProjectionTarget,
        *,
        forecast: ForecastRetrievalResult | None = None,
        availability: GameAvailability | None = None,
    ) -> HybridProjectionResult:
        require_aware(target.game_start)
        if target.game_start <= self.cutoff:
            return HybridProjectionResult(
                target,
                self.cutoff,
                failure="game_already_started",
                history_fingerprint=self.history_fingerprint,
            )
        if nba_season_start_year(target.game_start) != self.season:
            raise HybridProjectionError("batch_season_mismatch")
        if target.provider_player_id is None:
            return HybridProjectionResult(
                target,
                self.cutoff,
                failure="unresolved_history_identity",
                history_fingerprint=self.history_fingerprint,
            )
        if not self.config.use_external:
            forecast = None
        external = qualify_external_center(
            forecast,
            player_id=target.sleeper_player_id,
            cutoff=self.cutoff,
            game_start=target.game_start,
            config=self.config,
        )
        rejected = external.rejection
        source = "internal"
        pool: JointStatPool | None = None
        weights: tuple[float, ...] = ()
        if external.values is not None:
            try:
                pool = self.pools.pool(target.provider_player_id, external.values)
                # Fixed preparation makes the conditional fit independent of game availability.
                fit_key = target.provider_player_id, external.values
                fit = self._fits.get(fit_key)
                if fit is None:
                    fit = fit_joint_weights(
                        tuple(row.core_stats for row in pool.samples),
                        pool.weights,
                        external.values,
                        self.config,
                    )
                    self._fits[fit_key] = fit
                rejected = fit.failure or weight_support(fit.weights, self.config)
                if rejected is None:
                    weights = fit.weights
                    source = "external"
            except HybridProjectionError as error:
                rejected = error.reason
        try:
            if source == "internal":
                pool = self.pools.pool(target.provider_player_id)
                weights = pool.weights
                if failure := weight_support(weights, self.config):
                    raise HybridProjectionError(failure)
            assert pool is not None
            participation = self.participation.estimate(
                target.provider_player_id,
                game_id=target.game_id,
                availability=availability,
                donor_player_ids=pool.donor_ids,
            )
        except HybridProjectionError as error:
            absence = constrain_live_availability(
                None,
                target,
                availability,
                cutoff=self.cutoff,
                scoring_policy=self.scoring_policy,
                max_age=timedelta(minutes=self.config.availability_max_age_minutes),
            )
            if absence is not None:
                absence = replace(
                    absence,
                    model_version=self.config.model_version,
                    input_version="hybrid-input-v1-"
                    + _fingerprint(
                        {
                            "absence": absence.input_version,
                            "history": self.history_fingerprint,
                            "target": asdict(target),
                            "cutoff": self.cutoff,
                            "conditional_projection_gap": error.reason,
                        }
                    ),
                    reasons=absence.reasons
                    + (
                        ProjectionReason("conditional_projection_gap", error.reason, applied=False),
                    ),
                )
            return HybridProjectionResult(
                target,
                self.cutoff,
                projection=absence,
                failure=None if absence is not None else error.reason,
                source="confirmed_absence" if absence is not None else None,
                conditional_projection_gap=error.reason if absence is not None else None,
                external_rejection=rejected,
                forecast=forecast,
                excluded_games=self.pools.excluded_game_keys,
                history_fingerprint=self.history_fingerprint,
            )
        played_probability = participation.probability
        scores = tuple(
            (calculate_fantasy_points(row.line, self.scoring_policy), weight * played_probability)
            for row, weight in zip(pool.samples, weights, strict=True)
            if weight * played_probability > 0
        )
        if played_probability < 1:
            scores += ((0.0, 1 - played_probability),)
        center = stat_means(pool.samples, weights)
        forecast_evidence = (
            forecast
            if self.config.use_external
            and forecast is not None
            and forecast.cutoff == self.cutoff
            and forecast.player_id == target.sleeper_player_id
            else None
        )
        version = _fingerprint(
            {
                "history": self.history_fingerprint,
                "target": asdict(target),
                "cutoff": self.cutoff,
                "forecast": asdict(forecast_evidence) if forecast_evidence else None,
                "availability": asdict(participation.availability)
                if participation.availability
                else None,
            }
        )
        reasons = [
            ProjectionReason(
                "joint_outcomes", "Whole historical stat lines scored with the league policy."
            ),
            ProjectionReason(
                "center_source",
                f"{source}; history={pool.history_kind}; donors={len(pool.donor_ids)}",
            ),
            ProjectionReason(
                "provisional_participation",
                f"played={played_probability:.6f}; own={participation.own_count}; "
                f"pooled={participation.pooled_count}; pool={participation.pool_kind}; "
                f"players={participation.pooled_player_count}; bucket={participation.bucket}",
            ),
        ]
        if rejected:
            reasons.append(ProjectionReason("external_fallback", rejected))
        if participation.fallback:
            reasons.append(ProjectionReason("history_only_participation", participation.fallback))
        if participation.pool_fallback:
            reasons.append(
                ProjectionReason("participation_pool_fallback", participation.pool_fallback)
            )
        if forecast_evidence and forecast_evidence.newer_attempt_receipt_id:
            reasons.append(
                ProjectionReason(
                    "newer_forecast_attempt_gap", forecast_evidence.newer_attempt_receipt_id
                )
            )
        snapshot = ProjectionSnapshot(
            target.sleeper_player_id,
            target.game_id,
            self.cutoff,
            self.config.model_version,
            f"hybrid-input-v1-{version}",
            self.scoring_policy.version,
            ProjectionDistribution.from_weighted_observations(scores),
            tuple(reasons),
        )
        return HybridProjectionResult(
            target,
            self.cutoff,
            projection=snapshot,
            source=source,
            external_rejection=rejected,
            forecast=forecast_evidence,
            requested_center=external.values,
            actual_center=center,
            donor_ids=pool.donor_ids,
            history_kind=pool.history_kind,
            effective_sample=1 / fsum(w * w for w in weights),
            maximum_weight=max(weights),
            participation=participation,
            joint_weights=tuple(
                (row.player_id, row.game_id, w)
                for row, w in zip(pool.samples, weights, strict=True)
            ),
            excluded_games=self.pools.excluded_game_keys,
            history_fingerprint=self.history_fingerprint,
        )
