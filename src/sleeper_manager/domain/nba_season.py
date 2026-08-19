from datetime import date


def nba_season_start_year(value: date) -> int:
    return value.year if value.month >= 10 else value.year - 1
