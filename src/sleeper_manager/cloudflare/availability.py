"""Fetch recent official reports without importing the local httpx client into Workers."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

from sleeper_manager.domain.nba import SourceMetadata
from sleeper_manager.integrations.nba.official_injury_models import (
    REPORT_SCHEMA_VERSION,
    OfficialInjuryReportError,
    OfficialInjuryReportSnapshot,
)
from sleeper_manager.integrations.nba.official_injury_parser import (
    extract_official_injury_report_text,
    official_injury_report_urls,
    parse_official_injury_report_text,
)
from sleeper_manager.projections.hybrid_types import require_aware

MAX_REPORT_BYTES = 2_000_000


class CloudflareOfficialAvailability:
    """Probe the last hour of quarter-hour publications; never reuse a stale cached report."""

    def __init__(
        self,
        fetcher: Any,
        *,
        clock: Callable[[], datetime] | None = None,
        extract: Callable[[bytes], str] = extract_official_injury_report_text,
    ) -> None:
        self._fetch = fetcher
        self._clock = clock or (lambda: datetime.now(UTC))
        self._extract = extract

    async def report(self, cutoff: datetime) -> OfficialInjuryReportSnapshot | None:
        """Return the newest available publication; provider errors cannot renew older evidence."""
        require_aware(cutoff)
        slot = cutoff.replace(minute=cutoff.minute // 15 * 15, second=0, microsecond=0)
        for offset in range(5):
            published_at = slot - timedelta(minutes=15 * offset)
            for url in official_injury_report_urls(published_at):
                response = await asyncio.wait_for(self._fetch(url), timeout=15)
                if int(response.status) in (403, 404):
                    continue
                if int(response.status) != 200:
                    raise OfficialInjuryReportError(f"Official report HTTP {int(response.status)}")
                length = response.headers.get("content-length")
                if length is not None and int(length) > MAX_REPORT_BYTES:
                    raise OfficialInjuryReportError("Official report exceeds byte limit")
                content = await asyncio.wait_for(response.bytes(), timeout=15)
                if not isinstance(content, bytes):
                    raise OfficialInjuryReportError("Official report body is not bytes")
                if len(content) > MAX_REPORT_BYTES:
                    raise OfficialInjuryReportError("Official report exceeds byte limit")
                retrieved_at = self._clock()
                require_aware(retrieved_at)
                source = SourceMetadata(
                    "nba_official_injury_report",
                    url,
                    retrieved_at,
                    published_at,
                    REPORT_SCHEMA_VERSION,
                    sha256(content).hexdigest(),
                )
                return parse_official_injury_report_text(self._extract(content), source=source)
        return None
