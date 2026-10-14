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
from sleeper_manager.integrations.nba.mapping import espn_team_endpoint_id

_ESPN_BASES = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba",
    "https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba",
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

    async def object(self, url: str, *, fallback_url: str | None = None) -> dict[str, Any]:
        payload = await self._request(url, fallback_url=fallback_url)
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

    async def _request(self, url: str, *, fallback_url: str | None = None) -> Any:
        """Spend at most two attempts, switching hosts only for transport failures."""
        for attempt, resource in enumerate((url, fallback_url or url)):
            try:
                response = await asyncio.wait_for(self._fetch(resource), timeout=self._timeout)
            except Exception as error:
                if attempt == 0:
                    continue
                raise CloudflareProviderError(
                    f"{self._provider} request failed for {resource}"
                ) from error
            status = int(response.status)
            if status == 429 or status >= 500 or (status == 403 and fallback_url is not None):
                if attempt == 0:
                    continue
                raise ProviderHTTPError(self._provider, status, resource)
            if status < 200 or status >= 300:
                raise ProviderHTTPError(self._provider, status, resource)
            try:
                payload = response.json()
                return await payload if inspect.isawaitable(payload) else payload
            except Exception as error:
                raise ProviderPayloadError(
                    f"{self._provider} returned invalid JSON for {resource}"
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
        base_url: str = _ESPN_BASES[0],
        timeout: timedelta = timedelta(seconds=15),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._fallback_base = (
            next((base for base in _ESPN_BASES if base != self._base_url), None)
            if self._base_url in _ESPN_BASES
            else None
        )
        self._client = _CloudflareJSONClient(fetcher, provider="ESPN", timeout=timeout)
        self._clock = clock or (lambda: datetime.now(UTC))

    def _url(self, path: str, **params: str | int) -> str:
        url = f"{self._base_url}{path}"
        return f"{url}?{urlencode(params)}" if params else url

    async def _object(self, path: str, **params: str | int) -> dict[str, Any]:
        """Keep paths and parameters identical on the two fixed public ESPN hosts."""
        url = self._url(path, **params)
        alternate = (
            self._fallback_base + url[len(self._base_url) :]
            if self._fallback_base is not None
            else None
        )
        return await self._client.object(url, fallback_url=alternate)

    async def scoreboard(self, game_date: date) -> ProviderResult[tuple[ScheduledGame, ...]]:
        payload = await self._object("/scoreboard", dates=game_date.strftime("%Y%m%d"), limit=100)
        return parse_scoreboard(payload, retrieved_at=self._tick())

    async def game_summary(self, game_id: str) -> ProviderResult[GameSummary]:
        payload = await self._object("/summary", event=game_id)
        return parse_game_summary(payload, retrieved_at=self._tick())

    async def injuries(self) -> ProviderResult[tuple[PlayerAvailability, ...]]:
        payload = await self._object("/injuries")
        return parse_injuries(payload, retrieved_at=self._tick())

    async def team_roster(self, team_id: str) -> ProviderResult[tuple[ProviderPlayer, ...]]:
        endpoint_id = espn_team_endpoint_id(team_id)
        payload = await self._object(f"/teams/{endpoint_id}/roster")
        return parse_team_roster(payload, team_id=team_id, retrieved_at=self._tick())

    async def team_schedule(
        self, team_id: str, season: int
    ) -> ProviderResult[tuple[ScheduledGame, ...]]:
        """Read the regular-season slate for an ESPN season end year."""
        endpoint_id = espn_team_endpoint_id(team_id)
        payload = await self._object(f"/teams/{endpoint_id}/schedule", season=season, seasontype=2)
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
