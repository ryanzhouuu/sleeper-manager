from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from sleeper_manager.backtesting.replay import (
    ReplayConfig,
    ReplayDecision,
    ReplayEventKind,
    ReplayGame,
    ReplayGameStatus,
    ReplayPlayerGame,
    ReplayRunnerError,
    ReplayState,
    ReplayTransaction,
    build_chronological_events,
    run_chronological_replay,
)
from sleeper_manager.backtesting.replay.models import LockedSlot
from sleeper_manager.domain.planning import PlanningReasonCode
from sleeper_manager.domain.projection import ProjectionDistribution, ProjectionSnapshot

BASE = datetime(2026, 1, 5, 12, tzinfo=UTC)


def _projection(player_id: str, game_id: str, available_as_of: datetime) -> ProjectionSnapshot:
    return ProjectionSnapshot(
        player_id=player_id,
        game_id=game_id,
        available_as_of=available_as_of,
        model_version="fixture-model",
        input_version="fixture-inputs",
        scoring_policy_version="fixture-scoring",
        distribution=ProjectionDistribution.from_weighted_observations(((10, 1.0),)),
        reasons=(),
    )


def _game(
    game_id: str,
    start: datetime,
    *,
    final_time: datetime | None,
    status: ReplayGameStatus = ReplayGameStatus.FINAL,
) -> ReplayGame:
    return ReplayGame(game_id, start, final_time, 1, ("home", "away"), status)


def _state(*, future_score: float = 20) -> ReplayState:
    g1_start = BASE - timedelta(hours=4)
    g2_start = BASE + timedelta(hours=2)
    games = (
        _game("g1", g1_start, final_time=BASE - timedelta(hours=2)),
        _game("g2", g2_start, final_time=BASE + timedelta(hours=4)),
    )
    player_games = (
        ReplayPlayerGame(
            "p1",
            "provider-p1",
            "g1",
            1,
            True,
            ("G",),
            10,
            _projection("p1", "g1", BASE - timedelta(hours=5)),
        ),
        ReplayPlayerGame(
            "p2",
            "provider-p2",
            "g2",
            1,
            True,
            ("G",),
            future_score,
            _projection("p2", "g2", BASE + timedelta(hours=3)),
        ),
    )
    return ReplayState(("G",), games, player_games)


def _config() -> ReplayConfig:
    return ReplayConfig(starter_slots=("G",), league_id="league-1", week=1, roster_id=1)


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


def test_future_scores_and_projections_cannot_change_an_earlier_snapshot() -> None:
    early = BASE + timedelta(hours=1, minutes=30)
    late = BASE + timedelta(hours=5)
    first_trace = run_chronological_replay(
        _state(future_score=20),
        config=_config(),
        planning_cutoffs=(early, late),
    )
    second_trace = run_chronological_replay(
        _state(future_score=200),
        config=_config(),
        planning_cutoffs=(early, late),
    )

    assert first_trace.planning_snapshots[0].state == second_trace.planning_snapshots[0].state
    early_state = first_trace.planning_snapshots[0].state
    late_state = first_trace.planning_snapshots[1].state
    early_opportunities = {item.game_id: item for item in early_state.opportunities}
    late_opportunities = {item.game_id: item for item in late_state.opportunities}
    assert early_opportunities["g1"].completed_fantasy_score == 10
    assert early_opportunities["g2"].completed_fantasy_score is None
    assert early_opportunities["g2"].projection is None
    assert late_opportunities["g2"].completed_fantasy_score == 20
    assert late_opportunities["g2"].projection is not None


def test_transaction_effects_update_roster_before_later_planning_cutoff() -> None:
    transaction_time = BASE + timedelta(hours=1)
    state = _state()
    transaction = ReplayTransaction(
        "tx-1",
        transaction_time,
        1,
        adds=("p2",),
        drops=("p1",),
    )
    trace = run_chronological_replay(
        state,
        config=_config(),
        transactions=(transaction,),
        planning_cutoffs=(BASE, BASE + timedelta(hours=2)),
    )

    assert trace.planning_snapshots[0].state.roster_player_ids == ("p1",)
    assert trace.planning_snapshots[1].state.roster_player_ids == ("p2",)
    early_opportunity = trace.planning_snapshots[0].state.opportunities[1]
    late_opportunity = trace.planning_snapshots[1].state.opportunities[1]
    assert early_opportunity.rostered_at_tipoff is False
    assert late_opportunity.rostered_at_tipoff is True
    transaction_events = [
        event for event in trace.events if event.kind is ReplayEventKind.TRANSACTION_EFFECT
    ]
    assert transaction_events[0].transaction == transaction


def test_future_locks_and_passes_are_hidden_until_their_decision_time() -> None:
    state = _state()
    lock_time = BASE + timedelta(hours=1)
    locked_state = replace(
        state,
        locked_slots=(LockedSlot(0, "G", "p1", "g1", 10, lock_time),),
        decisions=(
            ReplayDecision(
                lock_time,
                "lock",
                "p1",
                "g1",
                0,
                "fixture-inputs",
                10,
                10,
                "fixture lock",
            ),
            ReplayDecision(
                lock_time,
                "pass",
                "p2",
                "g2",
                None,
                "fixture-inputs",
                0,
                0,
                "fixture pass",
            ),
        ),
    )
    trace = run_chronological_replay(
        locked_state,
        config=_config(),
        planning_cutoffs=(BASE, BASE + timedelta(hours=2)),
    )

    before, after = (snapshot.state for snapshot in trace.planning_snapshots)
    assert before.fixed_slots == ()
    assert before.passed_opportunities == ()
    assert after.fixed_slots[0].player_id == "p1"
    assert (after.passed_opportunities[0].player_id, after.passed_opportunities[0].game_id) == (
        "p2",
        "g2",
    )


def test_replay_trace_is_deterministic_and_does_not_mutate_input_state() -> None:
    state = _state()
    original = state
    first = run_chronological_replay(
        state,
        config=_config(),
        planning_cutoffs=(BASE + timedelta(hours=1),),
    )
    second = run_chronological_replay(
        state,
        config=_config(),
        planning_cutoffs=(BASE + timedelta(hours=1),),
    )

    assert state == original
    assert first == second
    assert first.fingerprint == second.fingerprint
    assert first.to_dict()["fingerprint"] == first.fingerprint
