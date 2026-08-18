import json
from datetime import UTC, datetime
from pathlib import Path

from sleeper_manager.backtesting.experiments.injuries import (
    acquire_injury_archive,
    latest_report_timestamp,
    requested_report_timestamps,
)
from sleeper_manager.domain.nba import (
    AvailabilityStatus,
    GameStatus,
    ProviderPlayer,
    ScheduledGame,
    SourceMetadata,
)
from sleeper_manager.integrations.nba.official_injury_mapping import InjuryMappingCategory
from sleeper_manager.integrations.nba.official_injury_models import (
    OfficialInjuryReportEntry,
    OfficialInjuryReportSnapshot,
)
from sleeper_manager.integrations.nba.official_injury_parser import (
    official_injury_report_minute_url,
)


class FakeResponse:
    def __init__(self, status_code: int, content: bytes = b"") -> None:
        self.status_code = status_code
        self.content = content


class FakeClient:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def get(self, url: str) -> FakeResponse:
        self.urls.append(url)
        return self.responses.pop(0)


def game(game_id: str, start: datetime) -> ScheduledGame:
    return ScheduledGame(
        provider_id=game_id,
        start_time=start,
        status=GameStatus.FINAL,
        home_team_id="1",
        away_team_id="2",
        status_detail="Final",
        source=SourceMetadata("fixture", game_id, start),
    )


def parse_fixture(_: bytes, source: SourceMetadata) -> OfficialInjuryReportSnapshot:
    assert source.source_updated_at is not None
    return OfficialInjuryReportSnapshot(
        published_at=source.source_updated_at,
        entries=(),
        team_statuses=(),
        source=source,
    )


def test_latest_report_timestamp_floors_to_prior_half_hour() -> None:
    assert latest_report_timestamp(datetime(2025, 1, 2, 1, 45, tzinfo=UTC)) == datetime(
        2025, 1, 2, 1, 30, tzinfo=UTC
    )
    assert latest_report_timestamp(datetime(2025, 1, 2, 1, 15, tzinfo=UTC)) == datetime(
        2025, 1, 2, 0, 30, tzinfo=UTC
    )
    assert requested_report_timestamps(
        (game("later", datetime(2025, 1, 2, 2, 30, tzinfo=UTC)),)
    ) == (datetime(2025, 1, 2, 2, 0, tzinfo=UTC),)


def test_archive_falls_back_and_reuses_cached_report(tmp_path: Path) -> None:
    games = (
        game("g1", datetime(2025, 1, 2, 2, 0, tzinfo=UTC)),
        game("g2", datetime(2025, 1, 2, 2, 0, tzinfo=UTC)),
    )
    assert requested_report_timestamps(games) == (datetime(2025, 1, 2, 1, 30, tzinfo=UTC),)
    client = FakeClient([FakeResponse(404), FakeResponse(200, b"report")])

    first = acquire_injury_archive(
        games,
        (),
        tmp_path,
        retrieved_at=datetime(2026, 8, 14, tzinfo=UTC),
        client=client,
        parser=parse_fixture,
        request_interval_seconds=0,
    )

    assert len(client.urls) == 2
    assert first.selections[0].selected_at == datetime(2025, 1, 2, 1, 30, tzinfo=UTC)
    assert first.selections[0].attempts == 1
    assert first.selections[0].url == official_injury_report_minute_url(
        datetime(2025, 1, 2, 1, 30, tzinfo=UTC)
    )
    assert first.selections[0].cache_path is not None
    assert first.selections[0].cache_path.endswith("-minute.pdf")
    assert first.selections[0].unavailable_candidates == (
        (datetime(2025, 1, 2, 1, 30, tzinfo=UTC), "http_404"),
    )
    assert first.selections[0].sha256 is not None
    assert len(first.snapshots) == 1

    cached_client = FakeClient([FakeResponse(404)])
    second = acquire_injury_archive(
        games,
        (),
        tmp_path,
        retrieved_at=datetime(2026, 8, 14, tzinfo=UTC),
        client=cached_client,
        parser=parse_fixture,
        request_interval_seconds=0,
    )

    assert len(cached_client.urls) == 1
    assert second.selections[0].selected_at == first.selections[0].selected_at
    assert second.selections[0].url == first.selections[0].url


def test_archive_reuses_versioned_parsed_snapshot_sidecar(tmp_path: Path, monkeypatch) -> None:
    import sleeper_manager.backtesting.experiments.injuries as injuries_module

    def parsed_snapshot(_: bytes, source: SourceMetadata) -> OfficialInjuryReportSnapshot:
        assert source.source_updated_at is not None
        return OfficialInjuryReportSnapshot(
            published_at=source.source_updated_at,
            entries=(),
            team_statuses=(),
            source=source,
        )

    monkeypatch.setattr(injuries_module, "_parse_snapshot", parsed_snapshot)
    games = (game("g1", datetime(2025, 1, 2, 2, 0, tzinfo=UTC)),)

    first = acquire_injury_archive(
        games,
        (),
        tmp_path,
        retrieved_at=datetime(2026, 8, 14, tzinfo=UTC),
        client=FakeClient([FakeResponse(200, b"report")]),
        request_interval_seconds=0,
    )

    pdf_path = Path(first.selections[0].cache_path or "")
    sidecar_path = pdf_path.with_suffix(".json")
    assert sidecar_path.is_file()

    def unexpected_parse(_: bytes, __: SourceMetadata) -> OfficialInjuryReportSnapshot:
        raise AssertionError("parsed sidecar should avoid PDF parsing")

    monkeypatch.setattr(injuries_module, "_parse_snapshot", unexpected_parse)
    second = acquire_injury_archive(
        games,
        (),
        tmp_path,
        retrieved_at=datetime(2026, 8, 14, tzinfo=UTC),
        client=FakeClient([]),
        request_interval_seconds=0,
    )

    assert second.snapshots == first.snapshots


def test_archive_reparses_stale_parsed_snapshot_sidecar(tmp_path: Path, monkeypatch) -> None:
    import sleeper_manager.backtesting.experiments.injuries as injuries_module

    parse_count = 0

    def parsed_snapshot(_: bytes, source: SourceMetadata) -> OfficialInjuryReportSnapshot:
        nonlocal parse_count
        parse_count += 1
        assert source.source_updated_at is not None
        return OfficialInjuryReportSnapshot(
            published_at=source.source_updated_at,
            entries=(),
            team_statuses=(),
            source=source,
        )

    monkeypatch.setattr(injuries_module, "_parse_snapshot", parsed_snapshot)
    games = (game("g1", datetime(2025, 1, 2, 2, 0, tzinfo=UTC)),)
    first = acquire_injury_archive(
        games,
        (),
        tmp_path,
        retrieved_at=datetime(2026, 8, 14, tzinfo=UTC),
        client=FakeClient([FakeResponse(200, b"report")]),
        request_interval_seconds=0,
    )

    sidecar_path = Path(first.selections[0].cache_path or "").with_suffix(".json")
    payload = json.loads(sidecar_path.read_text())
    payload["pdf_sha256"] = "stale"
    sidecar_path.write_text(json.dumps(payload))

    acquire_injury_archive(
        games,
        (),
        tmp_path,
        retrieved_at=datetime(2026, 8, 14, tzinfo=UTC),
        client=FakeClient([]),
        request_interval_seconds=0,
    )

    assert parse_count == 2


def test_archive_retries_transient_cdn_throttling(tmp_path: Path) -> None:
    delays: list[float] = []
    client = FakeClient([FakeResponse(429), FakeResponse(200, b"report")])

    result = acquire_injury_archive(
        (game("g1", datetime(2025, 1, 2, 2, 0, tzinfo=UTC)),),
        (),
        tmp_path,
        retrieved_at=datetime(2026, 8, 14, tzinfo=UTC),
        client=client,
        parser=parse_fixture,
        request_interval_seconds=0,
        sleeper=delays.append,
    )

    assert len(result.snapshots) == 1
    assert delays == [1.0, 0]


def test_archive_rejects_report_header_after_decision_cutoff(tmp_path: Path) -> None:
    def quarter_hour_report(_: bytes, source: SourceMetadata) -> OfficialInjuryReportSnapshot:
        assert source.source_updated_at is not None
        published_at = source.source_updated_at.replace(minute=45)
        return OfficialInjuryReportSnapshot(published_at, (), (), source)

    result = acquire_injury_archive(
        (game("g1", datetime(2025, 1, 2, 2, 0, tzinfo=UTC)),),
        (),
        tmp_path,
        retrieved_at=datetime(2026, 8, 14, tzinfo=UTC),
        client=FakeClient([FakeResponse(200, b"late"), FakeResponse(200, b"prior")]),
        parser=quarter_hour_report,
        request_interval_seconds=0,
    )

    assert result.selections[0].requested_at == datetime(2025, 1, 2, 1, 30, tzinfo=UTC)
    assert result.selections[0].selected_at == datetime(2025, 1, 2, 0, 45, tzinfo=UTC)
    assert result.selections[0].unavailable_candidates == (
        (datetime(2025, 1, 2, 1, 30, tzinfo=UTC), "post_cutoff"),
    )


def test_archive_aggregates_deterministic_mapping_diagnostics(tmp_path: Path) -> None:
    def mapping_fixture(_: bytes, source: SourceMetadata) -> OfficialInjuryReportSnapshot:
        assert source.source_updated_at is not None
        return OfficialInjuryReportSnapshot(
            published_at=source.source_updated_at,
            entries=(
                OfficialInjuryReportEntry(
                    game_date=datetime(2025, 1, 2, tzinfo=UTC).date(),
                    game_time=datetime.min.time(),
                    matchup="CHI@BOS",
                    team_abbreviation="CHI",
                    team_name="Chicago Bulls",
                    player_name="Craig, Torrey",
                    status=AvailabilityStatus.QUESTIONABLE,
                    reason=None,
                ),
                OfficialInjuryReportEntry(
                    game_date=datetime(2025, 1, 2, tzinfo=UTC).date(),
                    game_time=datetime.min.time(),
                    matchup="CHI@BOS",
                    team_abbreviation="CHI",
                    team_name="Chicago Bulls",
                    player_name="Unknown, Player",
                    status=AvailabilityStatus.OUT,
                    reason=None,
                ),
                OfficialInjuryReportEntry(
                    game_date=datetime(2025, 1, 2, tzinfo=UTC).date(),
                    game_time=datetime.min.time(),
                    matchup="CHI@BOS",
                    team_abbreviation="CHI",
                    team_name="Chicago Bulls",
                    player_name="Smith, Jalen",
                    status=AvailabilityStatus.PROBABLE,
                    reason=None,
                ),
                OfficialInjuryReportEntry(
                    game_date=datetime(2025, 1, 2, tzinfo=UTC).date(),
                    game_time=datetime.min.time(),
                    matchup="CHI@BOS",
                    team_abbreviation="DAL",
                    team_name="Dallas Mavericks",
                    player_name="II, Dereck",
                    status=AvailabilityStatus.QUESTIONABLE,
                    reason=None,
                ),
            ),
            team_statuses=(),
            source=source,
        )

    result = acquire_injury_archive(
        (game("g1", datetime(2025, 1, 2, 2, 0, tzinfo=UTC)),),
        (
            ProviderPlayer(
                "espn-craig",
                "Torrey Craig",
                "team-chi",
                "CHI",
                True,
                SourceMetadata("fixture", "player", datetime(2025, 1, 2, tzinfo=UTC)),
            ),
            ProviderPlayer(
                "espn-smith",
                "Jalen Smith",
                "team-was",
                "WAS",
                True,
                SourceMetadata("fixture", "player", datetime(2025, 1, 2, tzinfo=UTC)),
            ),
            ProviderPlayer(
                "espn-lively",
                "Dereck Lively II",
                "team-dal",
                "DAL",
                True,
                SourceMetadata("fixture", "player", datetime(2025, 1, 2, tzinfo=UTC)),
            ),
        ),
        tmp_path,
        retrieved_at=datetime(2026, 8, 14, tzinfo=UTC),
        client=FakeClient([FakeResponse(200, b"report")]),
        parser=mapping_fixture,
        request_interval_seconds=0,
    )

    counts = {diagnostic.category: diagnostic.count for diagnostic in result.mapping_diagnostics}
    assert counts[InjuryMappingCategory.RESOLVED] == 1
    assert counts[InjuryMappingCategory.RESOLVED_NAME_ONLY] == 1
    assert counts[InjuryMappingCategory.RESOLVED_PARTIAL_NAME_TEAM] == 1
    assert counts[InjuryMappingCategory.NO_NAME_TEAM_MATCH] == 1
