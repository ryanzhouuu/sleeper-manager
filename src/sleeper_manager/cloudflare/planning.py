from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sleeper_manager.cloudflare.projection_history import D1ProjectionHistory
from sleeper_manager.cloudflare.providers import (
    CloudflareESPNProvider,
    CloudflareSleeperClient,
)
from sleeper_manager.domain.league import LeagueProfile
from sleeper_manager.domain.runtime_policy import RuntimePolicy
from sleeper_manager.domain.scheduling import eastern_fantasy_week
from sleeper_manager.integrations.nba.cached_provider import AsyncCachedNBAProvider
from sleeper_manager.integrations.sleeper.sync import LeagueSynchronizationService
from sleeper_manager.persistence.base import AsyncRuntimeStateRepository
from sleeper_manager.projections.live_baseline import DirectBaselineProjectionProvider
from sleeper_manager.workflows.planning_collection import (
    CollectedLiveEvidence,
    collect_live_planning_inputs,
)
from sleeper_manager.workflows.planning_inputs import (
    FantasyWeekWindow,
    PlanningFreshnessPolicy,
)


class CloudflarePlanningConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CloudflarePlanningAssembly:
    evidence: CollectedLiveEvidence
    player_names: Mapping[str, str]
    local_timezone: ZoneInfo


@dataclass(slots=True)
class _StaticProfileSource:
    profile: LeagueProfile

    async def fetch(self) -> LeagueProfile:
        return self.profile


@dataclass(slots=True)
class _CatalogSource:
    sleeper: CloudflareSleeperClient
    catalog: Mapping[str, Mapping[str, Any]] | None = None

    async def players(self) -> Mapping[str, Mapping[str, Any]]:
        if self.catalog is None:
            self.catalog = await self.sleeper.players(active=True)
        return self.catalog


async def collect_cloudflare_planning_inputs(
    env: Any,
    fetcher: Any,
    *,
    repository: AsyncRuntimeStateRepository,
    policy: RuntimePolicy,
    scheduled_at: datetime,
    clock: Callable[[], datetime] | None = None,
) -> CloudflarePlanningAssembly:
    if scheduled_at.tzinfo is None:
        raise CloudflarePlanningConfigurationError("Scheduled time must be timezone-aware")
    league_id = _required_env(env, "SLEEPER_LEAGUE_ID")
    user_id = _required_env(env, "SLEEPER_USER_ID")
    tick = clock or (lambda: datetime.now(UTC))
    sleeper = CloudflareSleeperClient(fetcher)
    profile = (
        await LeagueSynchronizationService(sleeper, clock=tick).sync(
            league_id=league_id,
            user_id=user_id,
        )
    ).profile
    week = eastern_fantasy_week(scheduled_at)
    week_window = FantasyWeekWindow(profile.fantasy_week.week, week.starts_at, week.ends_at)
    history_loaded_at = tick()
    history = await D1ProjectionHistory.load_version(
        repository,
        history_version=policy.projection_history_version,
        loaded_at=history_loaded_at,
    )
    nba = AsyncCachedNBAProvider(
        CloudflareESPNProvider(fetcher, clock=tick),
        repository,
        clock=tick,
        provider_name="espn",
    )
    catalog_source = _CatalogSource(sleeper)
    evidence = await collect_live_planning_inputs(
        profile_source=_StaticProfileSource(profile),
        catalog_source=catalog_source,
        nba=nba,
        projection_provider=DirectBaselineProjectionProvider(history),
        week_window=week_window,
        freshness_policy=PlanningFreshnessPolicy(
            max_sleeper_age=policy.sleeper_max_age,
            max_nba_schedule_age=policy.team_data_max_age,
            max_availability_age=policy.availability_max_age,
        ),
        runtime_policy_version=policy.version,
        move_lead_time=policy.move_lead_time,
        projection_history_version=history.history_version,
        projection_history_retrieved_at=history.loaded_at,
        acknowledgement_source=repository,
        mapping_overrides=policy.mapping_overrides,
        clock=tick,
    )
    catalog = catalog_source.catalog or {}
    player_names = {
        player_id: name
        for player_id, payload in catalog.items()
        if (name := _player_name(payload)) is not None
    }
    return CloudflarePlanningAssembly(
        evidence=evidence,
        player_names=player_names,
        local_timezone=ZoneInfo(policy.manager_timezone),
    )


def _player_name(payload: Mapping[str, Any]) -> str | None:
    for key in ("full_name", "display_name", "first_name"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _required_env(env: Any, name: str) -> str:
    value = getattr(env, name, None)
    if value is None or not str(value).strip():
        raise CloudflarePlanningConfigurationError(f"{name} is required")
    return str(value).strip()


__all__ = (
    "CloudflarePlanningAssembly",
    "CloudflarePlanningConfigurationError",
    "collect_cloudflare_planning_inputs",
)
