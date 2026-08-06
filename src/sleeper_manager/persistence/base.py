from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sleeper_manager.domain.nba import DataQualityState


@dataclass(frozen=True, slots=True)
class StoredLeagueProfile:
    league_id: str
    fingerprint: str
    retrieved_at: datetime


class LeagueProfileStore(Protocol):
    def load_profile(self, league_id: str) -> StoredLeagueProfile | None: ...

    def save_profile(self, profile: StoredLeagueProfile) -> None: ...


@dataclass(frozen=True, slots=True)
class CachedNBARecord:
    cache_key: str
    provider: str
    resource: str
    schema_version: str
    payload_json: str
    retrieved_at: datetime
    source_updated_at: datetime | None
    expires_at: datetime | None
    quality: DataQualityState
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


class NBADataCache(Protocol):
    def initialize(self) -> None: ...

    def get(self, cache_key: str, *, now: datetime) -> CachedNBARecord | None: ...

    def put(self, record: CachedNBARecord) -> None: ...


class StateRepository(Protocol):
    def initialize(self) -> None: ...

    def record_lock_acknowledgement(
        self,
        recommendation_id: str,
        player_id: str,
        acknowledged_at: datetime,
    ) -> None: ...

    def is_locked(self, recommendation_id: str) -> bool: ...
