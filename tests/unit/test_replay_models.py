import pytest

from sleeper_manager.backtesting.replay.models import ReplayPlayerGame, TeamWeekComparison


def test_replay_player_game_rejects_nonfinite_scores() -> None:
    with pytest.raises(ValueError, match="finite"):
        ReplayPlayerGame("p1", "provider-p1", "g1", 1, True, ("PG",), float("nan"))


def test_team_week_comparison_rejects_negative_regret() -> None:
    with pytest.raises(ValueError, match="negative"):
        TeamWeekComparison(10, 11, -1, 1.1, ())
