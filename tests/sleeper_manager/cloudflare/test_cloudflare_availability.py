"""Worker-native report fetches expose source failures and publication identity."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sleeper_manager.cloudflare.availability import MAX_REPORT_BYTES, CloudflareOfficialAvailability
from sleeper_manager.integrations.nba.official_injury_models import OfficialInjuryReportError
from tests.paths import FIXTURES_DIR

NOW = datetime(2025, 1, 1, 13, 30, tzinfo=UTC)
TEXT = (Path(FIXTURES_DIR) / "nba/official_injury_report.txt").read_text()


class Response:
    def __init__(self, status: int, body: bytes = b"pdf", length: str | None = "3") -> None:
        self.status, self.body = status, body
        self.headers = {"content-length": length} if length else {}

    async def bytes(self) -> bytes:
        return self.body


def test_newest_report_is_hashed_and_observed_at_actual_fetch_time() -> None:
    paths: list[str] = []

    async def fetch(url: str) -> Response:
        paths.append(url)
        return Response(200)

    provider = CloudflareOfficialAvailability(fetch, clock=lambda: NOW, extract=lambda _: TEXT)
    report = asyncio.run(provider.report(NOW))
    assert report is not None
    assert report.published_at == NOW
    assert report.source.retrieved_at == NOW
    assert report.source.content_hash
    assert len(paths) == 1
    assert report.entries


def test_missing_recent_report_is_an_explicit_gap_with_bounded_requests() -> None:
    paths: list[str] = []

    async def fetch(url: str) -> Response:
        paths.append(url)
        return Response(404)

    provider = CloudflareOfficialAvailability(fetch, extract=lambda _: TEXT)
    assert asyncio.run(provider.report(NOW)) is None
    assert 5 <= len(paths) <= 7


@pytest.mark.parametrize("status", [429, 500])
def test_provider_failure_cannot_fall_back_and_renew_old_report(status: int) -> None:
    async def fetch(url: str) -> Response:
        return Response(status)

    with pytest.raises(OfficialInjuryReportError, match="HTTP"):
        asyncio.run(CloudflareOfficialAvailability(fetch).report(NOW))


@pytest.mark.parametrize("header", [True, False])
def test_report_size_is_bounded_with_or_without_content_length(header: bool) -> None:
    async def fetch(url: str) -> Response:
        return Response(
            200, b"x" * (MAX_REPORT_BYTES + 1), str(MAX_REPORT_BYTES + 1) if header else None
        )

    with pytest.raises(OfficialInjuryReportError, match="byte limit"):
        asyncio.run(CloudflareOfficialAvailability(fetch).report(NOW))


def test_cached_response_with_wrong_publication_cannot_renew_a_status() -> None:
    async def fetch(url: str) -> Response:
        return Response(200)

    with pytest.raises(OfficialInjuryReportError, match="timestamp.*header"):
        asyncio.run(
            CloudflareOfficialAvailability(fetch, extract=lambda _: TEXT).report(
                NOW + timedelta(minutes=15)
            )
        )
