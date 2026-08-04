from datetime import timedelta
from typing import Any

import httpx


class SleeperAPIError(RuntimeError):
    pass


class SleeperClient:
    """Client for Sleeper's documented, read-only public API."""

    def __init__(
        self,
        *,
        base_url: str = "https://api.sleeper.app/v1",
        timeout: timedelta = timedelta(seconds=15),
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout.total_seconds(),
        )
        self._owns_client = client is None

    async def __aenter__(self) -> "SleeperClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get(self, path: str) -> Any:
        try:
            response = await self._client.get(path)
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise SleeperAPIError(f"Sleeper returned {response.status_code} for {path}") from error
        except httpx.HTTPError as error:
            raise SleeperAPIError(f"Sleeper request failed for {path}") from error
        try:
            return response.json()
        except ValueError as error:
            raise SleeperAPIError(f"Sleeper returned invalid JSON for {path}") from error

    async def _get_object(self, path: str) -> dict[str, Any]:
        payload = await self._get(path)
        if not isinstance(payload, dict):
            raise SleeperAPIError(f"Sleeper returned an unexpected payload for {path}")
        return payload

    async def _get_object_list(self, path: str) -> list[dict[str, Any]]:
        payload = await self._get(path)
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise SleeperAPIError(f"Sleeper returned an unexpected payload for {path}")
        return payload

    async def league(self, league_id: str) -> dict[str, Any]:
        return await self._get_object(f"/league/{league_id}")

    async def rosters(self, league_id: str) -> list[dict[str, Any]]:
        return await self._get_object_list(f"/league/{league_id}/rosters")

    async def users(self, league_id: str) -> list[dict[str, Any]]:
        return await self._get_object_list(f"/league/{league_id}/users")

    async def matchups(self, league_id: str, week: int) -> list[dict[str, Any]]:
        return await self._get_object_list(f"/league/{league_id}/matchups/{week}")

    async def transactions(self, league_id: str, week: int) -> list[dict[str, Any]]:
        return await self._get_object_list(f"/league/{league_id}/transactions/{week}")

    async def players(self, *, active: bool = True) -> dict[str, dict[str, Any]]:
        suffix = "?active=true" if active else ""
        path = f"/players/nba{suffix}"
        payload = await self._get_object(path)
        if not all(isinstance(player, dict) for player in payload.values()):
            raise SleeperAPIError(f"Sleeper returned an unexpected payload for {path}")
        return payload

    async def state(self) -> dict[str, Any]:
        return await self._get_object("/state/nba")
