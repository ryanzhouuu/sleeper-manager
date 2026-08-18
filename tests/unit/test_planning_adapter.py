from datetime import UTC, datetime

from sleeper_manager.backtesting.planning_adapter import team_week_state_from_replay
from sleeper_manager.backtesting.replay import ReplayConfig, ReplayState
from sleeper_manager.backtesting.replay_models import (
    LockedSlot,
    ReplayDecision,
    ReplayGame,
    ReplayGameStatus,
    ReplayPlayerGame,
)
from sleeper_manager.domain.planning import PlanningGameStatus, PlanningReasonCode

DECISION_TIME = datetime(2026, 1, 5, 12, tzinfo=UTC)


def _replay_state(*, future_score: float = 99) -> ReplayState:
    games = (
        ReplayGame(
            "g1",
            datetime(2026, 1, 5, 8, tzinfo=UTC),
            datetime(2026, 1, 5, 10, tzinfo=UTC),
            1,
            ("a", "b"),
            ReplayGameStatus.FINAL,
        ),
        ReplayGame(
            "g2",
            datetime(2026, 1, 5, 14, tzinfo=UTC),
            datetime(2026, 1, 5, 16, tzinfo=UTC),
            1,
            ("a", "b"),
            ReplayGameStatus.FINAL,
        ),
    )
    player_games = (
        ReplayPlayerGame("p1", "provider-p1", "g1", 1, True, ("PG",), 10),
        ReplayPlayerGame("p1", "provider-p1", "g2", 1, True, ("PG",), future_score),
    )
    return ReplayState(("G",), games, player_games)


def _config() -> ReplayConfig:
    return ReplayConfig(starter_slots=("G",), league_id="league-1", week=1, roster_id=1)


def test_adapter_only_exposes_scores_observable_at_decision_time() -> None:
    state = team_week_state_from_replay(
        _replay_state(),
        config=_config(),
        decision_time=DECISION_TIME,
    )
    opportunities = {opportunity.game_id: opportunity for opportunity in state.opportunities}

    assert opportunities["g1"].status is PlanningGameStatus.FINAL
    assert opportunities["g1"].completed_fantasy_score == 10
    assert opportunities["g2"].completed_fantasy_score is None
    assert tuple(opportunity.game_id for opportunity in state.completed_opportunities) == ("g1",)


def test_future_realized_outcomes_do_not_change_earlier_state() -> None:
    first = team_week_state_from_replay(
        _replay_state(future_score=1),
        config=_config(),
        decision_time=DECISION_TIME,
    )
    second = team_week_state_from_replay(
        _replay_state(future_score=1000),
        config=_config(),
        decision_time=DECISION_TIME,
    )

    assert first == second


def test_duplicate_position_labels_retain_distinct_slot_indices() -> None:
    replay = ReplayState(
        ("G", "G"),
        (_replay_state().games[0],),
        (ReplayPlayerGame("p1", "provider-p1", "g1", 1, True, ("PG",), 10),),
    )
    state = team_week_state_from_replay(
        replay,
        config=ReplayConfig(starter_slots=("G", "G"), roster_id=1),
        decision_time=DECISION_TIME,
        observed_starter_ids=("p1",),
    )

    assert tuple(slot.index for slot in state.starter_slots) == (0, 1)
    assert tuple(slot.position for slot in state.starter_slots) == ("G", "G")
    assert state.observed_starters[0].slot_index == 0


def test_duplicate_game_records_are_retained_as_a_blocking_reason() -> None:
    replay = _replay_state()
    replay = ReplayState(
        replay.starter_slots,
        replay.games + (replay.games[0],),
        replay.player_games,
    )

    state = team_week_state_from_replay(replay, config=_config(), decision_time=DECISION_TIME)

    assert state.is_blocked
    assert PlanningReasonCode.AMBIGUOUS_EVENT_ORDER in state.blocking_reasons


def test_locked_and_passed_state_round_trips_into_shared_records() -> None:
    replay = _replay_state()
    replay = ReplayState(
        replay.starter_slots,
        replay.games,
        replay.player_games,
        locked_slots=(LockedSlot(0, "G", "p1", "g1", 10, DECISION_TIME),),
        decisions=(
            ReplayDecision(
                DECISION_TIME,
                "lock",
                "p1",
                "g1",
                0,
                "replay-inputs-v1",
                10,
                10,
                "accepted",
            ),
            ReplayDecision(
                DECISION_TIME,
                "pass",
                "p1",
                "g2",
                None,
                "replay-inputs-v1",
                0,
                0,
                "preserved future flexibility",
            ),
        ),
    )
    state = team_week_state_from_replay(replay, config=_config(), decision_time=DECISION_TIME)

    assert state.fixed_slots[0].slot_index == 0
    assert state.fixed_slots[0].accepted_fantasy_score == 10
    assert (state.passed_opportunities[0].player_id, state.passed_opportunities[0].game_id) == (
        "p1",
        "g2",
    )
    assert state.open_slot_indices == ()
