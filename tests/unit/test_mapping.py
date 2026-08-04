from sleeper_manager.integrations.nba.mapping import normalize_player_name


def test_normalizes_diacritics() -> None:
    assert normalize_player_name("Nikola Jokić") == normalize_player_name("Nikola Jokic")


def test_normalizes_suffixes() -> None:
    assert normalize_player_name("Darius Acuff") == normalize_player_name("Darius Acuff Jr.")
