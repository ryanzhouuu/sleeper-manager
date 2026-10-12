"""Preserve active players when locking a previously started bench player."""

from sleeper_manager.backtesting.experiments.full_advisor_replay_legality import realign_for_lock
from sleeper_manager.backtesting.replay.state import ReplayState
from tests.sleeper_manager.backtesting.experiments.lock_in_diagnostic_support import _team_week


def test_completed_player_can_return_from_bench_to_lock() -> None:
    week = _team_week()
    state = ReplayState(week.starter_slots, week.games, week.player_games)
    result = realign_for_lock(
        player_id="p1",
        target_slot=0,
        lineup={1: "p2"},
        active={"p2": 1},
        replay_state=state,
        team_week=week,
    )
    assert result == {0: "p1", 1: "p2"}


def test_active_player_can_realign_to_another_eligible_starter_slot() -> None:
    week = _team_week()
    state = ReplayState(week.starter_slots, week.games, week.player_games)
    result = realign_for_lock(
        player_id="p1",
        target_slot=0,
        lineup={0: "p2"},
        active={"p2": 0},
        replay_state=state,
        team_week=week,
    )
    assert result == {0: "p1", 1: "p2"}
