from datetime import UTC, datetime

from sleeper_manager.backtesting.replay import (
    ReplayConfig,
    compare_team_week,
    oracle_team_week_result,
)
from sleeper_manager.backtesting.replay_models import (
    ReplayGame,
    ReplayGameStatus,
    ReplayPlayerGame,
    TeamWeekReplayResult,
)


def test_oracle_selects_realized_best_game_and_comparison_handles_zero_denominator() -> None:
    games = (
        ReplayGame(
            "g1",
            datetime(2026, 1, 5, 1, tzinfo=UTC),
            datetime(2026, 1, 5, 3, tzinfo=UTC),
            1,
            ("a", "b"),
            ReplayGameStatus.FINAL,
        ),
        ReplayGame(
            "g2",
            datetime(2026, 1, 7, 1, tzinfo=UTC),
            datetime(2026, 1, 7, 3, tzinfo=UTC),
            1,
            ("a", "b"),
            ReplayGameStatus.FINAL,
        ),
    )
    player_games = (
        ReplayPlayerGame("p1", "provider-p1", "g1", 1, True, ("PG",), 12),
        ReplayPlayerGame("p1", "provider-p1", "g2", 1, True, ("PG",), 8),
    )
    oracle = oracle_team_week_result(
        player_games,
        games=games,
        config=ReplayConfig(starter_slots=("PG",), league_id="league", roster_id=1),
    )
    assert oracle.realized_score == 12
    assert oracle.locked_slots[0].game_id == "g1"

    zero = TeamWeekReplayResult("league", 1, 1, "oracle", 0, (), (), (), "exact", "complete")
    comparison = compare_team_week(zero, zero)
    assert comparison.lock_in_regret == 0
    assert comparison.score_capture is None
