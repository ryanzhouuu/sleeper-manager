"""Score a matchup side the way Sleeper's lock-in mode does: only locked games count."""

import sys
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Protocol

from sleeper_manager.cloudflare.home_cache import read_home_cache, write_home_cache
from sleeper_manager.cloudflare.home_document import HomeSide
from sleeper_manager.cloudflare.providers import CloudflareProviderError
from sleeper_manager.cloudflare.sleeper_scoring import PlayerStatsReader
from sleeper_manager.domain.nba_season import nba_season_start_year
from sleeper_manager.domain.scheduling import manager_local_day
from sleeper_manager.domain.scoring import (
    ScoringCompatibilityError,
    ScoringPolicy,
    calculate_fantasy_points,
)
from sleeper_manager.integrations.sleeper.game_stats import (
    SleeperGameStatsError,
    box_score_line,
    game_stats_by_id,
)
from sleeper_manager.integrations.sleeper.matchup_legs import (
    MatchupLeg,
    MatchupLegsError,
    parse_matchup_legs,
)
from sleeper_manager.persistence.base import AsyncRuntimeStateRepository


@dataclass(frozen=True, slots=True)
class SideScore:
    """``pending`` means a locked game's stats are not posted yet, so ``points`` is unknown."""

    points: float | None
    final: bool
    locked: int
    starters: int
    pending: bool


class LockedGameCache(Protocol):
    """Stats for completed locked games, which rarely change once posted."""

    async def get(self, player_id: str, game_id: str) -> Mapping[str, Any] | None: ...

    async def put(self, player_id: str, game_id: str, stats: Mapping[str, Any]) -> None: ...


class MatchupLegReader(Protocol):
    async def matchup_legs(self, league_id: str, week: int) -> object: ...


class LiveScoreSleeper(MatchupLegReader, Protocol):
    async def player_game_stats(self, player_id: str, season: int) -> dict[str, Any]: ...


class HomeLockedGameCache:
    """Keep each locked game's stats for the manager-local day so corrections still land."""

    def __init__(
        self, repository: AsyncRuntimeStateRepository, *, timezone_name: str, now: datetime
    ) -> None:
        self._repository = repository
        self._timezone_name = timezone_name
        self._now = now
        self._day = manager_local_day(now, timezone_name=timezone_name).isoformat()

    def _key(self, player_id: str, game_id: str) -> str:
        return f"home-game-stats:{player_id}:{game_id}:{self._day}"

    async def get(self, player_id: str, game_id: str) -> Mapping[str, Any] | None:
        cached = await read_home_cache(self._repository, self._key(player_id, game_id), self._now)
        return cached if isinstance(cached, dict) and cached else None

    async def put(self, player_id: str, game_id: str, stats: Mapping[str, Any]) -> None:
        await write_home_cache(
            self._repository,
            self._key(player_id, game_id),
            dict(stats),
            resource="locked-game-stats",
            timezone_name=self._timezone_name,
            now=self._now,
        )


async def side_score(
    leg: MatchupLeg,
    *,
    read_stats: PlayerStatsReader,
    season: int,
    scoring: ScoringPolicy,
    cache: LockedGameCache | None = None,
) -> SideScore:
    locked = len(leg.starters_games)
    starters = len(leg.starters)
    if leg.points is not None:
        return SideScore(leg.points, True, locked, starters, False)
    total = 0.0
    for player_id, game_id in leg.starters_games.items():
        stats = None if cache is None else await cache.get(player_id, game_id)
        if stats is None:
            stats = game_stats_by_id(await read_stats(player_id, season), game_id)
            if stats and cache is not None:
                await cache.put(player_id, game_id, stats)
        if not stats:
            return SideScore(None, False, locked, starters, True)
        total += calculate_fantasy_points(box_score_line(stats), scoring)
    return SideScore(round(total, 2), False, locked, starters, False)


async def matchup_scores(
    reader: MatchupLegReader,
    league_id: str,
    week: int,
    manager_roster_id: int,
    *,
    season: int,
    scoring: ScoringPolicy,
    read_stats: PlayerStatsReader,
    cache: LockedGameCache | None = None,
) -> dict[int, SideScore] | None:
    """Score the manager and the opponent Sleeper pairs them with; None when locks are unknown."""
    try:
        legs = parse_matchup_legs(await reader.matchup_legs(league_id, week))
    except (CloudflareProviderError, MatchupLegsError) as error:
        print(f"Sleeper lock data unavailable: {type(error).__name__}", file=sys.stderr)
        return None
    mine = next((leg for leg in legs if leg.roster_id == manager_roster_id), None)
    if mine is None:
        return {}
    paired = [
        leg
        for leg in legs
        if leg is mine or (mine.matchup_id is not None and leg.matchup_id == mine.matchup_id)
    ]
    scores: dict[int, SideScore] = {}
    for leg in paired:
        try:
            scores[leg.roster_id] = await side_score(
                leg, read_stats=read_stats, season=season, scoring=scoring, cache=cache
            )
        except (CloudflareProviderError, SleeperGameStatsError):
            scores[leg.roster_id] = SideScore(
                None, False, len(leg.starters_games), len(leg.starters), True
            )
    return scores


async def home_matchup_scores(
    sleeper: LiveScoreSleeper,
    repository: AsyncRuntimeStateRepository,
    scoring_settings: Mapping[str, Any],
    league_id: str,
    week: int,
    roster_id: int,
    *,
    timezone_name: str,
    now: datetime,
) -> dict[int, SideScore] | None:
    """Live scores for the home page, caching locked-game stats for the local day."""
    try:
        scoring = ScoringPolicy.from_sleeper(scoring_settings)
    except (ScoringCompatibilityError, ValueError):
        return None
    return await matchup_scores(
        sleeper,
        league_id,
        week,
        roster_id,
        season=nba_season_start_year(manager_local_day(now, timezone_name=timezone_name)),
        scoring=scoring,
        read_stats=sleeper.player_game_stats,
        cache=HomeLockedGameCache(repository, timezone_name=timezone_name, now=now),
    )


def scored_sides(
    manager: HomeSide,
    opponent: HomeSide | None,
    scores: Mapping[int, SideScore] | None,
    manager_roster_id: int,
) -> tuple[HomeSide, HomeSide | None]:
    """Replace both displayed scores; Sleeper's public matchup points ignore lock-in."""
    mine = None if scores is None else scores.get(manager_roster_id)
    theirs = (
        None
        if scores is None
        else next((score for rid, score in scores.items() if rid != manager_roster_id), None)
    )
    return _with_score(manager, mine), None if opponent is None else _with_score(opponent, theirs)


def _with_score(side: HomeSide, score: SideScore | None) -> HomeSide:
    if score is None:
        return replace(
            side, score=None, score_final=False, locked=None, starters=None, lock_data="unavailable"
        )
    return replace(
        side,
        score=score.points,
        score_final=score.final,
        locked=score.locked,
        starters=score.starters,
        lock_data="pending" if score.pending else "ready",
    )
