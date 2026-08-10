from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sleeper_manager.domain.models import Roster
from sleeper_manager.domain.scoring import ScoringPolicy


class LeagueMode(StrEnum):
    LOCK_IN = "lock_in"


@dataclass(frozen=True, slots=True)
class RosterSlot:
    index: int
    position: str
    is_starting: bool


@dataclass(frozen=True, slots=True)
class LeagueUser:
    sleeper_id: str
    username: str | None
    display_name: str | None
    team_name: str | None


@dataclass(frozen=True, slots=True)
class FantasyWeek:
    week: int
    season: str
    season_type: str


@dataclass(frozen=True, slots=True)
class LeagueTransaction:
    transaction_id: str
    transaction_type: str
    status: str
    leg: int | None
    roster_ids: tuple[int, ...]
    adds: tuple[tuple[str, int], ...]
    drops: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class LeagueProfile:
    league_id: str
    name: str
    sport: str
    season: str
    season_type: str
    status: str
    total_rosters: int
    previous_league_id: str | None
    mode: LeagueMode
    roster_slots: tuple[RosterSlot, ...]
    scoring: ScoringPolicy
    users: tuple[LeagueUser, ...]
    rosters: tuple[Roster, ...]
    manager_user_id: str
    manager_roster_id: int
    fantasy_week: FantasyWeek
    transactions: tuple[LeagueTransaction, ...]
    configuration_fingerprint: str
    retrieved_at: datetime
