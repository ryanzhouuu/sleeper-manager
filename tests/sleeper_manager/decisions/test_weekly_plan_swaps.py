"""Executable swaps respect full benches, intermediate positions, and game locks."""

from dataclasses import replace
from datetime import timedelta

import pytest

from sleeper_manager.decisions._weekly_plan_moves import plan_moves
from sleeper_manager.decisions.weekly_plan import WeeklyPlanError, build_weekly_plan
from sleeper_manager.domain.eligibility import eligible_for_slot
from sleeper_manager.domain.lineup_moves import apply_lineup_moves
from sleeper_manager.domain.planning import (
    FixedSlot,
    ObservedStarter,
    PlanningGameStatus,
    PlanStatus,
    StarterSlot,
)
from tests.sleeper_manager.decisions.weekly_plan_support import NOW, _opportunity, _planned, _state

START = NOW + timedelta(hours=1)


def test_center_util_exchange_uses_one_swap_with_a_full_bench() -> None:
    state = _state(
        tuple(
            _opportunity(player, player, START, ("C",), ((20, 1),))
            for player in ("jokic", "embiid", "bench")
        ),
        observed=(ObservedStarter(0, "jokic", ("C",)), ObservedStarter(1, "embiid", ("C",))),
        starter_slots=(StarterSlot(0, "C"), StarterSlot(1, "UTIL")),
    )
    state = replace(state, bench_capacity=1)

    moves = plan_moves(
        state, (_planned(0, "C", "embiid"), _planned(1, "UTIL", "jokic")), START, timedelta(0)
    )

    assert len(moves) == 1
    assert {moves[0].player_id, moves[0].swap_player_id} == {"jokic", "embiid"}
    assert moves[0].source_slot_index is not None
    assert moves[0].target_slot_index is not None


def test_bench_substitution_is_atomic_even_when_the_bench_is_full() -> None:
    state = replace(
        _state(
            tuple(
                _opportunity(player, player, START, ("PG",), ((value, 1),))
                for player, value in (("outgoing", 10), ("incoming", 40))
            ),
            observed=(ObservedStarter(0, "outgoing", ("PG",)),),
            starter_slots=(StarterSlot(0, "G"),),
        ),
        bench_capacity=1,
    )

    plan = build_weekly_plan(state)

    assert plan.status is PlanStatus.ACTION_REQUIRED
    assert len(plan.moves) == 1
    assert plan.moves[0].player_id == "incoming"
    assert plan.moves[0].swap_player_id == "outgoing"


def test_rotation_chooses_swaps_with_legal_intermediate_positions() -> None:
    positions = {"a": ("PG", "SG"), "b": ("SG",), "c": ("PG",)}
    state = replace(
        _state(
            tuple(
                _opportunity(player, player, START, allowed, ((20, 1),))
                for player, allowed in positions.items()
            ),
            observed=tuple(ObservedStarter(i, p, positions[p]) for i, p in enumerate(positions)),
            starter_slots=(StarterSlot(0, "PG"), StarterSlot(1, "SG"), StarterSlot(2, "G")),
        ),
        bench_capacity=0,
    )
    desired = (_planned(0, "PG", "c"), _planned(1, "SG", "a"), _planned(2, "G", "b"))

    moves = plan_moves(state, desired, START, timedelta(0))

    assert len(moves) == 2
    lineup = {starter.slot_index: starter.player_id for starter in state.observed_starters}
    for move in moves:
        assert move.swap_player_id is not None
        lineup = apply_lineup_moves((move,), lineup)
        assert all(
            eligible_for_slot(positions[player], state.starter_slots[index].position)
            for index, player in lineup.items()
            if player is not None
        )
    assert tuple(lineup.values()) == ("c", "a", "b")


def test_position_cycle_without_a_legal_first_swap_fails_closed() -> None:
    positions = {"a": ("PG", "SG"), "b": ("SG", "SF"), "c": ("SF", "PG")}
    state = replace(
        _state(
            tuple(
                _opportunity(player, player, START, allowed, ((20, 1),))
                for player, allowed in positions.items()
            ),
            observed=tuple(ObservedStarter(i, p, positions[p]) for i, p in enumerate(positions)),
            starter_slots=tuple(StarterSlot(i, slot) for i, slot in enumerate(("PG", "SG", "SF"))),
        ),
        bench_capacity=0,
    )

    with pytest.raises(WeeklyPlanError, match="legal lineup sequence"):
        plan_moves(
            state,
            tuple(
                _planned(i, slot, p)
                for i, slot, p in ((0, "PG", "c"), (1, "SG", "a"), (2, "SF", "b"))
            ),
            START,
            timedelta(0),
        )


def test_invalid_swap_destination_is_rejected() -> None:
    state = _state(
        (_opportunity("guard", "g", START, ("PG",), ((20, 1),)),),
        observed=(ObservedStarter(0, "guard", ("PG",)),),
        starter_slots=(StarterSlot(0, "G"), StarterSlot(1, "C")),
    )

    with pytest.raises(WeeklyPlanError, match="eligible"):
        plan_moves(state, (_planned(0, "G", None), _planned(1, "C", "guard")), START, timedelta(0))


def test_both_active_starters_can_swap_without_being_benched() -> None:
    state = _state(
        tuple(
            _opportunity(
                player,
                player,
                NOW - timedelta(hours=1),
                ("PG",),
                ((20, 1),),
                status=PlanningGameStatus.ACTIVE,
            )
            for player in ("a", "b")
        ),
        observed=(ObservedStarter(0, "a", ("PG",)), ObservedStarter(1, "b", ("PG",))),
    )

    moves = plan_moves(
        state, (_planned(0, "G", "b"), _planned(1, "UTIL", "a")), START, timedelta(0)
    )

    assert len(moves) == 1
    assert moves[0].swap_player_id is not None


def test_fixed_slot_is_not_available_as_a_rotation_buffer() -> None:
    state = _state(
        tuple(_opportunity(p, p, START, ("PG",), ((20, 1),)) for p in ("a", "b")),
        observed=(ObservedStarter(0, "a", ("PG",)), ObservedStarter(1, "b", ("PG",))),
        fixed=(FixedSlot(0, "G", "a", "a", 20, NOW, "lock", "fixture"),),
    )

    with pytest.raises(WeeklyPlanError, match="fixed"):
        plan_moves(state, (_planned(0, "G", "b"), _planned(1, "UTIL", "a")), START, timedelta(0))


@pytest.mark.parametrize("capacity", (None, 0))
def test_benching_alone_requires_a_verified_vacancy(capacity: int | None) -> None:
    state = replace(
        _state(
            (_opportunity("a", "a", START, ("PG",), ((20, 1),)),),
            observed=(ObservedStarter(0, "a", ("PG",)),),
        ),
        bench_capacity=capacity,
    )
    with pytest.raises(WeeklyPlanError, match="legal lineup sequence"):
        plan_moves(state, (_planned(0, "G", None), _planned(1, "UTIL", None)), START, timedelta(0))


def test_benching_alone_can_use_verified_bench_space() -> None:
    state = replace(
        _state(
            (_opportunity("a", "a", START, ("PG",), ((20, 1),)),),
            observed=(ObservedStarter(0, "a", ("PG",)),),
        ),
        bench_capacity=1,
    )
    moves = plan_moves(
        state, (_planned(0, "G", None), _planned(1, "UTIL", None)), START, timedelta(0)
    )
    assert len(moves) == 1 and moves[0].target_slot_index is None


def test_reserve_player_cannot_be_used_in_a_bench_swap() -> None:
    state = replace(
        _state(
            tuple(_opportunity(p, p, START, ("PG",), ((20, 1),)) for p in ("a", "ir")),
            observed=(ObservedStarter(0, "a", ("PG",)),),
        ),
        reserve_player_ids=("ir",),
    )
    with pytest.raises(WeeklyPlanError, match="reserve"):
        plan_moves(state, (_planned(0, "G", "ir"), _planned(1, "UTIL", None)), START, timedelta(0))


def test_unknown_capacity_can_reuse_a_vacancy_created_during_the_sequence() -> None:
    state = _state(
        (
            _opportunity("a", "a", START, ("PG",), ((20, 1),)),
            _opportunity("b", "b", START, ("SG",), ((20, 1),)),
        ),
        observed=(ObservedStarter(0, "a", ("PG",)),),
        starter_slots=(StarterSlot(0, "PG"), StarterSlot(1, "SG")),
    )

    moves = plan_moves(
        state, (_planned(0, "PG", None), _planned(1, "SG", "b")), START, timedelta(0)
    )

    assert [(move.player_id, move.source_slot_index, move.target_slot_index) for move in moves] == [
        ("b", None, 1),
        ("a", 0, None),
    ]


def test_full_bench_player_can_buffer_an_otherwise_impossible_rotation() -> None:
    positions = {
        "a": ("PG", "SG"),
        "b": ("SG", "SF"),
        "c": ("SF", "PG"),
        "buffer": ("PG", "SG", "SF"),
    }
    state = replace(
        _state(
            tuple(
                _opportunity(
                    player,
                    player,
                    START if player != "buffer" else NOW + timedelta(minutes=30),
                    allowed,
                    ((20, 1),),
                )
                for player, allowed in positions.items()
            ),
            observed=tuple(
                ObservedStarter(i, p, positions[p]) for i, p in enumerate(("a", "b", "c"))
            ),
            starter_slots=tuple(StarterSlot(i, slot) for i, slot in enumerate(("PG", "SG", "SF"))),
        ),
        bench_capacity=1,
    )
    target = (_planned(0, "PG", "c"), _planned(1, "SG", "a"), _planned(2, "SF", "b"))

    moves = plan_moves(state, target, START, timedelta(minutes=10))

    assert len(moves) == 4
    assert all(move.swap_player_id is not None for move in moves)
    assert any("buffer" in (move.player_id, move.swap_player_id) for move in moves)
    assert all(move.deadline == NOW + timedelta(minutes=20) for move in moves)
    lineup = {i: p for i, p in enumerate(("a", "b", "c"))}
    for move in moves:
        lineup = apply_lineup_moves((move,), lineup)
        assert all(
            eligible_for_slot(positions[player], state.starter_slots[index].position)
            for index, player in lineup.items()
            if player is not None
        )
    assert tuple(lineup.values()) == ("c", "a", "b")


def test_sequence_and_deadlines_are_independent_of_input_order() -> None:
    state = _state(
        tuple(_opportunity(p, p, START, ("PG",), ((20, 1),)) for p in ("a", "b")),
        observed=(ObservedStarter(0, "a", ("PG",)), ObservedStarter(1, "b", ("PG",))),
    )
    target = (_planned(0, "G", "b"), _planned(1, "UTIL", "a"))
    reordered = replace(
        state,
        roster_player_ids=tuple(reversed(state.roster_player_ids)),
        observed_starters=tuple(reversed(state.observed_starters)),
        starter_slots=tuple(reversed(state.starter_slots)),
        opportunities=tuple(reversed(state.opportunities)),
    )

    assert plan_moves(state, target, START, timedelta(0)) == plan_moves(
        reordered, tuple(reversed(target)), START, timedelta(0)
    )


def test_search_exhaustion_never_returns_partial_moves(monkeypatch: pytest.MonkeyPatch) -> None:
    from sleeper_manager.decisions import _lineup_move_search

    state = _state(
        tuple(_opportunity(p, p, START, ("PG",), ((20, 1),)) for p in ("a", "b")),
        observed=(ObservedStarter(0, "a", ("PG",)), ObservedStarter(1, "b", ("PG",))),
    )
    monkeypatch.setattr(_lineup_move_search, "MAX_SEARCH_STATES", 1)

    with pytest.raises(WeeklyPlanError, match="search limit"):
        plan_moves(state, (_planned(0, "G", "b"), _planned(1, "UTIL", "a")), START, timedelta(0))
