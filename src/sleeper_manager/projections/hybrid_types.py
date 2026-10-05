"""Explicit coverage and resolved opportunities for joint-stat projection inputs.

Callers must attest to source field coverage and supply enumerated opportunities;
normalized box-score defaults cannot prove either kind of evidence.
"""

from dataclasses import dataclass
from datetime import datetime
from math import isfinite

from sleeper_manager.domain.nba import AvailabilityStatus
from sleeper_manager.domain.scoring import BoxScoreLine
from sleeper_manager.integrations.nba.historical_feature_models import AvailabilityObservation
from sleeper_manager.projections.hybrid_config import (
    OUTCOME_FIELDS,
    STAT_FIELDS,
    HybridProjectionError,
)


def require_aware(value: datetime) -> None:
    """Reject naive cutoffs and evidence timestamps."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise HybridProjectionError("naive_timestamp")


@dataclass(frozen=True, slots=True)
class JointGameObservation:
    """One resolved regular-season game with explicitly verified outcome fields."""

    player_id: str
    game_id: str
    game_start: datetime
    finalized_at: datetime
    did_play: bool
    minutes: float | None
    line: BoxScoreLine
    verified_fields: tuple[str, ...]
    source_version: str

    def __post_init__(self) -> None:
        """Reject malformed evidence rather than silently treating it as no history."""
        require_aware(self.game_start)
        require_aware(self.finalized_at)
        if (
            not self.player_id.strip()
            or not self.game_id.strip()
            or not self.source_version.strip()
        ):
            raise HybridProjectionError("missing_outcome_identity")
        if self.finalized_at < self.game_start or not isinstance(self.did_play, bool):
            raise HybridProjectionError("invalid_outcome_time_or_participation")
        if self.minutes is not None and (
            isinstance(self.minutes, bool) or not isfinite(self.minutes) or self.minutes < 0
        ):
            raise HybridProjectionError("invalid_minutes")
        if set(self.verified_fields) - set(OUTCOME_FIELDS):
            raise HybridProjectionError("unknown_outcome_field")
        object.__setattr__(self, "verified_fields", tuple(sorted(set(self.verified_fields))))
        for name in OUTCOME_FIELDS:
            value = getattr(self.line, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise HybridProjectionError("invalid_box_score")
        if {"points", "three_pointers_made"}.issubset(self.verified_fields):
            if self.line.points < 3 * self.line.three_pointers_made:
                raise HybridProjectionError("incoherent_box_score")

    @property
    def core_stats(self) -> tuple[float, ...]:
        return tuple(float(getattr(self.line, name)) for name in STAT_FIELDS)


@dataclass(frozen=True, slots=True)
class GameAvailability:
    """An observation tied to a specific game, not an indefinite injury designation."""

    game_id: str
    observed_at: datetime
    status: AvailabilityStatus
    observation: AvailabilityObservation
    source_version: str

    def __post_init__(self) -> None:
        require_aware(self.observed_at)
        if not self.game_id.strip() or not self.source_version.strip():
            raise HybridProjectionError("missing_availability_identity")

    @property
    def bucket(self) -> str:
        if self.observation is AvailabilityObservation.REPORTED:
            return f"reported:{self.status.value}"
        return self.observation.value


@dataclass(frozen=True, slots=True)
class ParticipationOpportunity:
    """A census-derived scheduled opportunity with a resolved played or DNP outcome."""

    player_id: str
    game_id: str
    game_start: datetime
    finalized_at: datetime
    did_play: bool
    source_version: str
    availability: GameAvailability | None = None

    def __post_init__(self) -> None:
        require_aware(self.game_start)
        require_aware(self.finalized_at)
        if (
            not self.player_id.strip()
            or not self.game_id.strip()
            or not self.source_version.strip()
        ):
            raise HybridProjectionError("missing_opportunity_identity")
        if self.finalized_at < self.game_start or not isinstance(self.did_play, bool):
            raise HybridProjectionError("invalid_opportunity")
        if self.availability is not None and (
            self.availability.game_id != self.game_id
            or self.availability.observed_at >= self.game_start
        ):
            raise HybridProjectionError("invalid_opportunity_availability")


@dataclass(frozen=True, slots=True)
class HybridHistory:
    """Immutable source-versioned evidence; model code filters every read by cutoff."""

    dataset_version: str
    observations: tuple[JointGameObservation, ...]
    opportunities: tuple[ParticipationOpportunity, ...]

    def __post_init__(self) -> None:
        if not self.dataset_version.strip():
            raise HybridProjectionError("missing_dataset_version")
        for records in (self.observations, self.opportunities):
            keys = [(row.player_id, row.game_id) for row in records]
            if len(set(keys)) != len(keys):
                raise HybridProjectionError("duplicate_player_game")
        outcomes = {(row.player_id, row.game_id): row.did_play for row in self.observations}
        if any(
            (row.player_id, row.game_id) in outcomes
            and outcomes[row.player_id, row.game_id] != row.did_play
            for row in self.opportunities
        ):
            raise HybridProjectionError("contradictory_participation")
