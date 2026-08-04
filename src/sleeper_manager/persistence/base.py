from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class StoredLeagueProfile:
    league_id: str
    fingerprint: str
    retrieved_at: datetime


class LeagueProfileStore(Protocol):
    def load_profile(self, league_id: str) -> StoredLeagueProfile | None: ...

    def save_profile(self, profile: StoredLeagueProfile) -> None: ...


class StateRepository(Protocol):
    def initialize(self) -> None: ...

    def record_lock_acknowledgement(
        self,
        recommendation_id: str,
        player_id: str,
        acknowledged_at: datetime,
    ) -> None: ...

    def is_locked(self, recommendation_id: str) -> bool: ...
