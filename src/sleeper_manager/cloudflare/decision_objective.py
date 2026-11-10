"""Turn the manager's win-chance objective into planner input for one decision.

Without today's opponent outlook or Sleeper's live opponent locks, decisions fall back to
expected points rather than guessing at the opponent.
"""

from __future__ import annotations

import sys
from datetime import datetime

from sleeper_manager.cloudflare.home_win_probability import opponent_finish
from sleeper_manager.cloudflare.live_score import HomeLockedGameCache, matchup_scores
from sleeper_manager.cloudflare.opponent_outlook import load_opponent_outlook
from sleeper_manager.decisions.win_probability import WinObjective
from sleeper_manager.domain.league import LeagueProfile
from sleeper_manager.domain.nba_season import nba_season_start_year
from sleeper_manager.domain.runtime_policy import RuntimePolicy
from sleeper_manager.domain.scheduling import manager_local_day
from sleeper_manager.persistence.base import AsyncRuntimeStateRepository
from sleeper_manager.workflows.sleeper_locks import SleeperLockReader


async def planning_win_objective(
    runtime_policy: RuntimePolicy,
    *,
    repository: AsyncRuntimeStateRepository,
    reader: SleeperLockReader | None,
    profile: LeagueProfile,
    now: datetime,
) -> WinObjective | None:
    if runtime_policy.manager_intent.objective != "win_chance" or reader is None:
        return None
    timezone_name = runtime_policy.manager_timezone
    season = nba_season_start_year(manager_local_day(now, timezone_name=timezone_name))
    cache = HomeLockedGameCache(repository, timezone_name=timezone_name, now=now)
    week = profile.fantasy_week.week
    try:
        scores = await matchup_scores(
            reader,
            profile.league_id,
            week,
            profile.manager_roster_id,
            season=season,
            scoring=profile.scoring,
            read_stats=reader.player_game_stats,
            cache=cache,
        )
        opponent = next(
            (item for item in (scores or {}).items() if item[0] != profile.manager_roster_id),
            None,
        )
        if opponent is None:
            return None
        finish = await opponent_finish(
            await load_opponent_outlook(repository, profile.league_id, week, now=now),
            opponent[0],
            opponent[1],
            read_stats=reader.player_game_stats,
            season=season,
            scoring=profile.scoring,
            cache=cache,
            now=now,
        )
    except Exception as error:
        print(f"Win-chance objective unavailable: {type(error).__name__}", file=sys.stderr)
        return None
    return None if finish is None else WinObjective(finish)


__all__ = ("planning_win_objective",)
