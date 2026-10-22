"""Resolve editor scope from the configured Sleeper owner, never submitted identity fields."""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sleeper_manager.cloudflare.home_identities import load_player_identities
from sleeper_manager.cloudflare.providers import CloudflareSleeperClient
from sleeper_manager.integrations.sleeper.schemas import (
    SleeperLeaguePayload,
    SleeperRosterPayload,
    SleeperStatePayload,
)
from sleeper_manager.persistence.manager_store import D1ManagerStore


@dataclass(frozen=True)
class ManagerContext:
    league_id: str
    week: int
    roster_id: int
    positions: tuple[str, ...]
    starters: tuple[str, ...]
    names: dict[str, str]
    timezone: str

    def actual_slot(self, player_id: str) -> tuple[int, str] | None:
        """Only an observed current starting slot can support a reported lock."""
        for index, starter in enumerate(self.starters):
            if starter == player_id:
                return index, self.positions[index]
        return None


async def load_manager_context(
    env: Any, fetcher: Any, store: D1ManagerStore, now: datetime
) -> ManagerContext:
    """Fail closed if the manager, current week, or starter alignment is ambiguous."""
    league_id, user_id = str(env.SLEEPER_LEAGUE_ID), str(env.SLEEPER_USER_ID)
    sleeper = CloudflareSleeperClient(fetcher)
    league_raw, rosters_raw, state_raw = await asyncio.gather(
        sleeper.league(league_id), sleeper.rosters(league_id), sleeper.state()
    )
    league = SleeperLeaguePayload.model_validate(league_raw)
    state = SleeperStatePayload.model_validate(state_raw)
    rosters = [SleeperRosterPayload.model_validate(row) for row in rosters_raw]
    owned = [row for row in rosters if row.owner_id == user_id]
    week = next((value for value in (state.week, state.leg, state.display_week) if value), None)
    if len(owned) != 1 or week is None or week < 1 or league.league_id != league_id:
        raise ValueError("Manager scope unavailable")
    positions = tuple(p for p in league.roster_positions if p.upper() not in {"BN", "IR"})
    starters = tuple(owned[0].starters)
    occupied = [player for player in starters if player not in {"", "0"}]
    if len(positions) != len(starters) or len(set(occupied)) != len(occupied):
        raise ValueError("Starter alignment unavailable")
    timezone = str(getattr(env, "TIMEZONE", "America/Chicago"))
    loaded = await load_player_identities(sleeper, store, timezone_name=timezone, now=now)
    names = {key: value.name or key for key, value in loaded[0].items()} if loaded else {}
    return ManagerContext(league_id, week, owned[0].roster_id, positions, starters, names, timezone)
