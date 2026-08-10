from datetime import UTC, datetime
from pathlib import Path

from sleeper_manager.domain.nba import ProviderPlayer, SourceMetadata
from sleeper_manager.integrations.nba.official_injury_mapping import map_official_injury_report
from sleeper_manager.integrations.nba.official_injury_report import (
    parse_official_injury_report_text,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "nba" / "official_injury_report.txt"
PUBLISHED_AT = datetime(2025, 1, 1, 13, 30, tzinfo=UTC)
RETRIEVED_AT = datetime(2026, 8, 9, 4, tzinfo=UTC)
SOURCE = SourceMetadata(
    provider="nba_official_injury_report",
    provider_id="fixture",
    retrieved_at=RETRIEVED_AT,
    source_updated_at=PUBLISHED_AT,
)


def provider(player_id: str, name: str, team: str) -> ProviderPlayer:
    return ProviderPlayer(player_id, name, f"team-{team.casefold()}", team, True, SOURCE)


def test_official_injury_mapping_resolves_report_display_names_to_provider_ids() -> None:
    snapshot = parse_official_injury_report_text(FIXTURE.read_text(), source=SOURCE)
    result = map_official_injury_report(
        snapshot,
        [
            provider("espn-craig", "Torrey Craig", "CHI"),
            provider("espn-dosunmu", "Ayo Dosunmu", "CHI"),
            provider("espn-smith", "Jalen Smith", "CHI"),
            provider("espn-bagley", "Marvin Bagley III", "WAS"),
            provider("espn-poole", "Jordan Poole", "WAS"),
            provider("espn-melton", "De'Anthony Melton", "BKN"),
        ],
    )

    assert len(result.availability) == 6
    assert not result.unresolved
    assert result.availability[0].player_id == "espn-craig"
    assert result.availability[0].available_as_of == PUBLISHED_AT
    assert result.availability[3].player_id == "espn-bagley"


def test_official_injury_mapping_surfaces_missing_identity_without_guessing() -> None:
    snapshot = parse_official_injury_report_text(FIXTURE.read_text(), source=SOURCE)
    result = map_official_injury_report(snapshot, [provider("espn-craig", "Torrey Craig", "CHI")])

    assert len(result.unresolved) == 5
    assert result.unresolved[0].entry.player_name == "Dosunmu, Ayo"
    assert result.warnings
