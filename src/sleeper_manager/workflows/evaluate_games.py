from dataclasses import dataclass
from datetime import date
from typing import Any

from sleeper_manager.domain.nba import ProviderResult, ScheduledGame
from sleeper_manager.integrations.nba.base import NBAProvider
from sleeper_manager.integrations.sleeper.client import SleeperClient


@dataclass(frozen=True, slots=True)
class EvaluationSnapshot:
    league: dict[str, Any]
    rosters: list[dict[str, Any]]
    matchups: list[dict[str, Any]]
    scoreboard: ProviderResult[tuple[ScheduledGame, ...]]


class EvaluateGamesWorkflow:
    """Collects a consistent input snapshot before decision policies run."""

    def __init__(self, sleeper: SleeperClient, nba: NBAProvider) -> None:
        self._sleeper = sleeper
        self._nba = nba

    async def collect(
        self,
        *,
        league_id: str,
        week: int,
        game_date: date,
    ) -> EvaluationSnapshot:
        league = await self._sleeper.league(league_id)
        rosters = await self._sleeper.rosters(league_id)
        matchups = await self._sleeper.matchups(league_id, week)
        scoreboard = await self._nba.scoreboard(game_date)
        return EvaluationSnapshot(league, rosters, matchups, scoreboard)
