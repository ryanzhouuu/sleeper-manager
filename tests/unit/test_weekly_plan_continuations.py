"""Prove shared continuation tables match the reference assignment rollout."""

from datetime import UTC, datetime

from sleeper_manager.decisions._weekly_plan_continuations import (
    PreparedContinuationTopology,
)
from sleeper_manager.decisions._weekly_plan_evaluations import (
    assignment_terminal_value,
    enumerate_current_assignments,
)
from sleeper_manager.decisions._weekly_plan_terminal_values import assignment_terminal_values
from sleeper_manager.decisions.lineup import AssignmentCandidate, maximum_weight_assignment
from sleeper_manager.decisions.simulation import Scenario, ScenarioInput
from sleeper_manager.domain.planning import StarterSlot
from sleeper_manager.domain.projection import ProjectionDistribution, ProjectionSnapshot

NOW = datetime(2026, 1, 5, 12, tzinfo=UTC)


def _input(
    candidate_id: str,
    player_id: str,
    positions: tuple[str, ...],
    slot_indices: tuple[int, ...] | None = None,
) -> ScenarioInput:
    """Build topology input whose projection values are supplied by scenarios."""

    return ScenarioInput(
        candidate_id=candidate_id,
        player_id=player_id,
        game_id=f"game-{candidate_id}",
        eligible_positions=positions,
        projection=ProjectionSnapshot(
            player_id=player_id,
            game_id=f"game-{candidate_id}",
            available_as_of=NOW,
            model_version="fixture-model",
            input_version="fixture-inputs",
            scoring_policy_version="fixture-scoring",
            distribution=ProjectionDistribution.from_weighted_observations(((0.0, 1.0),)),
            reasons=(),
        ),
        eligible_slot_indices=slot_indices,
    )


def test_continuation_table_matches_reference_for_every_allowed_mask() -> None:
    """Match exact solver values across slot masks and excluded current players."""

    slots = (StarterSlot(2, "G"), StarterSlot(5, "UTIL"), StarterSlot(9, "C"))
    inputs = (
        _input("a-later-1", "p1", ("PG",), (2, 5)),
        _input("a-later-2", "p1", ("C",), (5, 9)),
        _input("b-later", "p2", ("C",), (5, 9)),
        _input("c-later", "p3", ("PG",), (2, 5)),
        _input("negative", "p4", ("PG", "C")),
    )
    values = {
        "a-later-1": 10.0,
        "a-later-2": 14.0,
        "b-later": 14.0000000005,
        "c-later": 10.0,
        "negative": -4.0,
    }
    topology = PreparedContinuationTopology(inputs, slots)
    exclusion_groups = (
        frozenset(),
        frozenset({"p1"}),
        frozenset({"p2", "p3"}),
    )
    tables = topology.best_scores_by_excluded_players(
        values,
        {group: frozenset(range(1 << len(slots))) for group in exclusion_groups},
    )

    for excluded_players in exclusion_groups:
        table = tables[excluded_players]
        for allowed_mask in range(1 << len(slots)):
            allowed_slots = tuple(
                slot for position, slot in enumerate(slots) if allowed_mask & (1 << position)
            )
            candidates = tuple(
                AssignmentCandidate(
                    candidate_id=item.candidate_id,
                    player_id=item.player_id,
                    score=values[item.candidate_id],
                    eligible_positions=item.eligible_positions,
                    game_id=item.game_id,
                    eligible_slot_indices=item.eligible_slot_indices,
                )
                for item in inputs
                if item.player_id not in excluded_players
            )
            expected = maximum_weight_assignment(
                candidates,
                tuple(slot.position for slot in allowed_slots),
                slot_indices=tuple(slot.index for slot in allowed_slots),
            ).score

            assert table[allowed_mask] == expected


def test_exclusion_tables_do_not_depend_on_group_order() -> None:
    """Branch evaluation must leave cached parent slot states reusable."""

    slots = (StarterSlot(0, "G"), StarterSlot(1, "F"), StarterSlot(2, "UTIL"))
    inputs = (
        _input("a", "p1", ("PG", "SF")),
        _input("b", "p2", ("PG",)),
        _input("c", "p3", ("SF",)),
    )
    values = {"a": 12.0, "b": 11.0, "c": 10.0}
    topology = PreparedContinuationTopology(inputs, slots)
    groups = (
        frozenset({"p1", "p2"}),
        frozenset({"p1"}),
        frozenset({"p2"}),
        frozenset(),
    )
    masks = frozenset(range(1 << len(slots)))

    forward = topology.best_scores_by_excluded_players(values, {group: masks for group in groups})
    reverse = topology.best_scores_by_excluded_players(
        values, {group: masks for group in reversed(groups)}
    )

    assert forward == reverse
    assert forward[frozenset()][topology.full_slot_mask] == 33.0
    assert forward[frozenset({"p1", "p2"})][topology.full_slot_mask] == 10.0


def test_shared_terminal_values_match_reference_assignment_rollouts() -> None:
    """Preserve scenario order, future-player exclusion, and active placeholders."""

    slots = (StarterSlot(0, "G"), StarterSlot(1, "UTIL"))
    current = (
        AssignmentCandidate("now-p1@slot-0", "p1", 0.0, ("PG",), eligible_slot_indices=(0,)),
        AssignmentCandidate("now-p1@slot-1", "p1", 0.0, ("PG",), eligible_slot_indices=(1,)),
        AssignmentCandidate("now-p2@slot-1", "p2", 0.0, ("C",), eligible_slot_indices=(1,)),
        AssignmentCandidate(
            "active:now-p3",
            "p3",
            0.0,
            ("PG",),
            eligible_slot_indices=(0, 1),
        ),
    )
    future = (
        _input("later-p1", "p1", ("PG",)),
        _input("later-p2", "p2", ("C",)),
        _input("later-p4", "p4", ("PG", "C")),
    )
    scenarios = (
        Scenario(
            (
                ("now-p1", 11.0),
                ("now-p2", 7.0),
                ("later-p1", 20.0),
                ("later-p2", 8.0),
                ("later-p4", 5.0),
            )
        ),
        Scenario(
            (
                ("now-p1", -2.0),
                ("now-p2", 13.0),
                ("later-p1", 4.0),
                ("later-p2", 18.0),
                ("later-p4", 9.0),
            )
        ),
    )
    assignments = enumerate_current_assignments(
        current,
        slots,
        required_player_ids=frozenset({"p3"}),
    )
    ignored = frozenset({"active:now-p3"})

    actual = assignment_terminal_values(
        assignments,
        candidates=current,
        fixed_assignments=(),
        future_inputs=future,
        open_slots=slots,
        scenarios=scenarios,
        ignored_candidate_ids=ignored,
    )

    for assignment in assignments:
        assert actual[assignment] == assignment_terminal_value(
            assignment,
            candidates=current,
            fixed_assignments=(),
            future_inputs=future,
            open_slots=slots,
            scenarios=scenarios,
            ignored_candidate_ids=ignored,
        )
