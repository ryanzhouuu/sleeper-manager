from sleeper_manager.integrations.nba.mapping import (
    normalize_player_name,
    normalize_report_player_name,
)


def test_normalizes_diacritics() -> None:
    assert normalize_player_name("Nikola Jokić") == normalize_player_name("Nikola Jokic")


def test_normalizes_suffixes() -> None:
    assert normalize_player_name("Darius Acuff") == normalize_player_name("Darius Acuff Jr.")


def test_normalizes_report_last_first_display() -> None:
    assert normalize_report_player_name("Bagley III, Marvin") == "marvin bagley"
