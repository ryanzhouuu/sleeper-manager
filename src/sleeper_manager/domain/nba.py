from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sleeper_manager.domain.scoring import BoxScoreLine


class DataQualityState(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    PARTIAL = "partial"
    EMPTY = "empty"
    UNRESOLVED = "unresolved"
    ERROR = "error"


class GameStatus(StrEnum):
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    FINAL = "final"
    POSTPONED = "postponed"
    CANCELED = "canceled"
    UNKNOWN = "unknown"


class AvailabilityStatus(StrEnum):
    AVAILABLE = "available"
    PROBABLE = "probable"
    QUESTIONABLE = "questionable"
    DOUBTFUL = "doubtful"
    OUT = "out"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SourceMetadata:
    provider: str
    provider_id: str
    retrieved_at: datetime
    source_updated_at: datetime | None = None
    schema_version: str = "1"
    content_hash: str | None = None


@dataclass(frozen=True, slots=True)
class Team:
    provider_id: str
    abbreviation: str
    name: str
    location: str | None
    source: SourceMetadata


@dataclass(frozen=True, slots=True)
class ProviderPlayer:
    provider_id: str
    full_name: str
    team_id: str | None
    team_abbreviation: str | None
    active: bool | None
    source: SourceMetadata


@dataclass(frozen=True, slots=True)
class ScheduledGame:
    provider_id: str
    start_time: datetime
    status: GameStatus
    home_team_id: str
    away_team_id: str
    status_detail: str | None
    source: SourceMetadata
    venue_id: str | None = None
    venue_name: str | None = None
    venue_city: str | None = None
    venue_state: str | None = None
    neutral_site: bool = False
    regulation_periods: int = 4
    completed_periods: int = 4
    finalized_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.finalized_at is None:
            return
        if self.finalized_at.tzinfo is None:
            raise ValueError("Game finalization must be timezone-aware")
        if self.finalized_at < self.start_time:
            raise ValueError("Game finalization cannot precede its start")
        if self.status is not GameStatus.FINAL:
            raise ValueError("Only final games may have a finalization timestamp")

    @property
    def duration_minutes(self) -> int:
        return 12 * self.regulation_periods + 5 * max(
            self.completed_periods - self.regulation_periods, 0
        )


@dataclass(frozen=True, slots=True)
class PlayerAvailability:
    player_id: str
    status: AvailabilityStatus
    detail: str | None
    source: SourceMetadata


@dataclass(frozen=True, slots=True)
class PlayerBoxScore:
    game_id: str
    player_id: str
    team_id: str
    played_at: datetime | None
    started: bool
    did_play: bool
    minutes: float | None
    line: BoxScoreLine
    source: SourceMetadata
    additional_sources: tuple[SourceMetadata, ...] = ()


@dataclass(frozen=True, slots=True)
class TeamBoxScore:
    game_id: str
    team_id: str
    opponent_team_id: str
    played_at: datetime
    points: int
    opponent_points: int
    field_goal_attempts: int
    free_throw_attempts: int
    offensive_rebounds: int
    turnovers: int
    source: SourceMetadata
    regulation_periods: int = 4
    completed_periods: int = 4

    @property
    def estimated_possessions(self) -> float:
        return (
            self.field_goal_attempts
            + 0.44 * self.free_throw_attempts
            - self.offensive_rebounds
            + self.turnovers
        )

    @property
    def duration_minutes(self) -> int:
        return 12 * self.regulation_periods + 5 * max(
            self.completed_periods - self.regulation_periods, 0
        )

    @property
    def pace_48(self) -> float:
        duration = self.duration_minutes
        if duration <= 0:
            raise ValueError("Team box-score duration must be positive")
        return self.estimated_possessions * 48 / duration


@dataclass(frozen=True, slots=True)
class PlayerGameFouls:
    game_id: str
    player_id: str
    technical_fouls: int
    flagrant_fouls: int
    source: SourceMetadata


@dataclass(frozen=True, slots=True)
class GameSummary:
    game: ScheduledGame
    player_box_scores: tuple[PlayerBoxScore, ...]


@dataclass(frozen=True, slots=True)
class DataQualityReport:
    state: DataQualityState
    resource: str
    record_count: int
    retrieved_at: datetime
    source_updated_at: datetime | None
    expires_at: datetime | None
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProviderResult[RecordT]:
    records: RecordT
    quality: DataQualityReport


def quality_for_records(
    *,
    resource: str,
    records: Sequence[object],
    retrieved_at: datetime,
    source_updated_at: datetime | None = None,
    expires_at: datetime | None = None,
    warnings: tuple[str, ...] = (),
) -> DataQualityReport:
    state = DataQualityState.FRESH if records else DataQualityState.EMPTY
    if warnings and state is DataQualityState.FRESH:
        state = DataQualityState.PARTIAL
    return DataQualityReport(
        state=state,
        resource=resource,
        record_count=len(records),
        retrieved_at=retrieved_at,
        source_updated_at=source_updated_at,
        expires_at=expires_at,
        warnings=warnings,
    )
