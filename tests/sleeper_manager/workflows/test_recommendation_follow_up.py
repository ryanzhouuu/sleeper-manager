"""Compact alerts preserve executable ordering, recovery consequences, and trace detail."""

import json
from dataclasses import replace
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from sleeper_manager.domain.lock_in import LockInDecisionKind
from sleeper_manager.domain.planning import LineupMove, PlanningReasonCode, PlanStatus
from sleeper_manager.domain.planning_actions import PlannedScoreAction, RecoveryObligation
from sleeper_manager.workflows.plan_rendering import render_weekly_plan, serialize_weekly_plan_trace
from tests.sleeper_manager.decisions.weekly_plan_support import DEADLINE, NOW, _plan, _planned

NAMES = {"p1": "Alice", "p2": "Bob", "p3": "Cal", "trial": "Tre", "bench": "Ben"}
BASELINE = (_planned(0, "PG", "p1"), _planned(1, "SG", "p2"), _planned(2, "UTIL", "p3"))
ATTEMPT = (BASELINE[0], _planned(1, "SG", "trial"), BASELINE[2])
RECOVERY = RecoveryObligation(
    added_player_games=(("trial", "tonight"),),
    baseline_assignments=BASELINE,
    attempt_assignments=ATTEMPT,
    recovery_at=NOW + timedelta(hours=2),
    deadline=NOW + timedelta(hours=1, minutes=50),
    created_at=NOW,
    input_version="fixture-inputs",
)


def test_recovery_lists_changed_slots_and_keeps_deadline_and_forfeiture_warning() -> None:
    plan = _plan(
        observed_assignments=BASELINE,
        desired_assignments=ATTEMPT,
        moves=(LineupMove("trial", None, 1, DEADLINE, swap_player_id="p2"),),
        recovery_obligations=(RECOVERY,),
        bench_player_ids=("p2", "bench"),
    )

    message = render_weekly_plan(
        plan, player_names=NAMES, local_timezone=ZoneInfo("America/Chicago")
    ).message

    assert message.endswith(
        "Review before Mon 7:50 AM CST: restore Bob at SG or follow updated advice. "
        "Benching Tre forfeits any active attempt."
    )
    assert "Alice" not in message
    assert "Cal" not in message
    assert "Complete lineup" not in message
    assert "Bench:" not in message
    trace = json.loads(serialize_weekly_plan_trace(plan))
    assert len(trace["desired_assignments"]) == 3
    assert trace["bench_player_ids"] == ["p2", "bench"]
    assert len(trace["recovery_obligations"][0]["baseline_assignments"]) == 3


def test_recovery_retains_duplicate_slot_labels_empty_slots_and_multiple_attempts() -> None:
    baseline = (_planned(0, "UTIL", "p1"), _planned(1, "UTIL", None))
    attempt = (_planned(0, "UTIL", "trial"), _planned(1, "UTIL", "p2"))
    recovery = replace(
        RECOVERY,
        baseline_assignments=baseline,
        attempt_assignments=attempt,
        added_player_games=(("trial", "tonight"), ("p2", "other")),
    )
    plan = _plan(
        observed_assignments=baseline,
        desired_assignments=attempt,
        moves=(
            LineupMove("trial", None, 0, DEADLINE, swap_player_id="p1"),
            LineupMove("p2", None, 1, DEADLINE),
        ),
        recovery_obligations=(recovery,),
    )

    message = render_weekly_plan(plan, player_names=NAMES).message

    assert "restore Alice at UTIL 1; leave UTIL 2 open or follow updated advice." in message
    assert "Benching Tre, Bob forfeits any active attempt." in message


def test_blocked_advice_suppresses_retained_recovery_instructions() -> None:
    plan = _plan(
        status=PlanStatus.BLOCKED,
        blocking_reasons=(PlanningReasonCode.MISSING_PROJECTION,),
        recovery_obligations=(RECOVERY,),
    )

    message = render_weekly_plan(plan, player_names=NAMES).message

    assert "Review before" not in message
    assert "restore" not in message
    assert "Update in Sleeper" not in message
    assert "Projected week: unavailable." in message


def test_current_recovery_keeps_irreversible_forfeiture_visible() -> None:
    message = render_weekly_plan(
        _plan(forfeited_opportunities=(("trial", "tonight"),)), player_names=NAMES
    ).message

    assert "Recovery forfeits: Tre. Returning to starters cannot restore these games." in message


@pytest.mark.parametrize("pre_lock_move_count", (0, 1))
def test_passes_precede_moves_and_locks_follow_required_moves(pre_lock_move_count: int) -> None:
    plan = _plan(
        observed_assignments=(_planned(0, "PG", "p1"), _planned(1, "SG", "p2")),
        desired_assignments=(_planned(0, "PG", "p1"), _planned(1, "SG", "p3")),
        moves=(LineupMove("p3", None, 1, DEADLINE, swap_player_id="p2"),),
        pre_lock_move_count=pre_lock_move_count,
        score_actions=(
            PlannedScoreAction("p2", "old", LockInDecisionKind.PASS, 10, DEADLINE),
            PlannedScoreAction(
                "p1", "new", LockInDecisionKind.LOCK, 25, DEADLINE, 0, "PG", ((0, 100),), 90
            ),
        ),
    )

    message = render_weekly_plan(plan, player_names=NAMES).message
    actions = message.split("\n\n")[1].splitlines()
    lock = "• Lock Alice's 25.00 pts in PG in Sleeper, then confirm."
    move = "• Start Cal at SG; bench Bob."
    assert actions[0] == "• Record Pass on Bob's 10.00 pts; substitution is optional."
    assert actions[1:] == ([lock, move] if pre_lock_move_count == 0 else [move, lock])
    assert "Alternative (pass): 90.0 pts." in message
    assert "Lock placement totals" not in message
    trace = json.loads(serialize_weekly_plan_trace(plan))
    assert trace["score_actions"][1]["placement_values"] == [[0, 100]]


def test_score_only_advice_does_not_imply_a_pass_requires_a_sleeper_move() -> None:
    plan = _plan(
        moves=(),
        desired_assignments=_plan().observed_assignments,
        score_actions=(PlannedScoreAction("p1", "old", LockInDecisionKind.PASS, 10, DEADLINE),),
    )

    message = render_weekly_plan(plan, player_names=NAMES).message

    assert message.startswith("Decide before Mon 12:50 PM UTC\n")
    assert "Sleeper" not in message
