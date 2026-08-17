from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime

import httpx

from sleeper_manager.domain.nba import SourceMetadata
from sleeper_manager.integrations.nba.official_injury_models import (
    REPORT_SCHEMA_VERSION,
    OfficialInjuryReportError,
    OfficialInjuryReportSnapshot,
)
from sleeper_manager.integrations.nba.official_injury_parser import (
    _as_eastern,
    extract_official_injury_report_text,
    official_injury_report_urls,
    parse_official_injury_report_text,
)


class OfficialInjuryReportClient:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] | None = None,
        pdf_text_extractor: Callable[[bytes], str] | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None
        self._clock = clock or (lambda: datetime.now(UTC))
        self._pdf_text_extractor = pdf_text_extractor or extract_official_injury_report_text

    async def __aenter__(self) -> OfficialInjuryReportClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def report(self, published_at: datetime) -> OfficialInjuryReportSnapshot:
        response: httpx.Response | None = None
        url: str | None = None
        for candidate_url in official_injury_report_urls(published_at):
            candidate_response = await self._client.get(candidate_url)
            if candidate_response.status_code in {403, 404}:
                response = candidate_response
                url = candidate_url
                continue
            if candidate_response.status_code != 200:
                raise OfficialInjuryReportError(
                    "Official injury report request failed with "
                    f"HTTP {candidate_response.status_code}: {candidate_url}"
                )
            response = candidate_response
            url = candidate_url
            break
        if response is None or response.status_code != 200 or url is None:
            status = response.status_code if response is not None else "unknown"
            raise OfficialInjuryReportError(
                f"Official injury report request failed with HTTP {status}: {url}"
            )
        retrieved_at = self._clock()
        if retrieved_at.tzinfo is None:
            raise OfficialInjuryReportError(
                "Official injury report clock must return a timezone-aware timestamp"
            )
        published_timestamp = _as_eastern(published_at).astimezone(UTC)
        source = SourceMetadata(
            provider="nba_official_injury_report",
            provider_id=url,
            retrieved_at=retrieved_at.astimezone(UTC),
            source_updated_at=published_timestamp,
            schema_version=REPORT_SCHEMA_VERSION,
            content_hash=hashlib.sha256(response.content).hexdigest(),
        )
        return parse_official_injury_report_text(
            self._pdf_text_extractor(response.content),
            source=source,
        )
