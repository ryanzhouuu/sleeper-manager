import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from sleeper_manager.integrations.sleeper.client import SleeperClient
from sleeper_manager.integrations.sleeper.sync import (
    LeagueCompatibilityError,
    LeagueOwnershipError,
    LeagueSynchronizationService,
)
from sleeper_manager.persistence.sqlite import SQLiteStateRepository

FIXTURES = Path(__file__).parents[1] / "fixtures" / "sleeper"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def route_league(
    payload: dict[str, Any],
    *,
    league_id: str = "league-current",
    users: Any = None,
    rosters: Any = None,
) -> None:
    respx.get(f"https://api.sleeper.app/v1/league/{league_id}").mock(
        return_value=httpx.Response(200, json=payload)
    )
    respx.get(f"https://api.sleeper.app/v1/league/{league_id}/users").mock(
        return_value=httpx.Response(200, json=fixture("users.json") if users is None else users)
    )
    respx.get(f"https://api.sleeper.app/v1/league/{league_id}/rosters").mock(
        return_value=httpx.Response(
            200, json=fixture("rosters.json") if rosters is None else rosters
        )
    )
    respx.get("https://api.sleeper.app/v1/state/nba").mock(
        return_value=httpx.Response(200, json=fixture("state.json"))
    )
    respx.get(f"https://api.sleeper.app/v1/league/{league_id}/transactions/1").mock(
        return_value=httpx.Response(200, json=fixture("transactions.json"))
    )


@respx.mock
def test_sync_builds_profile_and_detects_same_configuration(tmp_path) -> None:  # type: ignore[no-untyped-def]
    route_league(fixture("current_league.json"))
    repository = SQLiteStateRepository(tmp_path / "state.db")
    repository.initialize()

    def clock() -> datetime:
        return datetime(2026, 8, 3, 12, tzinfo=UTC)

    async def run() -> tuple[Any, Any]:
        async with SleeperClient() as sleeper:
            service = LeagueSynchronizationService(
                sleeper,
                profile_store=repository,
                clock=clock,
            )
            first = await service.sync(league_id="league-current", user_id="user-manager")
            second = await service.sync(league_id="league-current", user_id="user-manager")
            return first, second

    first, second = asyncio.run(run())

    assert first.profile.mode.value == "lock_in"
    assert first.profile.manager_roster_id == 1
    assert first.profile.fantasy_week.week == 1
    assert first.profile.transactions[0].transaction_id == "transaction-1"
    assert first.configuration_changed is False
    assert second.configuration_changed is False
    assert second.previous_fingerprint == first.profile.configuration_fingerprint


@respx.mock
def test_sync_replays_previous_league_fixture() -> None:
    route_league(fixture("previous_league.json"), league_id="league-previous")

    async def run() -> Any:
        async with SleeperClient() as sleeper:
            return await LeagueSynchronizationService(sleeper).sync(
                league_id="league-previous", user_id="user-manager"
            )

    result = asyncio.run(run())

    assert result.profile.league_id == "league-previous"
    assert result.profile.season == "2025"
    assert result.profile.scoring.rebounds == 1


@respx.mock
def test_sync_treats_null_reserve_as_empty() -> None:
    rosters = fixture("rosters.json")
    rosters[0]["reserve"] = None
    route_league(fixture("current_league.json"), rosters=rosters)

    async def run() -> Any:
        async with SleeperClient() as sleeper:
            return await LeagueSynchronizationService(sleeper).sync(
                league_id="league-current", user_id="user-manager"
            )

    result = asyncio.run(run())

    assert result.profile.rosters[0].reserve_ids == ()


@respx.mock
def test_sync_treats_zero_starter_as_empty() -> None:
    rosters = fixture("rosters.json")
    rosters[0]["starters"].append("0")
    route_league(fixture("current_league.json"), rosters=rosters)

    async def run() -> Any:
        async with SleeperClient() as sleeper:
            return await LeagueSynchronizationService(sleeper).sync(
                league_id="league-current", user_id="user-manager"
            )

    result = asyncio.run(run())

    assert result.profile.rosters[0].starter_ids == ("player-1", "player-2")


@respx.mock
def test_sync_allows_offseason_without_week_transactions() -> None:
    route_league(fixture("current_league.json"))
    respx.get("https://api.sleeper.app/v1/state/nba").mock(
        return_value=httpx.Response(
            200,
            json={"week": 0, "leg": 0, "display_week": 0, "season_type": "off"},
        )
    )

    async def run() -> Any:
        async with SleeperClient() as sleeper:
            return await LeagueSynchronizationService(sleeper).sync(
                league_id="league-current", user_id="user-manager"
            )

    result = asyncio.run(run())

    assert result.profile.fantasy_week.week == 0
    assert result.profile.fantasy_week.season_type == "off"
    assert result.profile.transactions == ()


@respx.mock
def test_sync_does_not_treat_numeric_type_as_mode() -> None:
    league = fixture("current_league.json")
    league["settings"]["game_mode"] = 0
    league["settings"]["type"] = 1
    route_league(league)

    async def run() -> Any:
        async with SleeperClient() as sleeper:
            return await LeagueSynchronizationService(sleeper).sync(
                league_id="league-current", user_id="user-manager"
            )

    with pytest.raises(LeagueCompatibilityError, match="verified Lock-In"):
        asyncio.run(run())


@respx.mock
def test_sync_detects_changed_configuration(tmp_path) -> None:  # type: ignore[no-untyped-def]
    current = fixture("current_league.json")
    changed = fixture("current_league.json")
    changed["scoring_settings"]["reb"] = 1
    league_route = respx.get("https://api.sleeper.app/v1/league/league-current").mock(
        side_effect=[httpx.Response(200, json=current), httpx.Response(200, json=changed)]
    )
    assert league_route
    respx.get("https://api.sleeper.app/v1/league/league-current/users").mock(
        return_value=httpx.Response(200, json=fixture("users.json"))
    )
    respx.get("https://api.sleeper.app/v1/league/league-current/rosters").mock(
        return_value=httpx.Response(200, json=fixture("rosters.json"))
    )
    respx.get("https://api.sleeper.app/v1/state/nba").mock(
        return_value=httpx.Response(200, json=fixture("state.json"))
    )
    respx.get("https://api.sleeper.app/v1/league/league-current/transactions/1").mock(
        return_value=httpx.Response(200, json=fixture("transactions.json"))
    )
    repository = SQLiteStateRepository(tmp_path / "state.db")
    repository.initialize()

    async def run() -> tuple[Any, Any]:
        async with SleeperClient() as sleeper:
            service = LeagueSynchronizationService(sleeper, profile_store=repository)
            first = await service.sync(league_id="league-current", user_id="user-manager")
            second = await service.sync(league_id="league-current", user_id="user-manager")
            return first, second

    first, second = asyncio.run(run())

    assert first.configuration_changed is False
    assert second.configuration_changed is True
    assert second.previous_fingerprint == first.profile.configuration_fingerprint


@respx.mock
def test_sync_rejects_missing_user(tmp_path) -> None:  # type: ignore[no-untyped-def]
    route_league(fixture("current_league.json"), users=[])

    async def run() -> None:
        async with SleeperClient() as sleeper:
            await LeagueSynchronizationService(sleeper).sync(
                league_id="league-current", user_id="user-manager"
            )

    with pytest.raises(LeagueOwnershipError, match="matched 0"):
        asyncio.run(run())


@respx.mock
def test_sync_rejects_unverified_mode(tmp_path) -> None:  # type: ignore[no-untyped-def]
    league = fixture("current_league.json")
    league["settings"].pop("game_mode")
    route_league(league)

    async def run() -> None:
        async with SleeperClient() as sleeper:
            await LeagueSynchronizationService(sleeper).sync(
                league_id="league-current", user_id="user-manager"
            )

    with pytest.raises(LeagueCompatibilityError, match="verified Lock-In"):
        asyncio.run(run())
