import pytest

from sleeper_manager.domain.scoring import (
    BoxScoreLine,
    ScoringCompatibilityError,
    ScoringPolicy,
    calculate_fantasy_points,
    calculate_score_breakdown,
)

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


def test_rejects_unknown_nonzero_scoring_field() -> None:
    with pytest.raises(ScoringCompatibilityError, match="personal_foul"):
        ScoringPolicy.from_sleeper({"pts": 1, "personal_foul": 1})


def test_allows_unknown_zero_scoring_field() -> None:
    policy = ScoringPolicy.from_sleeper({"pts": 1, "future_field": 0})

    assert policy.points == 1


def test_rejects_nonfinite_policy_weights() -> None:
    with pytest.raises(ScoringCompatibilityError, match="pts.*finite"):
        ScoringPolicy(points=float("nan"))


def test_policy_is_versioned_and_breakdown_lists_contributors() -> None:
    line = BoxScoreLine(
        points=22,
        rebounds=1,
        assists=15,
        steals=2,
        turnovers=1,
        three_pointers_made=1,
        technical_fouls=1,
    )

    breakdown = calculate_score_breakdown(line, LEAGUE_SCORING)

    assert LEAGUE_SCORING.version.startswith("scoring-policy-v1-")
    assert len(LEAGUE_SCORING.fingerprint) == 64
    assert breakdown.total == 50.2
    assert breakdown.consumed_fields == (
        "pts",
        "reb",
        "ast",
        "stl",
        "to",
        "tpm",
        "tf",
        "dd",
        "bonus_ast_15p",
    )
    assert breakdown.contributions[-1].contribution == 2


def test_threshold_bonuses_stack_at_upper_boundaries() -> None:
    line = BoxScoreLine(points=50, rebounds=20, assists=15)

    breakdown = calculate_score_breakdown(line, LEAGUE_SCORING)

    assert breakdown.total == 107.5
    assert breakdown.consumed_fields[-5:] == (
        "td",
        "bonus_pt_40p",
        "bonus_pt_50p",
        "bonus_ast_15p",
        "bonus_reb_20p",
    )
