from datetime import UTC, datetime
from pathlib import Path

import httpx

from sleeper_manager.domain.nba import AvailabilityStatus, SourceMetadata
from sleeper_manager.integrations.nba.official_injury_report import (
    OfficialInjuryReportClient,
    ReportSubmissionStatus,
    assess_official_report_coverage,
    official_injury_report_url,
    parse_official_injury_report_text,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "nba" / "official_injury_report.txt"
TOKEN_FIXTURE = (
    Path(__file__).parents[1] / "fixtures" / "nba" / "official_injury_report_pypdf_tokens.txt"
)
PUBLISHED_AT = datetime(2025, 1, 1, 13, 30, tzinfo=UTC)
RETRIEVED_AT = datetime(2026, 8, 9, 4, tzinfo=UTC)


def source() -> SourceMetadata:
    return SourceMetadata(
        provider="nba_official_injury_report",
        provider_id="fixture",
        retrieved_at=RETRIEVED_AT,
        source_updated_at=PUBLISHED_AT,
    )


def test_official_report_parser_preserves_report_time_status_reason_and_submission_state() -> None:
    snapshot = parse_official_injury_report_text(FIXTURE.read_text(), source=source())

    assert snapshot.published_at == PUBLISHED_AT
    assert snapshot.entries[0].team_abbreviation == "chi"
    assert snapshot.entries[0].player_name == "Craig, Torrey"
    assert snapshot.entries[0].status is AvailabilityStatus.DOUBTFUL
    assert snapshot.entries[0].reason == "Injury/Illness - Right Lower Leg; Contusion"
    assert snapshot.entries[2].status is AvailabilityStatus.PROBABLE
    assert snapshot.not_yet_submitted_teams == ("tor", "bos", "min")
    assert snapshot.team_statuses[-2].status is ReportSubmissionStatus.NOT_YET_SUBMITTED
    assert snapshot.team_statuses[-1].game_date.isoformat() == "2025-01-02"


def test_official_report_url_uses_pdf_filename_publication_slot() -> None:
    assert official_injury_report_url(PUBLISHED_AT) == (
        "https://ak-static.cms.nba.com/referee/injury/"
        "Injury-Report_2025-01-01_08AM.pdf"
    )


def test_official_report_parser_restores_pypdf_tokenized_rows() -> None:
    snapshot = parse_official_injury_report_text(TOKEN_FIXTURE.read_text(), source=source())

    assert [entry.player_name for entry in snapshot.entries] == [
        "Craig, Torrey",
        "Banchero, Paolo",
    ]
    assert snapshot.entries[1].game_time.isoformat() == "07:00:00"
    assert snapshot.not_yet_submitted_teams == ("was", "det")


def test_official_report_coverage_surfaces_missing_snapshots() -> None:
    snapshot = parse_official_injury_report_text(FIXTURE.read_text(), source=source())
    coverage = assess_official_report_coverage(
        [PUBLISHED_AT, datetime(2025, 1, 1, 14, tzinfo=UTC)],
        [snapshot],
    )

    assert coverage.received_published_at == (PUBLISHED_AT,)
    assert coverage.missing_published_at == (datetime(2025, 1, 1, 14, tzinfo=UTC),)
    assert not coverage.complete


def test_official_report_client_hashes_and_parses_downloaded_pdf() -> None:
    async def run() -> str:
        async def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url) == official_injury_report_url(PUBLISHED_AT)
            return httpx.Response(200, content=b"pdf bytes")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            async with OfficialInjuryReportClient(
                client=client,
                clock=lambda: RETRIEVED_AT,
                pdf_text_extractor=lambda _: FIXTURE.read_text(),
            ) as adapter:
                result = await adapter.report(PUBLISHED_AT)
                assert result.source.content_hash is not None
                return result.entries[0].player_name

    import asyncio

    assert asyncio.run(run()) == "Craig, Torrey"
