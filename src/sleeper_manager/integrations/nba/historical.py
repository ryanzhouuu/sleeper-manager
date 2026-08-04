SPORTSDATAVERSE_BASE_URL = (
    "https://github.com/sportsdataverse/sportsdataverse-data/releases/download"
)


def player_box_score_url(season: int) -> str:
    return f"{SPORTSDATAVERSE_BASE_URL}/espn_nba_player_boxscores/player_box_{season}.rds"


def schedule_url(season: int) -> str:
    return f"{SPORTSDATAVERSE_BASE_URL}/espn_nba_schedules/nba_schedule_{season}.rds"
