from datetime import date


def espn_season_end_year(sleeper_season: str) -> int:
    """Translate Sleeper's NBA start-year label to ESPN's schedule end-year label."""
    return int(sleeper_season) + 1


def nba_season_start_year(value: date) -> int:
    return value.year if value.month >= 10 else value.year - 1
