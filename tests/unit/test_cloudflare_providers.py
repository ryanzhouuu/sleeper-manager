import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from sleeper_manager.cloudflare.providers import (
    CloudflareESPNProvider,
    CloudflareProviderError,
    CloudflareSleeperClient,
    ProviderHTTPError,
    ProviderPayloadError,
)
from sleeper_manager.integrations.nba.espn import ESPNSchemaError

FIXTURES = Path(__file__).parents[1] / "fixtures"
NOW = datetime(2026, 1, 5, 12, tzinfo=UTC)


class Response:
    def __init__(self, status: int, payload: Any = None, *, invalid_json: bool = False) -> None:
        self.status = status
        self._payload = payload
        self._invalid_json = invalid_json

    async def json(self) -> Any:
        if self._invalid_json:
            raise ValueError("invalid json")
        return self._payload


class SequenceFetch:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.urls: list[str] = []

    async def __call__(self, url: str) -> Response:
        self.urls.append(url)
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        assert isinstance(value, Response)
        return value


def _fixture(path: str) -> Any:
    return json.loads((FIXTURES / path).read_text())


@pytest.mark.parametrize("status", (429, 500, 503))
def test_sleeper_fetch_retries_transient_http_once_and_validates_shape(status: int) -> None:
    fetch = SequenceFetch(
        Response(status, {}),
        Response(200, _fixture("sleeper/users.json")),
    )
    client = CloudflareSleeperClient(fetch, base_url="https://sleeper.test")

    users = asyncio.run(client.users("league-current"))

    assert len(users) == 2
    assert len(fetch.urls) == 2


@pytest.mark.parametrize("status", (400, 404))
def test_sleeper_fetch_does_not_retry_ordinary_4xx(status: int) -> None:
    fetch = SequenceFetch(Response(status, {}), Response(200, []))
    client = CloudflareSleeperClient(fetch)

    with pytest.raises(ProviderHTTPError) as captured:
        asyncio.run(client.rosters("league-current"))

    assert captured.value.status == status
    assert len(fetch.urls) == 1


def test_invalid_json_and_payload_shape_are_not_retried() -> None:
    invalid_json = SequenceFetch(Response(200, invalid_json=True), Response(200, []))
    client = CloudflareSleeperClient(invalid_json)
    with pytest.raises(ProviderPayloadError, match="invalid JSON"):
        asyncio.run(client.users("league-current"))
    assert len(invalid_json.urls) == 1

    invalid_shape = SequenceFetch(Response(200, {}), Response(200, []))
    client = CloudflareSleeperClient(invalid_shape)
    with pytest.raises(ProviderPayloadError, match="object list"):
        asyncio.run(client.users("league-current"))
    assert len(invalid_shape.urls) == 1


def test_network_failure_retries_once_then_fails() -> None:
    fetch = SequenceFetch(OSError("network down"), OSError("still down"))
    client = CloudflareSleeperClient(fetch, timeout=timedelta(milliseconds=10))

    with pytest.raises(CloudflareProviderError, match="request failed"):
        asyncio.run(client.state())

    assert len(fetch.urls) == 2


def test_timeout_retries_once_then_fails() -> None:
    class SlowFetch:
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, url: str) -> Response:
            del url
            self.calls += 1
            await asyncio.sleep(0.05)
            return Response(200, {})

    fetch = SlowFetch()
    client = CloudflareSleeperClient(fetch, timeout=timedelta(milliseconds=1))

    with pytest.raises(CloudflareProviderError, match="request failed"):
        asyncio.run(client.state())

    assert fetch.calls == 2


def test_espn_adapter_uses_recorded_fixture_and_does_not_retry_schema_drift() -> None:
    roster_fetch = SequenceFetch(Response(200, _fixture("espn/roster.json")))
    provider = CloudflareESPNProvider(roster_fetch, clock=lambda: NOW)
    roster = asyncio.run(provider.team_roster("12"))
    assert roster.records
    assert roster.quality.retrieved_at == NOW

    drifted = SequenceFetch(Response(200, {"athletes": [{"id": "1"}]}), Response(200, {}))
    provider = CloudflareESPNProvider(drifted, clock=lambda: NOW)
    with pytest.raises(ESPNSchemaError):
        asyncio.run(provider.team_roster("12"))
    assert len(drifted.urls) == 1


def test_espn_legitimate_empty_schedule_is_preserved() -> None:
    fetch = SequenceFetch(Response(200, {"events": []}))
    provider = CloudflareESPNProvider(fetch, clock=lambda: NOW)

    result = asyncio.run(provider.team_schedule("12", 2026))

    assert result.records == ()
    assert result.quality.record_count == 0
