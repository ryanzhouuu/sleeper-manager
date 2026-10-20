"""Intermediate swaps must not silently forfeit retained completed performances."""

from dataclasses import replace
from datetime import timedelta

import pytest

from sleeper_manager.decisions._weekly_plan_moves import plan_moves
from sleeper_manager.decisions.weekly_plan import WeeklyPlanError
from sleeper_manager.domain.lineup_moves import apply_lineup_moves
from sleeper_manager.domain.planning import (
    ObservedStarter,
    PassedOpportunity,
    PlanningGameStatus,
    StarterSlot,
)
from tests.sleeper_manager.decisions.weekly_plan_support import NOW, _opportunity, _planned, _state


@pytest.mark.parametrize("passed_buffer", (False, True))
def test_only_explicitly_passed_starters_can_be_benched_as_rotation_buffers(
    passed_buffer: bool,
) -> None:
    positions = {
        "a": ("PG", "SG"),
        "b": ("SG", "SF"),
        "c": ("SF", "PG"),
        "buffer": ("PG", "SG", "SF"),
    }
    upcoming = NOW + timedelta(hours=1)
    state = replace(
        _state(
            (
                *(
                    _opportunity(
                        player,
                        player,
                        NOW - timedelta(hours=2),
                        positions[player],
                        ((20, 1),),
                        status=PlanningGameStatus.FINAL,
                        completed_score=20,
                        finalized_at=NOW - timedelta(minutes=10),
                    )
                    for player in ("a", "b", "c")
                ),
                _opportunity("buffer", "buffer", upcoming, positions["buffer"], ((20, 1),)),
            ),
            observed=tuple(
                ObservedStarter(i, player, positions[player])
                for i, player in enumerate(("a", "b", "c"))
            ),
            starter_slots=tuple(StarterSlot(i, slot) for i, slot in enumerate(("PG", "SG", "SF"))),
            passed=(PassedOpportunity("b", "b", NOW, "pass-b", "fixture"),)
            if passed_buffer
            else (),
        ),
        bench_capacity=1,
    )
    desired = (_planned(0, "PG", "c"), _planned(1, "SG", "a"), _planned(2, "SF", "b"))
    if not passed_buffer:
        with pytest.raises(WeeklyPlanError, match="legal lineup sequence"):
            plan_moves(state, desired, upcoming, timedelta(0))
        return

    moves = plan_moves(state, desired, upcoming, timedelta(0))

    lineup = {i: player for i, player in enumerate(("a", "b", "c"))}
    for move in moves:
        lineup = apply_lineup_moves((move,), lineup)
        assert {"a", "c"} <= set(lineup.values())
    assert tuple(lineup.values()) == ("c", "a", "b")
