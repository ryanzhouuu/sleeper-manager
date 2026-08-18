from datetime import UTC, datetime

from sleeper_manager.backtesting.replay import ReplayState
from sleeper_manager.backtesting.replay.models import ReplayGame, ReplayGameStatus, ReplayPlayerGame


def test_replay_state_enforces_next_game_deadline_and_permanent_slot_lock() -> None:
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
        ReplayPlayerGame("p1", "p1", "g1", 1, True, ("PG",), 10),
        ReplayPlayerGame("p1", "p1", "g2", 1, True, ("PG",), 4),
    )
    state = ReplayState(("PG",), games, player_games)
    candidate = state.candidate_at(player_games[0], datetime(2026, 1, 5, 4, tzinfo=UTC))
    assert candidate is not None
    locked = state.lock(candidate, slot_index=0, at=datetime(2026, 1, 5, 4, tzinfo=UTC))
    assert locked.open_slot_indices == ()
    assert locked.candidate_at(player_games[1], datetime(2026, 1, 7, 4, tzinfo=UTC)) is None
