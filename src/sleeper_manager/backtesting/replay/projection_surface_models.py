"""Immutable contracts for decision-cutoff historical projection evidence.

These records keep model outputs separate from historical team-week facts. The
builder and strict artifact boundary live in ``projection_surface``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Any

from sleeper_manager.backtesting.artifacts import canonical_json, canonicalize, sha256_text
from sleeper_manager.backtesting.replay.inputs.models import SourceFingerprint
from sleeper_manager.domain.projection import ProjectionSnapshot

PROJECTION_SURFACE_SCHEMA_VERSION = "historical-projection-surface-v1"
FULL_ADVISOR_CUTOFF_SCHEDULE_VERSION = "full-advisor-tipoff-cutoffs-v1"


class HistoricalProjectionSurfaceError(ValueError):
    """Report corrupt, incomplete, or incompatible projection-surface evidence."""


@dataclass(frozen=True, slots=True)
class ProjectionSurfaceEntry:
    """Retain either one cutoff projection or its explicit generation failure."""

    decision_time: datetime
    player_id: str
    game_id: str
    projection: ProjectionSnapshot | None = None
    failure_reason: str | None = None
    failure_detail: str | None = None

    def __post_init__(self) -> None:
        """Reject ambiguous outcomes and projections from another logical key."""

        if self.decision_time.tzinfo is None:
            raise HistoricalProjectionSurfaceError("Surface decision times must be aware")
        if not self.player_id.strip() or not self.game_id.strip():
            raise HistoricalProjectionSurfaceError("Surface player and game IDs are required")
        succeeded = self.projection is not None
        failed = self.failure_reason is not None or self.failure_detail is not None
        if succeeded == failed:
            raise HistoricalProjectionSurfaceError(
                "Surface entries require exactly one projection or failure"
            )
        if self.projection is not None:
            if (
                self.projection.player_id != self.player_id
                or self.projection.game_id != self.game_id
            ):
                raise HistoricalProjectionSurfaceError(
                    "Surface projection identity disagrees with its entry"
                )
            if self.projection.available_as_of != self.decision_time:
                raise HistoricalProjectionSurfaceError(
                    "Surface projection availability must equal its decision time"
                )
        elif not (self.failure_reason or "").strip() or not (self.failure_detail or "").strip():
            raise HistoricalProjectionSurfaceError(
                "Surface failures require non-empty reason and detail"
            )

    @property
    def key(self) -> tuple[datetime, str, str]:
        """Return the stable cutoff/player/game identity."""

        return self.decision_time, self.player_id, self.game_id


@dataclass(frozen=True, slots=True)
class HistoricalProjectionSurface:
    """Bind cutoff-specific model attempts to one immutable historical team-week."""

    team_week_manifest_id: str
    league_id: str
    season: str
    week: int
    roster_id: int
    team_week_fingerprint: str
    projection_config_version: str
    scoring_policy_version: str
    source_fingerprints: tuple[SourceFingerprint, ...]
    entries: tuple[ProjectionSurfaceEntry, ...]
    planning_lead_time_seconds: float = 600.0
    finalization_policy_version: str = "approximate-next-eastern-day-0600-v1"
    cutoff_schedule_version: str = FULL_ADVISOR_CUTOFF_SCHEDULE_VERSION
    schema_version: str = PROJECTION_SURFACE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        """Normalize ordering and reject ambiguous surface-level identities."""

        for value in (
            self.team_week_manifest_id,
            self.league_id,
            self.season,
            self.team_week_fingerprint,
            self.projection_config_version,
            self.scoring_policy_version,
            self.finalization_policy_version,
            self.cutoff_schedule_version,
        ):
            if not value.strip():
                raise HistoricalProjectionSurfaceError("Surface identities must be non-empty")
        if self.week <= 0 or self.roster_id <= 0:
            raise HistoricalProjectionSurfaceError("Surface week and roster IDs must be positive")
        if not isfinite(self.planning_lead_time_seconds) or self.planning_lead_time_seconds < 0:
            raise HistoricalProjectionSurfaceError("Surface planning lead time must be finite")
        if self.schema_version != PROJECTION_SURFACE_SCHEMA_VERSION:
            raise HistoricalProjectionSurfaceError("Unsupported projection-surface schema")
        sources = tuple(sorted(self.source_fingerprints, key=lambda item: item.name))
        if len({item.name for item in sources}) != len(sources):
            raise HistoricalProjectionSurfaceError("Surface source names must be unique")
        entries = tuple(sorted(self.entries, key=lambda item: item.key))
        if len({item.key for item in entries}) != len(entries):
            raise HistoricalProjectionSurfaceError("Surface entry keys must be unique")
        object.__setattr__(self, "source_fingerprints", sources)
        object.__setattr__(self, "entries", entries)

    @property
    def fingerprint(self) -> str:
        """Return the stable identity of the complete surface payload."""

        return sha256_text(canonical_json(self))

    def to_dict(self) -> dict[str, Any]:
        """Return canonical JSON-compatible evidence with its verified identity."""

        payload = canonicalize(self)
        assert isinstance(payload, dict)
        payload["fingerprint"] = self.fingerprint
        return payload


__all__ = (
    "FULL_ADVISOR_CUTOFF_SCHEDULE_VERSION",
    "HistoricalProjectionSurface",
    "HistoricalProjectionSurfaceError",
    "PROJECTION_SURFACE_SCHEMA_VERSION",
    "ProjectionSurfaceEntry",
)
