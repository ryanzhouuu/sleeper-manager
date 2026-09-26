"""Point-in-time roster and schedule evidence linked to one forecast receipt.

The snapshot is what collection knew when the slot was persisted. Later reads
must not rebuild it from current rosters.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sleeper_manager.domain._forecast_capture_validation import (
    ForecastCaptureError,
    require_aware,
    require_text,
)
from sleeper_manager.domain.nba import GameStatus


class ForecastContextGapCode(StrEnum):
    """Names a companion source that could not be stored completely."""

    MATCHUP = "matchup"
    ROSTER = "roster"
    ELIGIBILITY = "eligibility"
    SCHEDULE = "schedule"
    IDENTITY = "identity"


@dataclass(frozen=True, slots=True)
class ForecastContextRoster:
    """One fantasy roster as read for a capture slot."""

    roster_id: int
    player_ids: tuple[str, ...]
    starter_ids: tuple[str | None, ...]
    reserve_ids: tuple[str, ...]
    read_at: datetime

    def __post_init__(self) -> None:
        if isinstance(self.roster_id, bool) or not isinstance(self.roster_id, int):
            raise ForecastCaptureError("Forecast context roster ID must be an integer")
        if self.roster_id <= 0:
            raise ForecastCaptureError("Forecast context roster ID must be positive")
        object.__setattr__(self, "player_ids", _player_ids(self.player_ids, "roster players"))
        object.__setattr__(
            self,
            "starter_ids",
            _optional_player_ids(self.starter_ids, "starter"),
        )
        object.__setattr__(self, "reserve_ids", _player_ids(self.reserve_ids, "reserve players"))
        require_aware(self.read_at, "Forecast context roster read")


@dataclass(frozen=True, slots=True)
class ForecastContextEligibility:
    """Fantasy positions known for one rostered player at a catalog read."""

    player_id: str
    positions: tuple[str, ...]
    read_at: datetime

    def __post_init__(self) -> None:
        require_text(self.player_id, "Forecast context eligibility player")
        positions: list[str] = []
        for position in self.positions:
            require_text(position, "Forecast context eligibility position")
            cleaned = position.strip()
            if cleaned in positions:
                raise ForecastCaptureError(f"Forecast context position {cleaned!r} is duplicated")
            positions.append(cleaned)
        object.__setattr__(self, "positions", tuple(positions))
        require_aware(self.read_at, "Forecast context eligibility read")


@dataclass(frozen=True, slots=True)
class ForecastContextGame:
    """One fantasy-week game observed for a rostered player's team."""

    game_id: str
    home_team_id: str
    away_team_id: str
    start_time: datetime
    status: GameStatus
    read_at: datetime

    def __post_init__(self) -> None:
        require_text(self.game_id, "Forecast context game ID")
        require_text(self.home_team_id, "Forecast context home team")
        require_text(self.away_team_id, "Forecast context away team")
        require_aware(self.start_time, "Forecast context game start")
        require_aware(self.read_at, "Forecast context game read")
        if not isinstance(self.status, GameStatus):
            raise ForecastCaptureError("Forecast context game status is unsupported")


@dataclass(frozen=True, slots=True)
class ForecastContextGap:
    """Records one missing companion source without inventing its contents."""

    code: ForecastContextGapCode
    subject: str
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, ForecastContextGapCode):
            raise ForecastCaptureError("Forecast context gap code is unsupported")
        require_text(self.subject, "Forecast context gap subject")
        require_text(self.detail, "Forecast context gap detail")


@dataclass(frozen=True, slots=True)
class ForecastCaptureContext:
    """Immutable companion evidence for the receipt that observed it."""

    receipt_id: str
    league_id: str
    season: str
    week: int
    matchup_id: int | None
    bye: bool
    manager: ForecastContextRoster | None
    opponent: ForecastContextRoster | None
    eligibility: tuple[ForecastContextEligibility, ...]
    games: tuple[ForecastContextGame, ...]
    gaps: tuple[ForecastContextGap, ...]
    persisted_at: datetime

    def __post_init__(self) -> None:
        require_text(self.receipt_id, "Forecast context receipt ID")
        require_text(self.league_id, "Forecast context league ID")
        require_text(self.season, "Forecast context season")
        if isinstance(self.week, bool) or not isinstance(self.week, int) or self.week <= 0:
            raise ForecastCaptureError("Forecast context week must be positive")
        if self.matchup_id is not None and (
            isinstance(self.matchup_id, bool)
            or not isinstance(self.matchup_id, int)
            or self.matchup_id <= 0
        ):
            raise ForecastCaptureError("Forecast context matchup ID must be positive")
        if not isinstance(self.bye, bool):
            raise ForecastCaptureError("Forecast context bye flag must be boolean")
        require_aware(self.persisted_at, "Forecast context persistence time")
        if self.bye and (self.opponent is not None or self.matchup_id is not None):
            raise ForecastCaptureError("Forecast context bye cannot name an opponent")
        if (
            not self.bye
            and self.matchup_id is None
            and not _has_gap(self.gaps, ForecastContextGapCode.MATCHUP)
        ):
            raise ForecastCaptureError("Forecast context matchup gap must be explicit")
        if self.manager is None and not _has_gap(self.gaps, ForecastContextGapCode.ROSTER):
            raise ForecastCaptureError("Forecast context manager roster gap must be explicit")
        if (
            not self.bye
            and self.opponent is None
            and not _has_gap(self.gaps, ForecastContextGapCode.ROSTER)
            and not _has_gap(self.gaps, ForecastContextGapCode.MATCHUP)
        ):
            raise ForecastCaptureError("Forecast context opponent roster gap must be explicit")
        eligibility = tuple(sorted(self.eligibility, key=lambda item: item.player_id))
        if len({item.player_id for item in eligibility}) != len(eligibility):
            raise ForecastCaptureError("Forecast context eligibility repeats a player")
        games = tuple(sorted(self.games, key=lambda item: item.game_id))
        if len({item.game_id for item in games}) != len(games):
            raise ForecastCaptureError("Forecast context repeats a game")
        for read_at in _read_times(self.manager, self.opponent, eligibility, games):
            if read_at > self.persisted_at:
                raise ForecastCaptureError("Forecast context read follows its persistence")
        object.__setattr__(self, "eligibility", eligibility)
        object.__setattr__(self, "games", games)
        object.__setattr__(
            self,
            "gaps",
            tuple(sorted(self.gaps, key=lambda item: (item.code.value, item.subject, item.detail))),
        )


def _player_ids(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    cleaned: list[str] = []
    for value in values:
        require_text(value, f"Forecast context {label}")
        player_id = value.strip()
        if player_id in cleaned:
            raise ForecastCaptureError(f"Forecast context {label} repeat {player_id!r}")
        cleaned.append(player_id)
    return tuple(cleaned)


def _optional_player_ids(values: tuple[str | None, ...], label: str) -> tuple[str | None, ...]:
    cleaned: list[str | None] = []
    seen: set[str] = set()
    for value in values:
        if value is None:
            cleaned.append(None)
            continue
        require_text(value, f"Forecast context {label}")
        player_id = value.strip()
        if player_id in seen:
            raise ForecastCaptureError(f"Forecast context {label} repeats {player_id!r}")
        seen.add(player_id)
        cleaned.append(player_id)
    return tuple(cleaned)


def _has_gap(gaps: tuple[ForecastContextGap, ...], code: ForecastContextGapCode) -> bool:
    return any(gap.code is code for gap in gaps)


def _read_times(
    manager: ForecastContextRoster | None,
    opponent: ForecastContextRoster | None,
    eligibility: tuple[ForecastContextEligibility, ...],
    games: tuple[ForecastContextGame, ...],
) -> tuple[datetime, ...]:
    times: list[datetime] = []
    if manager is not None:
        times.append(manager.read_at)
    if opponent is not None:
        times.append(opponent.read_at)
    times.extend(item.read_at for item in eligibility)
    times.extend(item.read_at for item in games)
    return tuple(times)


__all__ = (
    "ForecastCaptureContext",
    "ForecastContextEligibility",
    "ForecastContextGame",
    "ForecastContextGap",
    "ForecastContextGapCode",
    "ForecastContextRoster",
)
