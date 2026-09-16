import asyncio

import httpx
import pytest
import respx

from sleeper_manager.integrations.sleeper.client import SleeperAPIError, SleeperClient


@respx.mock
def test_wraps_transport_errors() -> None:
    respx.get("https://api.sleeper.app/v1/league/league-current").mock(
        side_effect=httpx.ConnectError("offline")
    )

    async def run() -> None:
        async with SleeperClient() as sleeper:
            await sleeper.league("league-current")

    with pytest.raises(SleeperAPIError, match="request failed"):
        asyncio.run(run())
