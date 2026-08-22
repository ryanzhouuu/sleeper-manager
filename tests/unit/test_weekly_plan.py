from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from sleeper_manager.domain.planning import (
    FixedSlot,
    LineupMove,
    PlanConfidence,
    PlannedAssignment,
    PlanningReasonCode,
    PlanningStateError,
    PlanStatus,
    WeeklyPlan,
)

NOW = datetime(2026, 1, 5, 12, tzinfo=UTC)
DEADLINE = NOW + timedelta(minutes=50)


def _planned(slot_index: int, position: str, player_id: str | None) -> PlannedAssignment:
    return PlannedAssignment(slot_index, position, player_id)


def _plan(**overrides: object) -> WeeklyPlan:
    values: dict[str, object] = {
        "league_id": "league-1",
        "season": "2026",
        "week": 1,
        "roster_id": 1,
        "decision_time": NOW,
        "status": PlanStatus.ACTION_REQUIRED,
        "observed_assignments": (
            _planned(0, "G", "p1"),
            _planned(1, "UTIL", "p2"),
        ),
        "desired_assignments": (
            _planned(0, "G", "p2"),
            _planned(1, "UTIL", "p1"),
        ),
        "moves": (
            LineupMove("p1", 0, None, DEADLINE),
            LineupMove("p2", 1, 0, DEADLINE),
            LineupMove("p1", None, 1, DEADLINE),
        ),
        "confidence": PlanConfidence.HIGH,
        "planner_version": "weekly-planner-v1",
        "manager_policy_version": "policy-v1",
    }
    values.update(overrides)
    return WeeklyPlan(**values)


def test_swap_moves_require_a_temporary_bench_step() -> None:
    plan = _plan()

    assert plan.status is PlanStatus.ACTION_REQUIRED
    assert plan.material_hash


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


def test_fixed_slots_demand_desired_preservation() -> None:
    fixed = (FixedSlot(1, "UTIL", "p2", "g9", 5, NOW - timedelta(days=1), "lock-1", "fixture"),)
    with pytest.raises(PlanningStateError, match="preserve a fixed slot"):
        _plan(fixed_slots=fixed)


def test_material_hash_ignores_explanation_only_drift() -> None:
    plan = _plan()

    drifted = replace(plan, expected_terminal_score=999.5, decision_margin=-1.25)
    retimed = replace(
        plan,
        moves=tuple(
            replace(move, deadline=move.deadline + timedelta(minutes=1)) for move in plan.moves
        ),
    )

    assert plan.material_hash == drifted.material_hash
    assert plan.plan_id != drifted.plan_id
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
