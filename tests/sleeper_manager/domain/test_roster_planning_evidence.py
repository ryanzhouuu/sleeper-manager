"""Reject invalid bench-capacity and reserve-membership evidence at the boundary."""

from dataclasses import replace
from datetime import timedelta

import pytest

from sleeper_manager.domain.planning import ObservedStarter, PlanningStateError
from tests.sleeper_manager.decisions.weekly_plan_support import NOW, _opportunity, _state


@pytest.mark.parametrize(
    "changes, reason",
    (
        ({"bench_capacity": -1}, "capacity"),
        ({"reserve_player_ids": ("missing",)}, "reserve"),
        ({"reserve_player_ids": ("a", "a")}, "reserve"),
        (
            {
                "reserve_player_ids": ("a",),
                "observed_starters": (ObservedStarter(0, "a", ("PG",)),),
            },
            "reserve",
        ),
    ),
)
def test_invalid_roster_constraints_are_rejected(changes: dict[str, object], reason: str) -> None:
    state = _state((_opportunity("a", "a", NOW + timedelta(hours=1), ("PG",), ((20, 1),)),))

    with pytest.raises(PlanningStateError, match=reason):
        replace(state, **changes)
