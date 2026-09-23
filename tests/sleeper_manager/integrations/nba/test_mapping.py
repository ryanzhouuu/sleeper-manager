from sleeper_manager.integrations.nba.mapping import (
    espn_team_endpoint_id,
    normalize_player_name,
    normalize_report_player_name,
)


def test_normalizes_diacritics() -> None:
    assert normalize_player_name("Nikola Jokić") == normalize_player_name("Nikola Jokic")


def test_normalizes_suffixes() -> None:
    assert normalize_player_name("Darius Acuff") == normalize_player_name("Darius Acuff Jr.")


def test_normalizes_report_last_first_display() -> None:
    assert normalize_report_player_name("Bagley III, Marvin") == "marvin bagley"


def test_espn_team_endpoint_aliases_match_public_team_ids() -> None:
    """ESPN's roster and schedule routes use different New Orleans and Utah tokens."""

    assert espn_team_endpoint_id("NOP") == "no"
    assert espn_team_endpoint_id("UTA") == "utah"
    assert espn_team_endpoint_id("CHI") == "CHI"
