"""Player names for the home document, including ESPN headshot ids.

Sleeper's NBA catalog often omits `espn_id`. Rostered players are matched to
ESPN team rosters before the name map is cached for the local day. A failed
roster read is not cached, so the next page load can try again.
"""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any, Protocol

from sleeper_manager.cloudflare.home_cache import (
    decode_identities,
    read_home_cache,
    write_home_cache,
)
from sleeper_manager.cloudflare.home_document import HomeIdentity
from sleeper_manager.cloudflare.providers import CloudflareProviderError
from sleeper_manager.domain.nba import DataQualityState, ProviderPlayer, ProviderResult
from sleeper_manager.domain.scheduling import manager_local_day
from sleeper_manager.integrations.nba.identity import (
    PlayerIdentityMapper,
    SleeperPlayerIdentity,
    parse_sleeper_player_identity,
)
from sleeper_manager.persistence.base import AsyncRuntimeStateRepository


class PlayerCatalog(Protocol):
    """Sleeper player catalog read."""

    async def players(self, *, active: bool = True) -> dict[str, dict[str, Any]]: ...


class TeamRosterSource(Protocol):
    """ESPN team roster read used to recover headshot ids."""

    async def team_roster(self, team_id: str) -> ProviderResult[tuple[ProviderPlayer, ...]]: ...


async def load_player_identities(
    sleeper: PlayerCatalog,
    repository: AsyncRuntimeStateRepository,
    *,
    timezone_name: str,
    now: datetime,
) -> tuple[dict[str, HomeIdentity], bool] | None:
    """Return player identities and whether they came from the local-day cache."""
    local_day = manager_local_day(now, timezone_name=timezone_name)
    key = f"home-players:{local_day.isoformat()}"
    cached = decode_identities(await read_home_cache(repository, key, now))
    if cached is not None:
        return cached, True
    try:
        payload = await sleeper.players(active=True)
    except (CloudflareProviderError, TypeError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    identities: dict[str, HomeIdentity] = {}
    for sleeper_id, raw in payload.items():
        if not isinstance(raw, Mapping):
            continue
        try:
            parsed = parse_sleeper_player_identity(str(sleeper_id), raw)
        except ValueError:
            continue
        identities[str(sleeper_id)] = HomeIdentity(
            sleeper_id=str(sleeper_id),
            name=parsed.full_name,
            nba_team=parsed.team,
            espn_id=parsed.espn_id,
        )
    return identities, False


async def fill_espn_ids(
    espn: TeamRosterSource,
    identities: dict[str, HomeIdentity],
    player_ids: Sequence[str],
) -> bool:
    """Fill roster headshots from ESPN team rosters. False when a roster read fails."""
    pending = [
        identities[player_id]
        for player_id in player_ids
        if player_id in identities
        and identities[player_id].espn_id is None
        and identities[player_id].name
        and identities[player_id].nba_team
    ]
    if not pending:
        return True
    provider_players: list[ProviderPlayer] = []
    for team in sorted({identity.nba_team for identity in pending if identity.nba_team}):
        try:
            result = await espn.team_roster(team)
        except Exception:
            return False
        if result.quality.state is DataQualityState.ERROR:
            return False
        provider_players.extend(result.records)
    report = PlayerIdentityMapper().resolve(
        (
            SleeperPlayerIdentity(
                sleeper_id=identity.sleeper_id,
                full_name=identity.name or "",
                team=identity.nba_team,
                espn_id=None,
            )
            for identity in pending
        ),
        provider_players,
    )
    for mapping in report.mappings:
        if mapping.espn_id is None:
            continue
        current = identities[mapping.sleeper_id]
        identities[mapping.sleeper_id] = replace(current, espn_id=mapping.espn_id)
    return True


async def remember_identities(
    repository: AsyncRuntimeStateRepository,
    identities: Mapping[str, HomeIdentity],
    *,
    timezone_name: str,
    now: datetime,
) -> None:
    """Cache names and resolved headshot ids until the manager-local day ends."""
    local_day = manager_local_day(now, timezone_name=timezone_name)
    await write_home_cache(
        repository,
        f"home-players:{local_day.isoformat()}",
        {
            identity.sleeper_id: {
                "name": identity.name,
                "nba_team": identity.nba_team,
                "espn_id": identity.espn_id,
            }
            for identity in identities.values()
        },
        resource="player-catalog",
        timezone_name=timezone_name,
        now=now,
    )
