import asyncio
from datetime import UTC, datetime
from pathlib import Path
from textwrap import dedent

import httpx

from sleeper_manager.domain.nba import AvailabilityStatus, SourceMetadata
from sleeper_manager.integrations.nba.official_injury_report import (
    OfficialInjuryReportClient,
    ReportSubmissionStatus,
    assess_official_report_coverage,
    deserialize_official_injury_report_snapshot,
    official_injury_report_url,
    official_injury_report_urls,
    parse_official_injury_report_text,
    serialize_official_injury_report_snapshot,
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


def test_official_report_snapshot_serialization_round_trips_nested_fields() -> None:
    snapshot = parse_official_injury_report_text(FIXTURE.read_text(), source=source())

    restored = deserialize_official_injury_report_snapshot(
        serialize_official_injury_report_snapshot(snapshot)
    )

    assert restored == snapshot


def test_official_report_url_uses_pdf_filename_publication_slot() -> None:
    assert official_injury_report_url(PUBLISHED_AT) == (
        "https://ak-static.cms.nba.com/referee/injury/Injury-Report_2025-01-01_08AM.pdf"
    )


def test_official_report_urls_include_minute_qualified_fallback() -> None:
    assert official_injury_report_urls(PUBLISHED_AT) == (
        "https://ak-static.cms.nba.com/referee/injury/Injury-Report_2025-01-01_08AM.pdf",
        "https://ak-static.cms.nba.com/referee/injury/Injury-Report_2025-01-01_08_30AM.pdf",
    )


def test_official_report_urls_support_non_half_hour_modern_slots() -> None:
    published_at = datetime(2025, 1, 1, 13, 45, tzinfo=UTC)
    assert official_injury_report_urls(published_at) == (
        "https://ak-static.cms.nba.com/referee/injury/Injury-Report_2025-01-01_08_45AM.pdf",
    )


def test_official_report_parser_restores_pypdf_tokenized_rows() -> None:
    snapshot = parse_official_injury_report_text(TOKEN_FIXTURE.read_text(), source=source())

    assert [entry.player_name for entry in snapshot.entries] == [
        "Craig, Torrey",
        "Banchero, Paolo",
    ]
    assert snapshot.entries[1].game_time.isoformat() == "07:00:00"
    assert snapshot.not_yet_submitted_teams == ("was", "det")


def test_official_report_parser_keeps_suffix_surname_after_prior_reason_tokens() -> None:
    text = """
    Injury
    Report:
    01/01/25
    08:30
    AM
    Page
    1
    of
    1
    Game
    Date
    Game
    Time
    Matchup
    Team
    Player
    Name
    Current
    Status
    Reason
    01/01/2025
    07:00
    (ET)
    CHI@BKN
    Chicago
    Bulls
    Craig,
    Torrey
    Out
    Injury/Illness
    -
    Left
    Calf;
    Strain
    Brooklyn
    Nets
    Repair
    Smith
    Jr.,
    Dennis
    Doubtful
    Injury/Illness
    -
    Back;
    Soreness
    """

    snapshot = parse_official_injury_report_text(dedent(text), source=source())

    assert [entry.player_name for entry in snapshot.entries] == [
        "Craig, Torrey",
        "Smith Jr., Dennis",
    ]


def test_official_report_parser_preserves_game_date_across_repeated_page_header() -> None:
    text = """
    Injury Report: 11/06/22 05:30 PM
    Game Date Game Time Matchup Team Player Name Current Status Reason
    11/07/2022 07:00 (ET) WAS@CHA Washington Wizards NOT YET SUBMITTED
    Charlotte Hornets NOT YET SUBMITTED
    Injury Report: 11/06/22 05:30 PM
    Game Date Game Time Matchup Team Player Name Current Status Reason
    08:45 (ET) TOR@CHI Chicago Bulls NOT YET SUBMITTED
    Toronto Raptors NOT YET SUBMITTED
    """

    report_source = SourceMetadata(
        provider="nba_official_injury_report",
        provider_id="pagination-fixture",
        retrieved_at=RETRIEVED_AT,
        source_updated_at=datetime(2022, 11, 6, 22, 30, tzinfo=UTC),
    )
    snapshot = parse_official_injury_report_text(text, source=report_source)

    reverse_matchup = tuple(
        status for status in snapshot.team_statuses if status.matchup == "TOR@CHI"
    )
    assert {status.game_date.isoformat() for status in reverse_matchup} == {"2022-11-07"}


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


def test_official_report_client_tries_minute_qualified_fallback() -> None:
    async def run() -> tuple[str, tuple[str, ...]]:
        requested: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requested.append(str(request.url))
            if len(requested) == 1:
                return httpx.Response(404)
            return httpx.Response(200, content=b"pdf bytes")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            async with OfficialInjuryReportClient(
                client=client,
                clock=lambda: RETRIEVED_AT,
                pdf_text_extractor=lambda _: FIXTURE.read_text(),
            ) as adapter:
                result = await adapter.report(PUBLISHED_AT)
                return result.entries[0].player_name, tuple(requested)

    player_name, requested_urls = asyncio.run(run())
    assert player_name == "Craig, Torrey"
    assert requested_urls == official_injury_report_urls(PUBLISHED_AT)
