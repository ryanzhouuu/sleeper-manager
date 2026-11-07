import json
from typing import Any

import pytest

from sleeper_manager.integrations.sleeper.matchup_legs import (
    MatchupLeg,
    MatchupLegsError,
    parse_matchup_legs,
)
from tests.paths import FIXTURES_DIR


def _fixture(path: str) -> Any:
    return json.loads((FIXTURES_DIR / path).read_text())


def _legs() -> tuple[MatchupLeg, ...]:
    return parse_matchup_legs(_fixture("sleeper/matchup_legs_raw.json")["data"]["matchup_legs_raw"])


def test_parse_matchup_legs_reads_locks_and_points() -> None:
    mine = next(leg for leg in _legs() if leg.roster_id == 6)

    assert mine.starters[0] == "2090"
    assert mine.starters_games["2090"] == "g-nop"
    assert mine.matchup_id == 2
    assert mine.points is None


def test_parse_matchup_legs_treats_null_starters_games_as_no_locks() -> None:
    opponent = next(leg for leg in _legs() if leg.roster_id == 7)

    assert dict(opponent.starters_games) == {}


def test_parse_matchup_legs_reads_final_points() -> None:
    (leg,) = parse_matchup_legs(
        [{"roster_id": 6, "matchup_id": 1, "points": 383, "starters": [], "starters_games": {}}]
    )

    assert leg.points == 383.0


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        [{"roster_id": "6"}],
        [{"roster_id": True, "starters": [], "starters_games": {}}],
        [{"roster_id": 6, "starters": "x"}],
        [{"roster_id": 6, "starters": [1], "starters_games": {}}],
        [{"roster_id": 6, "starters": [], "starters_games": {"p": 1}}],
        [{"roster_id": 6, "starters": [], "starters_games": [], "points": None}],
        [{"roster_id": 6, "starters": [], "starters_games": {}, "points": "383"}],
        [{"roster_id": 6, "starters": [], "starters_games": {}, "matchup_id": "2"}],
        [
            {"roster_id": 6, "starters": [], "starters_games": {}},
            {"roster_id": 6, "starters": [], "starters_games": {}},
        ],
    ],
)
def test_parse_matchup_legs_rejects_bad_shapes(bad: object) -> None:
    with pytest.raises(MatchupLegsError):
        parse_matchup_legs(bad)
