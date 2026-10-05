"""Versioned engineering defaults for the opening-week shadow candidate."""

import json
from dataclasses import asdict, dataclass
from hashlib import sha256
from math import isfinite

CORE_STATS = ("pts", "reb", "ast", "stl", "blk", "to", "tpm")
STAT_FIELDS = (
    "points",
    "rebounds",
    "assists",
    "steals",
    "blocks",
    "turnovers",
    "three_pointers_made",
)
OUTCOME_FIELDS = (*STAT_FIELDS, "technical_fouls", "flagrant_fouls")


class HybridProjectionError(ValueError):
    """An invalid evidence boundary or an unsupported candidate input."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class HybridProjectionConfig:
    """Changing a default changes model identity, including internal-only mode."""

    use_external: bool = True
    recency_half_life_days: float = 14.0
    previous_season_strength: float = 6.0
    older_season_strength: float = 3.0
    donor_strength: float = 5.0
    donor_count: int = 20
    minimum_donor_games: int = 10
    minimum_donors: int = 5
    forecast_max_age_hours: float = 48.0
    minimum_effective_sample: float = 20.0
    maximum_game_weight: float = 0.1
    fit_iterations: int = 200
    relative_mean_tolerance: float = 0.1
    mean_tolerance_floors: tuple[float, ...] = (1.0, 0.5, 0.5, 0.15, 0.15, 0.25, 0.25)
    profile_scale_floors: tuple[float, ...] = (1.0, 1.0, 1.0, 0.25, 0.25, 0.25, 0.25)
    participation_prior_strength: float = 20.0
    availability_max_age_minutes: float = 15.0

    def __post_init__(self) -> None:
        """Reject invalid weights and bounds before they reach numerical code."""
        for name, value in asdict(self).items():
            if name == "use_external":
                if not isinstance(value, bool):
                    raise HybridProjectionError("invalid_external_mode")
                continue
            values = value if isinstance(value, tuple) else (value,)
            if any(
                isinstance(item, bool)
                or not isinstance(item, int | float)
                or not isfinite(item)
                or item <= 0
                for item in values
            ):
                raise HybridProjectionError(f"invalid_config:{name}")
        for name in ("donor_count", "minimum_donor_games", "minimum_donors", "fit_iterations"):
            if not isinstance(getattr(self, name), int):
                raise HybridProjectionError(f"invalid_config:{name}")
        if self.minimum_donors > self.donor_count or self.maximum_game_weight > 1:
            raise HybridProjectionError("invalid_config_bounds")
        if len(self.mean_tolerance_floors) != 7 or len(self.profile_scale_floors) != 7:
            raise HybridProjectionError("invalid_stat_floors")

    @property
    def model_version(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return f"opening-week-hybrid-v1-{sha256(payload.encode()).hexdigest()[:12]}"
