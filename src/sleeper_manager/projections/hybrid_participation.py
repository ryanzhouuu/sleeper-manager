"""Provisional participation estimates from an explicit resolved opportunity census."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sleeper_manager.domain.nba_season import nba_season_start_year
from sleeper_manager.projections.hybrid_config import HybridProjectionConfig, HybridProjectionError
from sleeper_manager.projections.hybrid_types import (
    GameAvailability,
    HybridHistory,
    require_aware,
)


@dataclass(frozen=True, slots=True)
class ParticipationEstimate:
    probability: float
    history_probability: float
    own_count: int
    pooled_count: int
    own_bucket_count: int
    pooled_bucket_count: int
    bucket: str | None
    fallback: str | None
    availability: GameAvailability | None
    pool_kind: str = "global"
    pool_fallback: str | None = None
    pooled_player_count: int = 0


class HybridParticipation:
    """Prepare one cutoff's census separately from conditional played-stat samples."""

    def __init__(
        self,
        history: HybridHistory,
        *,
        cutoff: datetime,
        game_start: datetime,
        config: HybridProjectionConfig,
    ) -> None:
        require_aware(cutoff)
        require_aware(game_start)
        self.cutoff = cutoff
        self.config = config
        season = nba_season_start_year(game_start)
        self.opportunities = tuple(
            sorted(
                (
                    row
                    for row in history.opportunities
                    if row.finalized_at <= cutoff
                    and row.game_start < cutoff
                    and season - nba_season_start_year(row.game_start) in (0, 1)
                ),
                key=lambda row: (row.player_id, row.game_start, row.game_id),
            )
        )

    def estimate(
        self,
        player_id: str,
        *,
        game_id: str,
        availability: GameAvailability | None = None,
        donor_player_ids: tuple[str, ...] = (),
    ) -> ParticipationEstimate:
        """Use census-covered stat donors, with explicit global fallback for sparse coverage."""
        own = tuple(row for row in self.opportunities if row.player_id == player_id)
        pooled = tuple(row for row in self.opportunities if row.player_id != player_id)
        if not pooled:
            raise HybridProjectionError("missing_pooled_participation_opportunities")
        donor_ids = frozenset(donor_player_ids) - {player_id}
        role_pool = tuple(row for row in pooled if row.player_id in donor_ids)
        role_players = {row.player_id for row in role_pool}
        pool_kind = "global"
        pool_fallback: str | None = (
            "missing_role_donors" if not donor_ids else "insufficient_role_census"
        )
        if len(role_players) >= self.config.minimum_donors:
            pooled = role_pool
            pool_kind, pool_fallback = "stat_profile_donors", None
        pooled_rate = sum(row.did_play for row in pooled) / len(pooled)
        strength = self.config.participation_prior_strength
        history_probability = (sum(row.did_play for row in own) + strength * pooled_rate) / (
            len(own) + strength
        )
        fallback = self._availability_gap(availability, game_id)
        usable = availability if fallback is None else None
        bucket = usable.bucket if usable is not None else None
        own_bucket = tuple(
            row for row in own if row.availability and row.availability.bucket == bucket
        )
        pooled_bucket = tuple(
            row for row in pooled if row.availability and row.availability.bucket == bucket
        )
        probability = history_probability
        if bucket is not None:
            if usable is not None and usable.confirmed_absence:
                probability = 0.0
            elif not pooled_bucket:
                fallback = "missing_pooled_status_opportunities"
            else:
                bucket_prior = (
                    sum(row.did_play for row in pooled_bucket) + strength * pooled_rate
                ) / (len(pooled_bucket) + strength)
                probability = (
                    sum(row.did_play for row in own_bucket) + strength * bucket_prior
                ) / (len(own_bucket) + strength)
        return ParticipationEstimate(
            probability=probability,
            history_probability=history_probability,
            own_count=len(own),
            pooled_count=len(pooled),
            own_bucket_count=len(own_bucket),
            pooled_bucket_count=len(pooled_bucket),
            bucket=bucket,
            fallback=fallback,
            availability=usable,
            pool_kind=pool_kind,
            pool_fallback=pool_fallback,
            pooled_player_count=len({row.player_id for row in pooled}),
        )

    def _availability_gap(self, availability: GameAvailability | None, game_id: str) -> str | None:
        if availability is None:
            return "missing_game_availability"
        if availability.game_id != game_id:
            return "availability_for_other_game"
        if availability.observed_at > self.cutoff:
            return "future_availability"
        if self.cutoff - availability.observed_at > timedelta(
            minutes=self.config.availability_max_age_minutes
        ):
            return "stale_availability"
        return None
