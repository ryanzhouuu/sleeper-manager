from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class Player:
    sleeper_id: str
    name: str
    team: str | None
    eligible_positions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Roster:
    roster_id: int
    owner_id: str | None
    player_ids: tuple[str, ...]
    starter_ids: tuple[str | None, ...]
    reserve_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GamePerformance:
    player_id: str
    game_id: str
    completed_at: datetime
    fantasy_points: float


class RecommendationKind(StrEnum):
    LOCK = "lock"
    PASS = "pass"
    START = "start"
    BENCH = "bench"
    ADD = "add"
    DROP = "drop"


@dataclass(frozen=True, slots=True)
class Recommendation:
    recommendation_id: str
    kind: RecommendationKind
    player_id: str
    message: str
    confidence: float
    deadline: datetime | None = None
