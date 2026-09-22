"""Collect one due forecast snapshot after lineup planning has finished.

League synchronization discovers the season and whether pre-game captures apply.
The player catalog is consulted only when the local-day tipoff cache is missing.
Failures propagate to the wake hook, which keeps the planning result unchanged.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, time, timedelta
from typing import Any, Protocol

from sleeper_manager.domain.league import LeagueProfile
from sleeper_manager.domain.runtime_policy import RuntimePolicy
from sleeper_manager.integrations.sleeper.forecast_fetch import season_forecast_source
from sleeper_manager.integrations.sleeper.sync import LeagueSynchronizationService, SleeperReader
from sleeper_manager.persistence.base import AsyncNBADataCache
from sleeper_manager.persistence.forecast_repository import AsyncForecastArchiveRepository
from sleeper_manager.workflows.forecast_capture import execute_forecast_actions
from sleeper_manager.workflows.forecast_schedule import plan_forecast_capture
from sleeper_manager.workflows.forecast_tipoffs import (
    TeamScheduleSource,
    capture_roster_player_ids,
    resolve_relevant_tipoffs,
    tipoffs_for_capture,
)


class ForecastSleeperClient(SleeperReader, Protocol):
    """League sync plus the matchup and catalog reads used to find tipoffs."""

    async def matchups(self, league_id: str, week: int) -> list[dict[str, Any]]: ...

    async def players(self, *, active: bool = True) -> dict[str, dict[str, Any]]: ...


async def capture_scheduled_forecast(
    archive: AsyncForecastArchiveRepository,
    cache: AsyncNBADataCache,
    sleeper: ForecastSleeperClient,
    *,
    policy: RuntimePolicy,
    nba: TeamScheduleSource,
    fetch: Callable[[str], Awaitable[object]],
    now: datetime,
    league_id: str,
    user_id: str,
) -> None:
    """Sync the league, then store at most one forecast request for this wake."""

    profile = (
        await LeagueSynchronizationService(sleeper, clock=lambda: now).sync(
            league_id=league_id,
            user_id=user_id,
        )
    ).profile
    await record_due_forecast(
        archive,
        cache,
        sleeper,
        policy=policy,
        profile=profile,
        nba=nba,
        fetch=fetch,
        now=now,
    )


async def record_due_forecast(
    archive: AsyncForecastArchiveRepository,
    cache: AsyncNBADataCache,
    sleeper: ForecastSleeperClient,
    *,
    policy: RuntimePolicy,
    profile: LeagueProfile,
    nba: TeamScheduleSource,
    fetch: Callable[[str], Awaitable[object]],
    now: datetime,
) -> None:
    """Plan and persist the due forecast slot for an already synchronized league."""

    source = season_forecast_source(profile.season, profile.season_type)
    in_season = profile.status == "in_season"

    async def resolve() -> tuple[datetime, ...]:
        matchups = await sleeper.matchups(profile.league_id, profile.fantasy_week.week)
        catalog = await sleeper.players(active=True)
        return await resolve_relevant_tipoffs(
            player_ids=capture_roster_player_ids(profile, matchups),
            catalog=catalog,
            nba=nba,
            season=profile.season,
            mapping_overrides=policy.mapping_overrides,
        )

    tipoffs = await tipoffs_for_capture(
        cache,
        league_id=profile.league_id,
        now=now,
        timezone_name=policy.manager_timezone,
        in_season=in_season,
        resolve=resolve,
    )
    utc_start = datetime.combine(now.astimezone(UTC).date(), time.min, UTC)
    receipts = await archive.list_receipts(
        source,
        start=utc_start - timedelta(days=2),
        end=utc_start + timedelta(days=1),
    )
    actions = plan_forecast_capture(
        now=now,
        timezone_name=policy.manager_timezone,
        daily_plan_time=policy.daily_plan_time,
        in_season=in_season,
        tipoffs=tipoffs,
        receipts=receipts,
        source=source,
    )
    if not actions:
        return
    await execute_forecast_actions(
        archive,
        actions,
        source=source,
        fetch=fetch,
        now=now,
    )


__all__ = (
    "ForecastSleeperClient",
    "capture_scheduled_forecast",
    "record_due_forecast",
)
