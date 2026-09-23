"""Fetch the qualified Sleeper season forecast feed as exact response bytes.

JSON clients reparse the body before a caller can hash it. Capture identity
depends on those original bytes, so this module reads `content` or a bytes
method and leaves normalization to the season-forecast parser.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from sleeper_manager.domain.forecast_capture import ForecastSource

FORECAST_ADAPTER_VERSION = "sleeper-season-forecast-v1"
FORECAST_ORIGIN = "https://api.sleeper.com"
_TOKEN_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


class SleeperForecastFetchError(Exception):
    """Raised when the season feed cannot be retrieved as a usable response."""

    def __init__(
        self,
        code: str,
        *,
        started_at: datetime,
        status: int | None = None,
        received_at: datetime | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.started_at = started_at
        self.status = status
        self.received_at = received_at


@dataclass(frozen=True, slots=True)
class SleeperForecastFetch:
    """One completed HTTP response whose body has not been reinterpreted."""

    url: str
    status: int
    body: bytes
    started_at: datetime
    received_at: datetime


def season_forecast_source(season: str, season_type: str) -> ForecastSource:
    """Build the source identity qualified for the season-forecast parser."""

    return ForecastSource(
        provider="sleeper",
        endpoint=season_forecast_endpoint(season, season_type),
        season=season,
        season_type=season_type,
        horizon="season",
        adapter_version=FORECAST_ADAPTER_VERSION,
    )


def season_forecast_endpoint(season: str, season_type: str) -> str:
    """Return the path stored on receipts for one season feed."""

    _require_token(season, "Forecast season")
    _require_token(season_type, "Forecast season type")
    return f"/projections/nba/{season}?season_type={season_type}"


def season_forecast_url(source: ForecastSource) -> str:
    """Return the public URL for a source identity this adapter can fetch."""

    if source.provider != "sleeper" or source.horizon != "season":
        raise ValueError("Forecast URL requires the Sleeper season source")
    if source.endpoint != season_forecast_endpoint(source.season, source.season_type):
        raise ValueError("Forecast source endpoint does not match its season")
    return f"{FORECAST_ORIGIN}{source.endpoint}"


async def fetch_season_forecast(
    fetch: Callable[[str], Awaitable[object]],
    *,
    source: ForecastSource,
    clock: Callable[[], datetime],
) -> SleeperForecastFetch:
    """GET one season feed and preserve the response bytes and timing.

    Raises SleeperForecastFetchError when the request fails or the status is
    outside the success range. The body is not parsed.
    """

    url = season_forecast_url(source)
    started_at = clock()
    try:
        response = await fetch(url)
    except SleeperForecastFetchError:
        raise
    except Exception as error:
        raise SleeperForecastFetchError("transport", started_at=started_at) from error
    received_at = clock()
    try:
        status = _response_status(response)
        body = await _response_body(response)
    except SleeperForecastFetchError as error:
        raise SleeperForecastFetchError(
            error.code,
            started_at=started_at,
            status=error.status,
            received_at=received_at,
        ) from error
    if not 200 <= status <= 299:
        raise SleeperForecastFetchError(
            "http_error",
            started_at=started_at,
            status=status,
            received_at=received_at,
        )
    return SleeperForecastFetch(
        url=url,
        status=status,
        body=body,
        started_at=started_at,
        received_at=received_at,
    )


def _response_status(response: object) -> int:
    """Read either a Workers `status` or an httpx `status_code`."""

    status = getattr(response, "status", None)
    if status is None:
        status = getattr(response, "status_code", None)
    if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
        raise SleeperForecastFetchError("invalid_response", started_at=datetime.min)
    return status


async def _response_body(response: object) -> bytes:
    """Read the original payload from httpx or a Workers response."""

    content = getattr(response, "content", None)
    if isinstance(content, bytes):
        return content
    body = getattr(response, "body", None)
    if isinstance(body, bytes):
        return body
    for name in ("bytes", "arrayBuffer"):
        method = getattr(response, name, None)
        if not callable(method):
            continue
        value = method()
        if inspect.isawaitable(value):
            value = await value
        if isinstance(value, bytes):
            return value
        if isinstance(value, bytearray | memoryview):
            return bytes(value)
        to_bytes = getattr(value, "to_bytes", None)
        if callable(to_bytes):
            converted = to_bytes()
            if isinstance(converted, bytes):
                return converted
    raise SleeperForecastFetchError("response_body", started_at=datetime.min)


def _require_token(value: str, label: str) -> None:
    """Keep season identifiers from changing the request path."""

    if not value or any(character not in _TOKEN_CHARACTERS for character in value):
        raise ValueError(f"{label} contains unsupported characters")


__all__ = (
    "FORECAST_ADAPTER_VERSION",
    "FORECAST_ORIGIN",
    "SleeperForecastFetch",
    "SleeperForecastFetchError",
    "fetch_season_forecast",
    "season_forecast_endpoint",
    "season_forecast_source",
    "season_forecast_url",
)
