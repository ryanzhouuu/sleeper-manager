from datetime import date
from typing import Protocol

from sleeper_manager.domain.nba import (
    GameSummary,
    PlayerAvailability,
    ProviderPlayer,
    ProviderResult,
    ScheduledGame,
)


class NBAProvider(Protocol):
    async def scoreboard(self, game_date: date) -> ProviderResult[tuple[ScheduledGame, ...]]: ...

    async def game_summary(self, game_id: str) -> ProviderResult[GameSummary]: ...

    async def injuries(self) -> ProviderResult[tuple[PlayerAvailability, ...]]: ...

    async def team_roster(self, team_id: str) -> ProviderResult[tuple[ProviderPlayer, ...]]: ...

    async def team_schedule(
        self, team_id: str, season: int
    ) -> ProviderResult[tuple[ScheduledGame, ...]]: ...
