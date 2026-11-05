from datetime import date

import pytest

from sleeper_manager.domain.scoring import BoxScoreLine
from sleeper_manager.integrations.sleeper.game_stats import (
    SleeperGameStatsError,
    box_score_line,
    player_game_stats,
)

GAME_DATE = date(2026, 11, 4)
STATS = {"pts": 26.0, "reb": 15.0, "ast": 2.0, "stl": 1.0, "to": 4.0, "tf": 1.0, "min": 33.4}


def test_finds_the_row_for_the_game_date_across_weeks() -> None:
    payload = {
        "1": [{"date": "2026-10-20", "stats": {"pts": 13.0}}],
        "3": [{"date": "2026-11-04", "stats": STATS}],
        "4": [],
    }

    assert player_game_stats(payload, GAME_DATE) == STATS


def test_missing_row_is_distinct_from_empty_stats() -> None:
    assert player_game_stats({"3": []}, GAME_DATE) is None
    assert player_game_stats({"3": [{"date": "2026-11-04", "stats": {}}]}, GAME_DATE) == {}
    assert player_game_stats({"3": [{"date": "2026-11-04", "stats": None}]}, GAME_DATE) == {}


@pytest.mark.parametrize(
    "payload",
    [
        {"3": {"date": "2026-11-04"}},
        {"3": ["row"]},
        {"3": [{"date": "2026-11-04", "stats": "bad"}]},
        {"3": [{"date": "2026-11-04", "stats": {}}], "4": [{"date": "2026-11-04", "stats": {}}]},
    ],
)
def test_rejects_untrustworthy_payloads(payload: dict[str, object]) -> None:
    with pytest.raises(SleeperGameStatsError):
        player_game_stats(payload, GAME_DATE)


def test_line_includes_fouls_and_defaults_omitted_counts_to_zero() -> None:
    assert box_score_line(STATS) == BoxScoreLine(
        points=26,
        rebounds=15,
        assists=2,
        steals=1,
        turnovers=4,
        technical_fouls=1,
    )
    assert box_score_line({"ff": 2.0}).flagrant_fouls == 2


@pytest.mark.parametrize("value", [-1.0, 1.5, True, "2", None])
def test_line_rejects_values_that_are_not_counts(value: object) -> None:
    with pytest.raises(SleeperGameStatsError):
        box_score_line({"pts": value})
