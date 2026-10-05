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
    ) -> ParticipationEstimate:
        """Shrink player counts toward other players; absent pooled evidence blocks output."""
        own = tuple(row for row in self.opportunities if row.player_id == player_id)
        pooled = tuple(row for row in self.opportunities if row.player_id != player_id)
        if not pooled:
            raise HybridProjectionError("missing_pooled_participation_opportunities")
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
            if not pooled_bucket:
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
