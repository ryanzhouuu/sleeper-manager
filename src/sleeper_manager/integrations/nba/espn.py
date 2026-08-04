from datetime import date, timedelta
from typing import Any

import httpx


class ESPNAPIError(RuntimeError):
    pass


class ESPNClient:
    """Replaceable adapter around ESPN's public NBA JSON endpoints."""

    def __init__(
        self,
        *,
        base_url: str = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba",
        timeout: timedelta = timedelta(seconds=15),
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout.total_seconds(),
        )
        self._owns_client = client is None

    async def __aenter__(self) -> "ESPNClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get(self, path: str, **params: str | int) -> dict[str, Any]:
        response = await self._client.get(path, params=params)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise ESPNAPIError(f"ESPN returned {response.status_code} for {path}") from error
        payload = response.json()
        if not isinstance(payload, dict):
            raise ESPNAPIError(f"ESPN returned an unexpected payload for {path}")
        return payload

    async def scoreboard(self, game_date: date) -> dict[str, Any]:
        return await self._get("/scoreboard", dates=game_date.strftime("%Y%m%d"), limit=100)

    async def game_summary(self, game_id: str) -> dict[str, Any]:
        return await self._get("/summary", event=game_id)

    async def injuries(self) -> dict[str, Any]:
        return await self._get("/injuries")

    async def team_roster(self, team_id: str) -> dict[str, Any]:
        return await self._get(f"/teams/{team_id}/roster")

    async def team_schedule(self, team_id: str, season: int) -> dict[str, Any]:
        return await self._get(f"/teams/{team_id}/schedule", season=season)
