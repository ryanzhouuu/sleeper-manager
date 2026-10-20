"""Atomic exchanges must validate both current occupants before changing slots."""

from dataclasses import replace

import pytest

from sleeper_manager.domain.planning import FixedSlot, LineupMove, PlanningStateError
from tests.sleeper_manager.decisions.weekly_plan_support import DEADLINE, NOW, _plan, _planned


def test_starter_swap_is_one_atomic_action() -> None:
    move = LineupMove("p1", 0, 1, DEADLINE, swap_player_id="p2")
    plan = _plan(moves=(move,))

    assert plan.moves == (move,)
    assert plan.material_hash != _plan().material_hash


@pytest.mark.parametrize("reverse", (False, True))
def test_bench_exchange_does_not_need_an_empty_slot(reverse: bool) -> None:
    move = (
        LineupMove("p1", 0, None, DEADLINE, swap_player_id="p2")
        if reverse
        else LineupMove("p2", None, 0, DEADLINE, swap_player_id="p1")
    )
    plan = _plan(
        observed_assignments=(_planned(0, "G", "p1"),),
        desired_assignments=(_planned(0, "G", "p2"),),
        moves=(move,),
    )

    assert len(plan.moves) == 1


@pytest.mark.parametrize("counterpart", ("p1", "", " "))
def test_swap_requires_a_distinct_named_participant(counterpart: str) -> None:
    with pytest.raises(PlanningStateError):
        LineupMove("p1", 0, 1, DEADLINE, swap_player_id=counterpart)


def test_swap_rejects_stale_target_occupant() -> None:
    with pytest.raises(PlanningStateError, match="swap target"):
        _plan(moves=(LineupMove("p1", 0, 1, DEADLINE, swap_player_id="missing"),))


def test_swap_rejects_a_starter_disguised_as_a_bench_player() -> None:
    with pytest.raises(PlanningStateError, match="benched player"):
        _plan(moves=(LineupMove("p1", 0, None, DEADLINE, swap_player_id="p2"),))


def test_swap_rejects_unknown_slots() -> None:
    with pytest.raises(PlanningStateError, match="unknown slot"):
        _plan(moves=(LineupMove("p1", 0, 8, DEADLINE, swap_player_id="p2"),))


def test_swap_cannot_round_trip_through_a_locked_slot() -> None:
    move = LineupMove("p1", 0, 1, DEADLINE, swap_player_id="p2")
    with pytest.raises(PlanningStateError, match="Locked slots"):
        _plan(
            desired_assignments=_plan().observed_assignments,
            moves=(move, replace(move, player_id="p2", swap_player_id="p1")),
            fixed_slots=(FixedSlot(0, "G", "p1", "g", 5, NOW, "lock", "fixture"),),
        )


def test_swap_identity_includes_the_other_participant() -> None:
    first = _plan(moves=(LineupMove("p1", 0, 1, DEADLINE, swap_player_id="p2"),))
    second = _plan(
        observed_assignments=(_planned(0, "G", "p1"), _planned(1, "UTIL", "p3")),
        desired_assignments=(_planned(0, "G", "p3"), _planned(1, "UTIL", "p1")),
        moves=(LineupMove("p1", 0, 1, DEADLINE, swap_player_id="p3"),),
    )

    assert first.material_hash != second.material_hash
    assert first.plan_id != second.plan_id
