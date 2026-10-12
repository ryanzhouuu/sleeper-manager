"""Verify cached Lock/Pass values against independent generic assignment rollouts."""

from datetime import UTC, datetime

import pytest

from sleeper_manager.decisions._lock_in_continuations import lock_in_continuations
from sleeper_manager.decisions.lineup import AssignmentCandidate
from sleeper_manager.decisions.simulation import (
    Scenario,
    ScenarioInput,
    rollout_scenario_assignments,
)
from sleeper_manager.domain.planning import StarterSlot
from sleeper_manager.domain.projection import ProjectionDistribution, ProjectionSnapshot


@pytest.mark.parametrize("score", (-5.0, 10.0, 40.123456))
def test_cached_lock_values_match_reference(score: float) -> None:
    """Preserve exact slot restrictions, future-player exclusion, and fixed scores."""
    now = datetime(2026, 1, 5, tzinfo=UTC)
    slots = (StarterSlot(2, "G"), StarterSlot(5, "UTIL"), StarterSlot(9, "C"))
    inputs = tuple(
        ScenarioInput(
            candidate_id=key,
            player_id=player,
            game_id=key,
            eligible_positions=positions,
            eligible_slot_indices=indices,
            projection=ProjectionSnapshot(
                player,
                key,
                now,
                "model",
                "input",
                "scoring",
                ProjectionDistribution.from_weighted_observations(((0, 1),)),
                (),
            ),
        )
        for key, player, positions, indices in (
            ("a", "p1", ("PG",), (2, 5)),
            ("b", "p1", ("C",), (5, 9)),
            ("c", "p2", ("C",), (5, 9)),
            ("d", "p3", ("PG",), (2, 5)),
        )
    )
    scenarios = tuple(
        Scenario(tuple(zip(("a", "b", "c", "d"), values, strict=True)))
        for values in ((10.0, 14.0, 11.0, 9.0), (-2.0, -5.0, 8.0, 20.0))
    )
    actual = lock_in_continuations(
        inputs,
        slots,
        scenarios=scenarios,
        player_id="p1",
        completed_score=score,
        legal_slot_indices=(2, 5),
        locked_score=7.0,
    )
    reference = rollout_scenario_assignments(
        fixed_assignments=(),
        remaining_inputs=inputs,
        open_slots=tuple(s.position for s in slots),
        slot_indices=tuple(s.index for s in slots),
        scenarios=scenarios,
    )
    assert actual.pass_scores == pytest.approx(tuple(7 + r.score for r in reference))
    assert actual.pass_value == pytest.approx(7 + round(sum(r.score for r in reference) / 2, 6))
    for index, value, scores in actual.lock_options:
        remaining = tuple(s for s in slots if s.index != index)
        fixed = AssignmentCandidate("locked", "p1", score, ("PG",), eligible_slot_indices=(index,))
        reference = rollout_scenario_assignments(
            fixed_assignments=(fixed,),
            remaining_inputs=inputs,
            open_slots=tuple(s.position for s in remaining),
            slot_indices=tuple(s.index for s in remaining),
            scenarios=scenarios,
        )
        expected = tuple(7 + score + r.score for r in reference)
        assert scores == pytest.approx(expected)
        assert value == pytest.approx(7 + round(sum(score + r.score for r in reference) / 2, 6))
