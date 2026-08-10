import json
from pathlib import Path
from typing import Any

import pytest

from sleeper_manager.domain.scoring import (
    BoxScoreLine,
    ScoreParityCase,
    ScoringPolicy,
    compare_score_parity,
)

FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "sleeper" / "player_game_totals.json"
LEAGUE_SCORING = ScoringPolicy.from_sleeper(
    {
        "ast": 1.5,
        "blk": 2,
        "bonus_ast_15p": 2,
        "bonus_pt_40p": 2,
        "bonus_pt_50p": 2,
        "bonus_reb_20p": 2,
        "dd": 1,
        "ff": -2,
        "pts": 1,
        "reb": 1.2,
        "stl": 2,
        "td": 2,
        "tf": -2,
        "to": -1,
        "tpm": 0.5,
    }
)


def _fixture_cases() -> tuple[ScoreParityCase, ...]:
    records: list[ScoreParityCase] = []
    raw_records: list[dict[str, Any]] = json.loads(FIXTURE_PATH.read_text())
    for record in raw_records:
        records.append(
            ScoreParityCase(
                player_id=record["player_id"],
                game_id=record["game_id"],
                box_score=BoxScoreLine(**record["box_score"]),
                sleeper_fantasy_points=record["sleeper_fantasy_points"],
            )
        )
    return tuple(records)


def test_sanitized_sleeper_totals_match_calculated_scores() -> None:
    results = [compare_score_parity(case, LEAGUE_SCORING) for case in _fixture_cases()]

    assert all(result.matches for result in results)
    assert [result.calculated_fantasy_points for result in results] == [
        38.2,
        27.0,
        92.0,
        107.5,
        -9.0,
    ]


def test_parity_accepts_small_rounding_difference() -> None:
    case = _fixture_cases()[0]
    case = ScoreParityCase(
        player_id=case.player_id,
        game_id=case.game_id,
        box_score=case.box_score,
        sleeper_fantasy_points=38.211,
    )

    result = compare_score_parity(case, LEAGUE_SCORING)

    assert result.difference == -0.01
    assert result.matches


def test_parity_reports_material_difference() -> None:
    case = _fixture_cases()[0]
    case = ScoreParityCase(
        player_id=case.player_id,
        game_id=case.game_id,
        box_score=case.box_score,
        sleeper_fantasy_points=38.22,
    )

    result = compare_score_parity(case, LEAGUE_SCORING)

    assert result.difference == -0.02
    assert not result.matches


@pytest.mark.parametrize("tolerance", [-0.01, float("inf"), float("nan")])
def test_parity_rejects_invalid_tolerance(tolerance: float) -> None:
    with pytest.raises(ValueError, match="tolerance"):
        compare_score_parity(_fixture_cases()[0], LEAGUE_SCORING, tolerance=tolerance)
