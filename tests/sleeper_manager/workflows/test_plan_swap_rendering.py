"""Notifications and traces expose atomic swaps as one executable action."""

import json

from sleeper_manager.domain.planning import LineupMove
from sleeper_manager.workflows.plan_rendering import render_weekly_plan, serialize_weekly_plan_trace
from tests.sleeper_manager.decisions.weekly_plan_support import DEADLINE, _plan, _planned


def test_center_util_swap_names_both_players_and_positions() -> None:
    plan = _plan(
        observed_assignments=(_planned(0, "C", "jokic"), _planned(1, "UTIL", "embiid")),
        desired_assignments=(_planned(0, "C", "embiid"), _planned(1, "UTIL", "jokic")),
        moves=(LineupMove("jokic", 0, 1, DEADLINE, swap_player_id="embiid"),),
    )

    text = render_weekly_plan(plan, player_names={"jokic": "Jokić", "embiid": "Embiid"})

    assert text.message.split("\n\n")[1] == "• Swap Jokić (C) and Embiid (UTIL)."


def test_bench_swap_uses_a_single_instruction() -> None:
    plan = _plan(
        observed_assignments=(_planned(0, "G", "p1"),),
        desired_assignments=(_planned(0, "G", "p2"),),
        moves=(LineupMove("p2", None, 0, DEADLINE, swap_player_id="p1"),),
    )

    assert render_weekly_plan(plan).message.split("\n\n")[1] == "• Start p2 at G; bench p1."


def test_benching_swap_still_names_the_incoming_starter_first() -> None:
    plan = _plan(
        observed_assignments=(_planned(0, "G", "p1"),),
        desired_assignments=(_planned(0, "G", "p2"),),
        moves=(LineupMove("p1", 0, None, DEADLINE, swap_player_id="p2"),),
    )

    assert render_weekly_plan(plan).message.split("\n\n")[1] == "• Start p2 at G; bench p1."


def test_trace_preserves_swap_participants_and_action_type() -> None:
    plan = _plan(moves=(LineupMove("p1", 0, 1, DEADLINE, swap_player_id="p2"),))

    trace = json.loads(serialize_weekly_plan_trace(plan))

    assert trace["schema_version"] == 3
    assert trace["moves"] == [
        {
            "kind": "swap",
            "player_id": "p1",
            "swap_player_id": "p2",
            "source_slot_index": 0,
            "target_slot_index": 1,
            "deadline": DEADLINE.isoformat(),
        }
    ]
