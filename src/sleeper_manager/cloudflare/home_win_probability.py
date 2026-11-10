"""The dashboard's chance that the manager outscores this week's opponent.

It combines the plan's simulated finishes, today's opponent outlook, and Sleeper's live locks.
Any missing piece hides the number instead of guessing. A started game counts as final once
``FINAL_AFTER`` has passed since tipoff; until then its pregame projection stands in.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sleeper_manager.cloudflare.live_score import (
    HomeLockedGameCache,
    LockedGame,
    LockedGameCache,
    SideScore,
)
from sleeper_manager.cloudflare.opponent_outlook import (
    OpponentGame,
    OpponentOutlook,
    load_opponent_outlook,
)
from sleeper_manager.cloudflare.providers import CloudflareProviderError
from sleeper_manager.cloudflare.sleeper_scoring import PlayerStatsReader
from sleeper_manager.decisions.win_probability import (
    Finish,
    starter_finish,
    team_finish,
    win_probability,
)
from sleeper_manager.domain.nba_season import nba_season_start_year
from sleeper_manager.domain.scheduling import EASTERN_TIMEZONE, manager_local_day
from sleeper_manager.domain.scoring import (
    ScoringCompatibilityError,
    ScoringPolicy,
    calculate_fantasy_points,
)
from sleeper_manager.integrations.sleeper.game_stats import (
    SleeperGameStatsError,
    box_score_line,
    player_game_stats,
)
from sleeper_manager.persistence.base import AsyncRuntimeStateRepository

FINAL_AFTER = timedelta(hours=3, minutes=30)
_EMPTY_SLOT = "0"


async def dashboard_win_probability(
    repository: AsyncRuntimeStateRepository,
    league_id: str,
    scoring_settings: Mapping[str, Any],
    plan_trace: Mapping[str, Any] | None,
    scores: Mapping[int, SideScore] | None,
    manager_roster_id: int,
    *,
    read_stats: PlayerStatsReader,
    week: int,
    timezone_name: str,
    now: datetime,
) -> float | None:
    """Skip the outlook read unless a plan and the opponent's live locks are both present."""
    opponent = next((item for item in (scores or {}).items() if item[0] != manager_roster_id), None)
    if plan_trace is None or opponent is None:
        return None
    try:
        scoring = ScoringPolicy.from_sleeper(scoring_settings)
    except (ScoringCompatibilityError, ValueError):
        return None
    return await matchup_win_probability(
        plan_trace,
        await load_opponent_outlook(repository, league_id, week, now=now),
        opponent[0],
        opponent[1],
        read_stats=read_stats,
        season=nba_season_start_year(manager_local_day(now, timezone_name=timezone_name)),
        scoring=scoring,
        cache=HomeLockedGameCache(repository, timezone_name=timezone_name, now=now),
        now=now,
    )


async def matchup_win_probability(
    plan_trace: Mapping[str, Any] | None,
    outlook: OpponentOutlook | None,
    opponent_roster_id: int | None,
    opponent: SideScore | None,
    *,
    read_stats: PlayerStatsReader,
    season: int,
    scoring: ScoringPolicy,
    cache: LockedGameCache | None,
    now: datetime,
) -> float | None:
    samples = _samples(plan_trace)
    if not samples:
        return None
    finish = await opponent_finish(
        outlook,
        opponent_roster_id,
        opponent,
        read_stats=read_stats,
        season=season,
        scoring=scoring,
        cache=cache,
        now=now,
    )
    return None if finish is None else win_probability(samples, finish)


async def opponent_finish(
    outlook: OpponentOutlook | None,
    opponent_roster_id: int | None,
    opponent: SideScore | None,
    *,
    read_stats: PlayerStatsReader,
    season: int,
    scoring: ScoringPolicy,
    cache: LockedGameCache | None,
    now: datetime,
) -> Finish | None:
    """The opponent's week score so far plus every unlocked starter; None when any is unknown."""
    if opponent is None or opponent.points is None or opponent.final:
        return None
    if outlook is None or outlook.roster_id != opponent_roster_id:
        return None
    finishes: list[Finish] = []
    try:
        for player_id in opponent.starter_ids:
            if player_id == _EMPTY_SLOT or player_id in opponent.locked_ids:
                continue
            games = outlook.players.get(player_id)
            finish = (
                None
                if games is None
                else await _starter(player_id, games, now, read_stats, season, scoring, cache)
            )
            if finish is None:
                return None
            finishes.append(finish)
    except (CloudflareProviderError, SleeperGameStatsError):
        return None
    return team_finish(opponent.points, finishes)


async def _starter(
    player_id: str,
    games: Sequence[OpponentGame],
    now: datetime,
    read_stats: PlayerStatsReader,
    season: int,
    scoring: ScoringPolicy,
    cache: LockedGameCache | None,
) -> Finish | None:
    """None when a needed score or projection is unknown."""
    played = [game for game in games if game.start <= now]
    ahead = [game for game in games if game.start > now]
    lockable = None
    if played and now - played[-1].start >= FINAL_AFTER:
        lockable = await _final_score(player_id, played[-1], read_stats, season, scoring, cache)
        if lockable is None:
            return None
    elif played:
        ahead.insert(0, played[-1])
    outcomes = [game.outcomes for game in ahead]
    if any(item is None for item in outcomes):
        return None
    return starter_finish([item for item in outcomes if item is not None], lockable)


async def _final_score(
    player_id: str,
    game: OpponentGame,
    read_stats: PlayerStatsReader,
    season: int,
    scoring: ScoringPolicy,
    cache: LockedGameCache | None,
) -> float | None:
    """Sleeper's empty stats for a finished game mean the player did not play."""
    game_date = game.start.astimezone(ZoneInfo(EASTERN_TIMEZONE)).date()
    key = f"date:{game_date.isoformat()}"
    cached = None if cache is None else await cache.get(player_id, key)
    if cached is not None:
        stats = cached.stats
    else:
        found = player_game_stats(await read_stats(player_id, season), game_date)
        if found is None:
            return None
        stats = found
        if stats and cache is not None:
            await cache.put(player_id, key, LockedGame(stats, game_date))
    return calculate_fantasy_points(box_score_line(stats), scoring)


def _samples(plan_trace: Mapping[str, Any] | None) -> tuple[float, ...]:
    raw = (plan_trace or {}).get("terminal_score_samples")
    if not isinstance(raw, list):
        return ()
    if not all(isinstance(value, int | float) and not isinstance(value, bool) for value in raw):
        return ()
    return tuple(float(value) for value in raw)


__all__ = (
    "FINAL_AFTER",
    "dashboard_win_probability",
    "matchup_win_probability",
    "opponent_finish",
)
