from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from sleeper_manager.domain.nba import (
    GameSummary,
    PlayerAvailability,
    ProviderPlayer,
    ProviderResult,
    ScheduledGame,
)
from sleeper_manager.integrations.nba.espn import (
    parse_game_summary,
    parse_injuries,
    parse_scoreboard,
    parse_team_roster,
    parse_team_schedule,
)


class CloudflareProviderError(RuntimeError):
    pass


class ProviderHTTPError(CloudflareProviderError):
    def __init__(self, provider: str, status: int, resource: str) -> None:
        super().__init__(f"{provider} returned HTTP {status} for {resource}")
        self.provider = provider
        self.status = status
        self.resource = resource


class ProviderPayloadError(CloudflareProviderError):
    pass


class _CloudflareJSONClient:
    def __init__(
        self,
        fetcher: Any,
        *,
        provider: str,
        timeout: timedelta = timedelta(seconds=15),
    ) -> None:
        if timeout <= timedelta(0):
            raise ValueError("Provider timeout must be positive")
        self._fetch = fetcher
        self._provider = provider
        self._timeout = timeout.total_seconds()

    async def object(self, url: str) -> dict[str, Any]:
        payload = await self._request(url)
        if not isinstance(payload, dict):
            raise ProviderPayloadError(f"{self._provider} returned a non-object payload for {url}")
        return payload

    async def object_list(self, url: str) -> list[dict[str, Any]]:
        payload = await self._request(url)
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise ProviderPayloadError(
                f"{self._provider} returned an invalid object list for {url}"
            )
        return payload

    async def _request(self, url: str) -> Any:
        for attempt in range(2):
            try:
                response = await asyncio.wait_for(self._fetch(url), timeout=self._timeout)
            except Exception as error:
                if attempt == 0:
                    continue
                raise CloudflareProviderError(
                    f"{self._provider} request failed for {url}"
                ) from error
            status = int(response.status)
            if status == 429 or status >= 500:
                if attempt == 0:
                    continue
                raise ProviderHTTPError(self._provider, status, url)
            if status < 200 or status >= 300:
                raise ProviderHTTPError(self._provider, status, url)
            try:
                payload = response.json()
                return await payload if inspect.isawaitable(payload) else payload
            except Exception as error:
                raise ProviderPayloadError(
                    f"{self._provider} returned invalid JSON for {url}"
                ) from error
        raise AssertionError("Provider retry loop did not terminate")


class CloudflareSleeperClient:
    """Sleeper's read-only API through the Workers-native fetch function."""

    def __init__(
        self,
        fetcher: Any,
        *,
        base_url: str = "https://api.sleeper.app/v1",
        timeout: timedelta = timedelta(seconds=15),
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = _CloudflareJSONClient(fetcher, provider="Sleeper", timeout=timeout)

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    async def league(self, league_id: str) -> dict[str, Any]:
        return await self._client.object(self._url(f"/league/{league_id}"))

    async def rosters(self, league_id: str) -> list[dict[str, Any]]:
        return await self._client.object_list(self._url(f"/league/{league_id}/rosters"))

    async def users(self, league_id: str) -> list[dict[str, Any]]:
        return await self._client.object_list(self._url(f"/league/{league_id}/users"))

    async def matchups(self, league_id: str, week: int) -> list[dict[str, Any]]:
        return await self._client.object_list(self._url(f"/league/{league_id}/matchups/{week}"))

    async def transactions(self, league_id: str, week: int) -> list[dict[str, Any]]:
        return await self._client.object_list(self._url(f"/league/{league_id}/transactions/{week}"))

    async def players(self, *, active: bool = True) -> dict[str, dict[str, Any]]:
        suffix = "?active=true" if active else ""
        payload = await self._client.object(self._url(f"/players/nba{suffix}"))
        if not all(isinstance(player, dict) for player in payload.values()):
            raise ProviderPayloadError("Sleeper returned an invalid NBA player catalog")
        return payload

    async def state(self) -> dict[str, Any]:
        return await self._client.object(self._url("/state/nba"))


class CloudflareESPNProvider:
    """ESPN's NBA endpoints through Workers fetch and existing strict parsers."""

    def __init__(
        self,
        fetcher: Any,
        *,
        base_url: str = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba",
        timeout: timedelta = timedelta(seconds=15),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = _CloudflareJSONClient(fetcher, provider="ESPN", timeout=timeout)
        self._clock = clock or (lambda: datetime.now(UTC))

    def _url(self, path: str, **params: str | int) -> str:
        url = f"{self._base_url}{path}"
        return f"{url}?{urlencode(params)}" if params else url

    async def scoreboard(self, game_date: date) -> ProviderResult[tuple[ScheduledGame, ...]]:
        payload = await self._client.object(
            self._url("/scoreboard", dates=game_date.strftime("%Y%m%d"), limit=100)
        )
        return parse_scoreboard(payload, retrieved_at=self._tick())

    async def game_summary(self, game_id: str) -> ProviderResult[GameSummary]:
        payload = await self._client.object(self._url("/summary", event=game_id))
        return parse_game_summary(payload, retrieved_at=self._tick())

    async def injuries(self) -> ProviderResult[tuple[PlayerAvailability, ...]]:
        payload = await self._client.object(self._url("/injuries"))
        return parse_injuries(payload, retrieved_at=self._tick())

    async def team_roster(self, team_id: str) -> ProviderResult[tuple[ProviderPlayer, ...]]:
        payload = await self._client.object(self._url(f"/teams/{team_id}/roster"))
        return parse_team_roster(payload, team_id=team_id, retrieved_at=self._tick())

    async def team_schedule(
        self, team_id: str, season: int
    ) -> ProviderResult[tuple[ScheduledGame, ...]]:
        payload = await self._client.object(self._url(f"/teams/{team_id}/schedule", season=season))
        return parse_team_schedule(payload, retrieved_at=self._tick())

    def _tick(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise CloudflareProviderError("Provider clock must be timezone-aware")
        return value


__all__ = (
    "CloudflareESPNProvider",
    "CloudflareProviderError",
    "CloudflareSleeperClient",
    "ProviderHTTPError",
    "ProviderPayloadError",
)
