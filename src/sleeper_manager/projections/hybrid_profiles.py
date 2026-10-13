"""Donor selection over compact profiles, independent of loading joint outcome rows."""

from dataclasses import dataclass
from math import fsum, isfinite
from statistics import pstdev

from sleeper_manager.projections.hybrid_config import HybridProjectionConfig, HybridProjectionError


@dataclass(frozen=True, slots=True)
class PlayerStatProfile:
    """Sufficient donor-selection evidence; outcomes remain separately addressable."""

    player_id: str
    count: int
    strength: float
    means: tuple[float, ...]


class StatProfilePopulation:
    """Keep the complete donor population while fetching outcomes only for selected donors."""

    def __init__(
        self, profiles: tuple[PlayerStatProfile, ...], config: HybridProjectionConfig
    ) -> None:
        self.config = config
        self.profiles = {row.player_id: row for row in profiles}
        eligible = [row.means for row in profiles if row.count >= config.minimum_donor_games]
        self.scales = tuple(
            max(pstdev([row[k] for row in eligible]), floor) if eligible else floor
            for k, floor in enumerate(config.profile_scale_floors)
        )

    def donors(self, player_id: str, center: tuple[float, ...] | None = None) -> tuple[str, ...]:
        """Reproduce tolerant-fit and internal donor lookup without reading any outcome payloads."""
        own = self.profiles.get(player_id)
        others = [
            row
            for player, row in self.profiles.items()
            if player != player_id and row.count >= self.config.minimum_donor_games
        ]
        if center is None:
            if not others and own is None:
                raise HybridProjectionError("missing_stat_history")
            if others:
                broad = tuple(fsum(row.means[k] for row in others) / len(others) for k in range(7))
            else:
                assert own is not None
                broad = own.means
            if own is not None:
                center = tuple(
                    (own.means[k] * own.strength + broad[k] * self.config.donor_strength)
                    / (own.strength + self.config.donor_strength)
                    for k in range(7)
                )
            else:
                center = broad
        if len(center) != 7 or any(not isfinite(value) or value < 0 for value in center):
            raise HybridProjectionError("invalid_center_dimension")
        selected = tuple(
            row.player_id
            for row in sorted(
                others,
                key=lambda row: (
                    fsum(((row.means[k] - center[k]) / self.scales[k]) ** 2 for k in range(7)),
                    row.player_id,
                ),
            )[: self.config.donor_count]
        )
        return selected if len(selected) >= self.config.minimum_donors else ()
