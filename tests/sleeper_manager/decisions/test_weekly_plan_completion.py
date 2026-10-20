"""Exercise full-lineup matching independently of projection optimization."""

from dataclasses import replace
from datetime import timedelta

import pytest

from sleeper_manager.decisions._weekly_plan_completion import complete_assignment
from sleeper_manager.decisions.lineup import AssignmentResult, SlotAssignment
from sleeper_manager.decisions.weekly_plan import WeeklyPlanError
from sleeper_manager.domain.planning import (
    FixedSlot,
    ObservedStarter,
    PlanningGameStatus,
    StarterSlot,
)
from tests.sleeper_manager.decisions.weekly_plan_support import NOW, _opportunity, _state


def _assignment(player_id: str, index: int, position: str = "UTIL") -> AssignmentResult:
    """Represent an already selected batch placement, with its sampled score."""
    return AssignmentResult(
        20.0, (SlotAssignment(index, position, "selected", player_id, "early", 20.0),)
    )


def test_completion_preserves_later_starters_without_sampling_them() -> None:
    early = _opportunity("early", "early", NOW + timedelta(hours=1), ("PG",), ((20, 1),))
    later = _opportunity("later", "later", NOW + timedelta(hours=2), ("C",), ((30, 1),))
    state = _state(
        (early, later),
        observed=(ObservedStarter(0, "early", ("PG",)), ObservedStarter(1, "later", ("C",))),
    )

    completed = complete_assignment(state, (early,), _assignment("early", 0, "G"))

    assert tuple(item.player_id for item in completed) == ("early", "later")
    assert completed[0].score == 20
    assert completed[1].score == 0
    assert completed[1].game_id is None


def test_completion_realigns_a_later_center_into_util() -> None:
    early = _opportunity("early", "early", NOW + timedelta(hours=1), ("C",), ((20, 1),))
    later = _opportunity("later", "later", NOW + timedelta(hours=2), ("C",), ((30, 1),))
    state = _state(
        (early, later),
        observed=(ObservedStarter(0, "later", ("C",)), ObservedStarter(1, "early", ("C",))),
        starter_slots=(StarterSlot(0, "C"), StarterSlot(1, "UTIL")),
    )

    completed = complete_assignment(state, (early,), _assignment("early", 0, "C"))

    assert tuple(item.player_id for item in completed) == ("early", "later")


def test_completion_retains_maximum_cardinality_before_unchanged_slots() -> None:
    early = _opportunity("early", "early", NOW + timedelta(hours=1), ("PG",), ((20, 1),))
    state = _state(
        (early,),
        starter_slots=(StarterSlot(0, "PG"), StarterSlot(1, "C"), StarterSlot(2, "UTIL")),
    )
    state = replace(
        state,
        roster_player_ids=("early", "flexible", "guard"),
        observed_starters=(
            ObservedStarter(0, "flexible", ("PG", "C")),
            ObservedStarter(2, "guard", ("PG",)),
        ),
    )

    completed = complete_assignment(state, (early,), _assignment("early", 0, "PG"))

    assert tuple(item.player_id for item in completed) == ("early", "flexible", "guard")


def test_completion_allows_necessary_replacement_without_restoring_batch_players() -> None:
    batch = tuple(
        _opportunity(player, "early", NOW + timedelta(hours=1), ("PG",), ((20, 1),))
        for player in ("selected", "skipped")
    )
    state = _state(batch)
    state = replace(
        state,
        roster_player_ids=("selected", "skipped", "later"),
        observed_starters=(
            ObservedStarter(0, "later", ("PG",)),
            ObservedStarter(1, "skipped", ("PG",)),
        ),
    )

    completed = complete_assignment(state, batch, _assignment("selected", 0, "G"))

    assert tuple(item.player_id for item in completed) == ("selected", "later")
    single_slot = replace(
        state,
        starter_slots=(StarterSlot(0, "G"),),
        observed_starters=state.observed_starters[:1],
    )
    assert tuple(
        item.player_id
        for item in complete_assignment(single_slot, batch, _assignment("selected", 0, "G"))
    ) == ("selected",)


def test_completion_keeps_empty_slots_and_does_not_introduce_bench_players() -> None:
    early = _opportunity("early", "early", NOW + timedelta(hours=1), ("PG",), ((20, 1),))
    later = _opportunity("bench", "later", NOW + timedelta(hours=2), ("PG",), ((30, 1),))
    state = _state((early, later), observed=(ObservedStarter(0, "early", ("PG",)),))

    completed = complete_assignment(state, (early,), _assignment("early", 0, "G"))

    assert tuple(item.player_id for item in completed) == ("early", None)


def test_completion_excludes_fixed_slots_and_preserves_players_with_no_future_games() -> None:
    early = _opportunity("early", "early", NOW + timedelta(hours=1), ("PG",), ((20, 1),))
    fixed = _opportunity(
        "fixed",
        "done",
        NOW - timedelta(hours=2),
        ("PG",),
        ((40, 1),),
        status=PlanningGameStatus.FINAL,
        completed_score=40,
        finalized_at=NOW - timedelta(hours=1),
    )
    state = _state((early, fixed), starter_slots=tuple(StarterSlot(i, "UTIL") for i in range(3)))
    state = replace(
        state,
        roster_player_ids=("early", "fixed", "finished"),
        observed_starters=(
            ObservedStarter(0, "fixed", ("PG",)),
            ObservedStarter(2, "finished", ("C",)),
        ),
        fixed_slots=(FixedSlot(0, "UTIL", "fixed", "done", 40, NOW, "ack", "fixture"),),
    )

    completed = complete_assignment(state, (early,), _assignment("early", 1))

    assert tuple((item.slot_index, item.player_id) for item in completed) == (
        (1, "early"),
        (2, "finished"),
    )


def test_completion_rejects_active_starter_displacement() -> None:
    early = _opportunity("early", "early", NOW + timedelta(hours=1), ("PG",), ((20, 1),))
    active = _opportunity(
        "active",
        "active",
        NOW - timedelta(hours=1),
        ("PG",),
        ((10, 1),),
        status=PlanningGameStatus.ACTIVE,
    )
    state = _state(
        (early, active),
        observed=(ObservedStarter(0, "active", ("PG",)),),
        starter_slots=(StarterSlot(0, "G"),),
    )

    with pytest.raises(WeeklyPlanError, match="omit an active starter"):
        complete_assignment(state, (early,), _assignment("early", 0, "G"))


def test_completion_is_stable_under_input_order_changes() -> None:
    early = _opportunity("early", "early", NOW + timedelta(hours=1), ("PG",), ((20, 1),))
    state = _state((early,), starter_slots=tuple(StarterSlot(i, "UTIL") for i in range(3)))
    state = replace(
        state,
        roster_player_ids=("early", "a", "b"),
        observed_starters=(ObservedStarter(0, "a", ("PG",)), ObservedStarter(1, "b", ("PG",))),
    )

    completed = complete_assignment(state, (early,), _assignment("early", 0))
    reordered = replace(
        state,
        observed_starters=tuple(reversed(state.observed_starters)),
        starter_slots=tuple(reversed(state.starter_slots)),
    )

    assert completed == complete_assignment(reordered, (early,), _assignment("early", 0))
    assert tuple(item.player_id for item in completed) == ("early", "b", "a")
