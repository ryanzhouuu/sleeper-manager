"""Move generation, identity, and deadline invariants for weekly plans."""

from dataclasses import replace
from datetime import timedelta
from itertools import permutations

import pytest

from sleeper_manager.decisions._weekly_plan_moves import plan_moves
from sleeper_manager.decisions.weekly_plan import (
    build_weekly_plan,
)
from sleeper_manager.domain.planning import (
    FixedSlot,
    LineupMove,
    ObservedStarter,
    PlanningReasonCode,
    PlanningStateError,
    PlanStatus,
    StarterSlot,
)
from tests.sleeper_manager.decisions.weekly_plan_support import (
    DEADLINE,
    NOW,
    _opportunity,
    _plan,
    _planned,
    _state,
)


def test_swap_moves_require_a_temporary_bench_step() -> None:
    plan = _plan()

    assert plan.status is PlanStatus.ACTION_REQUIRED
    assert plan.material_hash


def test_generated_moves_preserve_explicit_empty_slots() -> None:
    """Reach every target permutation without dropping empty slot records."""

    start = NOW + timedelta(hours=1)
    opportunities = (
        _opportunity("a", "g-a", start, ("PG",), ((10, 1),), eligible_slot_indices=(0, 1, 2)),
        _opportunity("b", "g-b", start, ("PG",), ((10, 1),), eligible_slot_indices=(0, 1, 2)),
    )
    slots = tuple(StarterSlot(index, "UTIL") for index in range(3))
    lineups = tuple(permutations(("a", "b", None)))

    for observed_players in lineups:
        state = _state(
            opportunities,
            observed=tuple(
                ObservedStarter(index, player_id, ("PG",))
                for index, player_id in enumerate(observed_players)
                if player_id is not None
            ),
            starter_slots=slots,
        )
        observed = tuple(
            _planned(index, "UTIL", player_id) for index, player_id in enumerate(observed_players)
        )
        for desired_players in lineups:
            desired = tuple(
                _planned(index, "UTIL", player_id)
                for index, player_id in enumerate(desired_players)
            )
            moves = plan_moves(state, desired, start, timedelta(minutes=10))

            _plan(
                status=PlanStatus.ACTION_REQUIRED if moves else PlanStatus.NO_ACTION,
                observed_assignments=observed,
                desired_assignments=desired,
                moves=moves,
            )


def test_double_occupying_move_orders_fail_closed() -> None:
    with pytest.raises(PlanningStateError, match="targets an occupied slot"):
        _plan(
            moves=(
                LineupMove("p1", 0, 1, DEADLINE),
                LineupMove("p2", 1, 0, DEADLINE),
            )
        )


def test_three_way_cycles_require_a_temporary_bench_step() -> None:
    observed = (
        _planned(0, "G", "a"),
        _planned(1, "UTIL", "b"),
        _planned(2, "UTIL", "c"),
    )
    desired = (
        _planned(0, "G", "b"),
        _planned(1, "UTIL", "c"),
        _planned(2, "UTIL", "a"),
    )
    naive = (
        LineupMove("a", 0, 2, DEADLINE),
        LineupMove("b", 1, 0, DEADLINE),
        LineupMove("c", 2, 1, DEADLINE),
    )
    staged = (
        LineupMove("a", 0, None, DEADLINE),
        LineupMove("b", 1, 0, DEADLINE),
        LineupMove("c", 2, 1, DEADLINE),
        LineupMove("a", None, 2, DEADLINE),
    )

    with pytest.raises(PlanningStateError, match="targets an occupied slot"):
        _plan(observed_assignments=observed, desired_assignments=desired, moves=naive)

    plan = _plan(observed_assignments=observed, desired_assignments=desired, moves=staged)
    assert len(plan.moves) == 4


def test_swap_moves_share_the_earliest_affected_tipoff() -> None:
    tonight = NOW + timedelta(hours=2)
    tomorrow = NOW + timedelta(days=1)
    state = _state(
        (
            _opportunity("a", "g_tonight", tonight, ("PG",), ((40, 1),)),
            _opportunity("b", "g_tomorrow", tomorrow, ("PG",), ((10, 1),)),
        ),
        observed=(ObservedStarter(0, "b", ("PG",)),),
        starter_slots=(StarterSlot(0, "G"),),
    )

    plan = build_weekly_plan(state)

    assert [
        (move.player_id, move.source_slot_index, move.target_slot_index) for move in plan.moves
    ] == [
        ("b", 0, None),
        ("a", None, 0),
    ]
    assert all(move.deadline == tonight - timedelta(minutes=10) for move in plan.moves)


def test_locked_slots_cannot_appear_as_move_sources_or_targets() -> None:
    fixed = (FixedSlot(1, "UTIL", "p2", "g9", 5, NOW - timedelta(days=1), "lock-1", "fixture"),)
    assignments = (
        _planned(0, "G", "p1"),
        _planned(1, "UTIL", "p2"),
    )
    round_trip = (
        LineupMove("p2", 1, None, DEADLINE),
        LineupMove("p2", None, 1, DEADLINE),
    )
    with pytest.raises(PlanningStateError, match="cannot appear as move sources or targets"):
        _plan(
            observed_assignments=assignments,
            desired_assignments=assignments,
            moves=round_trip,
            fixed_slots=fixed,
        )


def test_material_hash_ignores_explanation_only_drift() -> None:
    plan = _plan()

    drifted = replace(plan, expected_terminal_score=999.5, decision_margin=-1.25)
    reobserved = replace(plan, observed_terminal_score=7.5)
    retimed = replace(
        plan,
        moves=tuple(
            replace(move, deadline=move.deadline + timedelta(minutes=1)) for move in plan.moves
        ),
    )

    assert plan.material_hash == drifted.material_hash
    assert plan.plan_id != drifted.plan_id
    assert plan.material_hash == reobserved.material_hash
    assert plan.plan_id != reobserved.plan_id
    assert plan.material_hash != retimed.material_hash


def test_plan_identity_tracks_lineage_versions() -> None:
    plan = _plan()

    relined = replace(plan, input_version="fixture-inputs-2")

    assert plan.material_hash == relined.material_hash
    assert plan.plan_id != relined.plan_id


def test_actionable_deadlines_must_follow_the_decision_time() -> None:
    expired = tuple(replace(move, deadline=NOW - timedelta(minutes=1)) for move in _plan().moves)
    with pytest.raises(PlanningStateError, match="follow the decision time"):
        _plan(moves=expired)


def test_status_and_move_counts_must_agree() -> None:
    with pytest.raises(PlanningStateError, match="No-action plans"):
        _plan(status=PlanStatus.NO_ACTION)
    with pytest.raises(PlanningStateError, match="Actionable plans require moves"):
        _plan(
            moves=(),
            desired_assignments=(
                _planned(0, "G", "p1"),
                _planned(1, "UTIL", "p2"),
            ),
        )
    with pytest.raises(PlanningStateError, match="blocking reasons"):
        _plan(blocking_reasons=(PlanningReasonCode.MISSING_PROJECTION,))
    blocked = _plan(
        status=PlanStatus.BLOCKED,
        blocking_reasons=(PlanningReasonCode.DEADLINE_ELAPSED,),
        moves=tuple(replace(move, deadline=NOW - timedelta(minutes=1)) for move in _plan().moves),
    )
    assert blocked.status is PlanStatus.BLOCKED
