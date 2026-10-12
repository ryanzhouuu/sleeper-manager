"""Check bounded postgame choices without revealing the scored future draw."""

from datetime import UTC, datetime

import pytest

from sleeper_manager.decisions._weekly_plan_recourse import recourse_terminal_values
from sleeper_manager.decisions.lineup import AssignmentCandidate, AssignmentResult, SlotAssignment
from sleeper_manager.decisions.simulation import Scenario, ScenarioInput
from sleeper_manager.domain.planning import StarterSlot
from sleeper_manager.domain.projection import ProjectionDistribution, ProjectionSnapshot


def _input(player: str, candidate: str, indices: tuple[int, ...]) -> ScenarioInput:
    return ScenarioInput(
        candidate,
        player,
        candidate,
        ("PG",),
        ProjectionSnapshot(
            player,
            candidate,
            datetime(2026, 1, 5, tzinfo=UTC),
            "model",
            "input",
            "scoring",
            ProjectionDistribution.from_weighted_observations(((10, 1),)),
            (),
        ),
        indices,
    )


def _assignment(*placements: tuple[str, str, int]) -> AssignmentResult:
    return AssignmentResult(
        0,
        tuple(
            SlotAssignment(index, "UTIL", candidate, player, candidate, 0)
            for candidate, player, index in placements
        ),
    )


def _values(
    outer,
    inner,
    *,
    placements=(("current", "p1", 2),),
    future=None,
    confidence=0.7,
    slots=None,
    fixed=(),
):
    candidates = tuple(
        AssignmentCandidate(c, p, 0, ("PG",), eligible_slot_indices=(s,)) for c, p, s in placements
    )
    selected = _assignment(*placements)
    empty = _assignment()
    result = recourse_terminal_values(
        (selected, empty),
        candidates=candidates,
        fixed_assignments=fixed,
        future_inputs=(_input("p1", "later", (2,)),) if future is None else future,
        open_slots=(StarterSlot(2, "UTIL"),) if slots is None else slots,
        scenarios=outer,
        postgame_scenarios=inner,
        minimum_confidence=confidence,
        tie_tolerance=0.01,
    )
    return result[selected], result[empty]


def test_start_observe_then_lock_or_pass_has_positive_option_value() -> None:
    """Exhaust the 0/20-then-10 example: starting is worth 15 rather than 10."""
    outer = tuple(Scenario((("current", score), ("later", 10))) for score in (0, 20))
    assert _values(outer, (Scenario((("later", 10),)),)) == (15, 10)


def test_postgame_action_cannot_see_the_outer_future_outcome() -> None:
    """An observed 15 locks against the inner constant 10 even when outer future is 100."""
    outer = tuple(Scenario((("current", 15), ("later", score))) for score in (0, 100))
    assert _values(outer, (Scenario((("later", 10),)),)) == (15, 50)


@pytest.mark.parametrize("confidence,expected", ((0.7, 10), (0.5, 15)))
def test_optional_lock_requires_material_mean_gain_and_confidence(confidence, expected) -> None:
    outer = (Scenario((("current", 15), ("later", 10))),)
    inner = (Scenario((("later", 0),)), Scenario((("later", 20),)))
    assert _values(outer, inner, confidence=confidence)[0] == expected


def test_tied_lock_pass_value_preserves_the_continuation() -> None:
    outer = (Scenario((("current", 10), ("later", 100))),)
    assert _values(outer, (Scenario((("later", 10),)),), confidence=0)[0] == 100


def test_final_game_can_be_retained_without_optional_confidence_gate() -> None:
    outer = (Scenario((("current", -5),)), Scenario((("current", 20),)))
    assert _values(outer, (Scenario(()),), future=(), confidence=1) == (10, 0)


def test_joint_acceptance_charges_slot_and_player_conflicts_once() -> None:
    """Two known 60s beat a 100 continuation only when accepted together."""
    placements = (("a", "p1", 5), ("b", "p2", 2))
    outer = (Scenario((("a", 60), ("b", 60), ("later", 100))),)
    inner = (Scenario((("later", 100),)),)
    fixed = (AssignmentCandidate("fixed", "p3", 7, ("PG",)),)
    assert _values(
        outer,
        inner,
        placements=placements,
        slots=(StarterSlot(2, "G"), StarterSlot(5, "UTIL")),
        fixed=fixed,
    ) == (127, 107)


def test_recourse_rejects_empty_inner_or_outer_samples() -> None:
    with pytest.raises(ValueError, match="requires scenarios"):
        _values((), (Scenario(()),))
    with pytest.raises(ValueError, match="requires scenarios"):
        _values((Scenario(()),), ())
