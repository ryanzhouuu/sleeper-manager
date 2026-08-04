from sleeper_manager.domain.scoring import (
    BoxScoreLine,
    ScoringSettings,
    calculate_fantasy_points,
)

LEAGUE_SCORING = ScoringSettings.from_sleeper(
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


def test_reproduces_verified_sabonis_triple_double() -> None:
    line = BoxScoreLine(
        points=29,
        rebounds=12,
        assists=10,
        steals=1,
        blocks=1,
        turnovers=2,
        three_pointers_made=1,
    )

    assert calculate_fantasy_points(line, LEAGUE_SCORING) == 63.9


def test_applies_assist_bonus_and_technical_foul() -> None:
    line = BoxScoreLine(
        points=22,
        rebounds=1,
        assists=15,
        steals=2,
        turnovers=1,
        three_pointers_made=1,
        technical_fouls=1,
    )

    assert calculate_fantasy_points(line, LEAGUE_SCORING) == 50.2
