"""Verify the season-forecast request preserves original response bytes."""

import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from sleeper_manager.integrations.sleeper.forecast_fetch import (
    FORECAST_ORIGIN,
    SleeperForecastFetchError,
    fetch_season_forecast,
    season_forecast_source,
)

NOW = datetime(2026, 9, 22, 12, tzinfo=UTC)
SOURCE = season_forecast_source("2026", "regular")
PAYLOAD = b'[{"player_id":"1"}]'


class RawResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.content = body


class WorkerResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status_code = status
        self._body = body

    async def bytes(self) -> bytes:
        return self._body


def test_fetch_preserves_httpx_bytes_and_qualified_url() -> None:
    """Hash the httpx content rather than a reparsed JSON document."""

    request = httpx.Request("GET", "https://example.test")
    response = httpx.Response(200, content=PAYLOAD, request=request)
    seen: list[str] = []

    async def fetch(url: str) -> httpx.Response:
        seen.append(url)
        return response

    fetched = asyncio.run(fetch_season_forecast(fetch, source=SOURCE, clock=lambda: NOW))

    assert seen == [f"{FORECAST_ORIGIN}/projections/nba/2026?season_type=regular"]
    assert fetched.body == PAYLOAD
    assert fetched.status == 200
    assert fetched.started_at == NOW
    assert fetched.received_at == NOW


def test_fetch_reads_worker_response_bytes() -> None:
    """Accept the Workers response bytes method used outside httpx."""

    async def fetch(url: str) -> WorkerResponse:
        del url
        return WorkerResponse(200, PAYLOAD)

    fetched = asyncio.run(fetch_season_forecast(fetch, source=SOURCE, clock=lambda: NOW))

    assert fetched.body == PAYLOAD


def test_http_and_transport_failures_keep_request_timing() -> None:
    """Expose status and start time so the archive can store a failed receipt."""

    async def http_fetch(url: str) -> RawResponse:
        del url
        return RawResponse(503, b"busy")

    with pytest.raises(SleeperForecastFetchError) as http_error:
        asyncio.run(fetch_season_forecast(http_fetch, source=SOURCE, clock=lambda: NOW))
    assert http_error.value.code == "http_error"
    assert http_error.value.status == 503
    assert http_error.value.started_at == NOW
    assert http_error.value.received_at == NOW

    async def broken_fetch(url: str) -> RawResponse:
        del url
        raise RuntimeError("network down")

    with pytest.raises(SleeperForecastFetchError) as transport_error:
        asyncio.run(fetch_season_forecast(broken_fetch, source=SOURCE, clock=lambda: NOW))
    assert transport_error.value.code == "transport"
    assert transport_error.value.status is None
    assert transport_error.value.started_at == NOW
