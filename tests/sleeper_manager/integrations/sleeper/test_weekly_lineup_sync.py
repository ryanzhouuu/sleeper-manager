"""League sync plans against the matchup without restoring departed ownership."""

import asyncio

import httpx
import pytest
import respx

from sleeper_manager.integrations.sleeper.client import SleeperClient
from sleeper_manager.integrations.sleeper.sync import (
    LeagueCompatibilityError,
    LeagueSynchronizationService,
)
from tests.sleeper_manager.integrations.sleeper.test_league_sync import fixture, route_league


async def _sync():  # type: ignore[no-untyped-def]
    async with SleeperClient() as sleeper:
        return await LeagueSynchronizationService(sleeper).sync(
            league_id="league-current", user_id="user-manager"
        )


@respx.mock
def test_sync_uses_current_matchup_starters_and_preserves_ownership() -> None:
    league = fixture("current_league.json")
    league["roster_positions"] = ["PG", "SG", "BN", "IR"]
    rosters = fixture("rosters.json")
    rosters[0]["starters"] = ["0", "player-1"]
    route_league(league, rosters=rosters)
    route = respx.get("https://api.sleeper.app/v1/league/league-current/matchups/1").mock(
        return_value=httpx.Response(
            200, json=[{"roster_id": 1, "starters": ["player-2", "player-1"]}]
        )
    )

    roster = asyncio.run(_sync()).profile.rosters[0]

    assert roster.starter_ids == ("player-2", "player-1")
    assert roster.player_ids == tuple(rosters[0]["players"])
    assert roster.reserve_ids == ("player-3",)
    assert route.call_count == 1


@respx.mock
def test_sync_does_not_restore_departed_matchup_starter_ownership() -> None:
    league = fixture("current_league.json")
    league["roster_positions"] = ["PG", "SG", "BN", "IR"]
    route_league(league)
    respx.get("https://api.sleeper.app/v1/league/league-current/matchups/1").mock(
        return_value=httpx.Response(
            200, json=[{"roster_id": 1, "starters": ["departed", "player-2"]}]
        )
    )

    roster = asyncio.run(_sync()).profile.rosters[0]

    assert roster.starter_ids == (None, "player-2")
    assert "departed" not in roster.player_ids


@respx.mock
@pytest.mark.parametrize("matchups", [[], [{"roster_id": 1}], [{"roster_id": 2, "starters": []}]])
def test_sync_rejects_missing_weekly_starter_evidence(matchups):  # type: ignore[no-untyped-def]
    route_league(fixture("current_league.json"))
    respx.get("https://api.sleeper.app/v1/league/league-current/matchups/1").mock(
        return_value=httpx.Response(200, json=matchups)
    )

    with pytest.raises(LeagueCompatibilityError, match="weekly lineup"):
        asyncio.run(_sync())
