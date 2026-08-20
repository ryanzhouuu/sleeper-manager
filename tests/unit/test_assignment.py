from sleeper_manager.decisions.lineup import (
    AssignmentCandidate,
    AssignmentResult,
    SlotAssignment,
    candidate_eligible_for_slot,
    maximum_weight_assignment,
)


def test_assignment_uses_dummy_for_negative_scores_and_respects_player_uniqueness() -> None:
    result = maximum_weight_assignment(
        [
            AssignmentCandidate("p1-pg", "p1", 5, ("PG",)),
            AssignmentCandidate("p1-util", "p1", 100, ("UTIL",)),
            AssignmentCandidate("p2", "p2", 4, ("PG",)),
            AssignmentCandidate("negative", "p3", -10, ("UTIL",)),
        ],
        ("PG", "UTIL"),
    )

    assert result.score == 104
    assert [assignment.player_id for assignment in result.assignments] == ["p2", "p1"]


def test_assignment_supports_exact_slots_and_required_alternative_edges() -> None:
    candidates = (
        AssignmentCandidate("p1-g", "p1", 8, ("PG",), eligible_slot_indices=(2,)),
        AssignmentCandidate("p2-util", "p2", 7, ("PG",), eligible_slot_indices=(5,)),
    )

    selected = maximum_weight_assignment(
        candidates,
        ("G", "UTIL"),
        slot_indices=(2, 5),
    )
    alternative = maximum_weight_assignment(
        candidates,
        ("G", "UTIL"),
        slot_indices=(2, 5),
        forbidden_edges=frozenset({(2, "p1-g")}),
        required_edges=frozenset({(5, "p2-util")}),
    )

    assert tuple(item.slot_index for item in selected.assignments) == (2, 5)
    assert tuple(item.player_id for item in selected.assignments) == ("p1", "p2")
    assert tuple(item.player_id for item in alternative.assignments) == (None, "p2")


def test_bitmask_assignment_matches_exhaustive_oracle_for_small_roster() -> None:
    candidates = (
        AssignmentCandidate(
            "p1:g1:segment-a",
            "p1",
            11,
            ("PG", "SG"),
            game_id="g1",
            eligible_slot_indices=(0, 1),
        ),
        AssignmentCandidate(
            "p2:g2:segment-b",
            "p2",
            9,
            ("C",),
            game_id="g2",
            eligible_slot_indices=(1, 2),
        ),
        AssignmentCandidate(
            "p3:g3:segment-a",
            "p3",
            -2,
            ("PG", "C"),
            game_id="g3",
        ),
        AssignmentCandidate(
            "p4:g4:segment-c",
            "p4",
            7,
            ("PF",),
            game_id="g4",
        ),
    )
    slots = ("G", "UTIL", "UTIL")
    slot_indices = (0, 1, 2)

    expected = _exhaustive_assignment(candidates, slots, slot_indices=slot_indices)
    actual = maximum_weight_assignment(candidates, slots, slot_indices=slot_indices)

    assert actual == expected
    assert tuple(item.game_id for item in actual.assignments) == ("g1", "g2", "g4")


def test_bitmask_assignment_matches_exhaustive_oracle_with_fixed_and_constrained_edges() -> None:
    candidates = (
        AssignmentCandidate(
            "fixed:0:p0:g0",
            "p0",
            20,
            ("PG",),
            game_id="g0",
            eligible_slot_indices=(0,),
        ),
        AssignmentCandidate(
            "p1:g1:segment-a",
            "p1",
            8,
            ("PG",),
            game_id="g1",
            eligible_slot_indices=(1,),
        ),
        AssignmentCandidate(
            "p2:g2:segment-b",
            "p2",
            8,
            ("C",),
            game_id="g2",
            eligible_slot_indices=(1,),
        ),
    )
    slots = ("G", "UTIL")
    slot_indices = (0, 1)
    forbidden = frozenset({(1, "p1:g1:segment-a")})
    required = frozenset({(0, "fixed:0:p0:g0")})

    expected = _exhaustive_assignment(
        candidates,
        slots,
        slot_indices=slot_indices,
        forbidden_edges=forbidden,
        required_edges=required,
    )
    actual = maximum_weight_assignment(
        candidates,
        slots,
        slot_indices=slot_indices,
        forbidden_edges=forbidden,
        required_edges=required,
    )

    assert actual == expected
    assert actual.assignments == (
        SlotAssignment(0, "G", "fixed:0:p0:g0", "p0", "g0", 20),
        SlotAssignment(1, "UTIL", "p2:g2:segment-b", "p2", "g2", 8),
    )


def _exhaustive_assignment(
    candidates: tuple[AssignmentCandidate, ...],
    slots: tuple[str, ...],
    *,
    slot_indices: tuple[int, ...],
    forbidden_edges: frozenset[tuple[int, str]] = frozenset(),
    required_edges: frozenset[tuple[int, str]] = frozenset(),
) -> AssignmentResult:
    required_by_slot = dict(required_edges)
    best: AssignmentResult | None = None

    def visit(
        position: int,
        used_players: frozenset[str],
        assignments: tuple[SlotAssignment, ...],
    ) -> None:
        nonlocal best
        if position == len(slots):
            result = AssignmentResult(
                round(sum(assignment.score for assignment in assignments), 6), assignments
            )
            if best is None or _better(result, best):
                best = result
            return

        slot_index = slot_indices[position]
        slot_position = slots[position].upper()
        if slot_index not in required_by_slot:
            dummy = SlotAssignment(slot_index, slot_position, None, None, None, 0.0)
            visit(position + 1, used_players, assignments + (dummy,))
        for candidate in sorted(candidates, key=lambda item: item.candidate_id):
            if not candidate_eligible_for_slot(
                candidate,
                slot_index=slot_index,
                slot_position=slot_position,
            ):
                continue
            if (
                slot_index in required_by_slot
                and required_by_slot[slot_index] != candidate.candidate_id
            ):
                continue
            if (slot_index, candidate.candidate_id) in forbidden_edges:
                continue
            if candidate.player_id in used_players:
                continue
            assignment = SlotAssignment(
                slot_index,
                slot_position,
                candidate.candidate_id,
                candidate.player_id,
                candidate.game_id,
                candidate.score,
            )
            visit(
                position + 1,
                used_players | {candidate.player_id},
                assignments + (assignment,),
            )

    visit(0, frozenset(), ())
    assert best is not None
    return best


def _better(candidate: AssignmentResult, incumbent: AssignmentResult) -> bool:
    if candidate.score != incumbent.score:
        return candidate.score > incumbent.score
    candidate_key = tuple(item.candidate_id or "" for item in candidate.assignments)
    incumbent_key = tuple(item.candidate_id or "" for item in incumbent.assignments)
    return candidate_key < incumbent_key
