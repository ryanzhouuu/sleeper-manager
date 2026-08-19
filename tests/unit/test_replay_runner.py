from datetime import UTC, datetime, timedelta

import pytest

from sleeper_manager.backtesting.replay import (
    ReplayEventKind,
    ReplayGame,
    ReplayGameStatus,
    ReplayRunnerError,
    ReplayState,
    ReplayTransaction,
    build_chronological_events,
)
from sleeper_manager.domain.planning import PlanningReasonCode

BASE = datetime(2026, 1, 5, 12, tzinfo=UTC)


def _game(
    game_id: str,
    start: datetime,
    *,
    final_time: datetime | None,
    status: ReplayGameStatus = ReplayGameStatus.FINAL,
) -> ReplayGame:
    return ReplayGame(game_id, start, final_time, 1, ("home", "away"), status)


def test_events_group_simultaneous_tipoffs_and_append_week_end() -> None:
    first = _game("g1", BASE, final_time=BASE + timedelta(hours=2))
    second = _game("g2", BASE, final_time=BASE + timedelta(hours=2))
    events = build_chronological_events(
        ReplayState(("G",), (second, first), ()),
        planning_cutoffs=(BASE - timedelta(minutes=1),),
    )

    tipoffs = [event for event in events if event.kind is ReplayEventKind.TIPOFF_BATCH]
    assert len(tipoffs) == 1
    assert tipoffs[0].game_ids == ("g1", "g2")
    assert events[-1].kind is ReplayEventKind.WEEK_END
    assert [event.kind for event in events[:3]] == [
        ReplayEventKind.PLANNING_CUTOFF,
        ReplayEventKind.TIPOFF_BATCH,
        ReplayEventKind.GAME_FINALIZATION,
    ]


def test_conflicting_same_time_transactions_fail_closed() -> None:
    at = BASE + timedelta(hours=1)
    state = ReplayState(("G",), (), ())
    transactions = (
        ReplayTransaction("tx-a", at, 1, drops=("p1",)),
        ReplayTransaction("tx-b", at, 1, adds=("p1",)),
    )

    with pytest.raises(ReplayRunnerError) as error:
        build_chronological_events(state, transactions=transactions)

    assert error.value.reason is PlanningReasonCode.AMBIGUOUS_EVENT_ORDER
